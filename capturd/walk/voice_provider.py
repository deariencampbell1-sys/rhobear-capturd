"""VoiceProvider abstraction (V2 voice layer) — Bedrock-era wiring.

Replaces the Gemini/Vertex TTS legacy. The switcher story:

* ``polly:<VoiceId>``   — AWS Polly neural/generative voices. THE provider for
  production narration: native word speech-marks feed the voice-synced camera
  (``step.voiceoverWords``), same contract edge-tts fills.
* ``edge:<name>`` / bare edge voice names — zero-config fallback (no AWS creds
  needed), unchanged.
* ``nova-sonic``        — SEAM ONLY. Amazon Nova Sonic is speech-to-speech
  bidirectional streaming on Bedrock; as of this pass it does not expose the
  word-level timings the camera sync needs, so it is not wired. The provider
  slot exists so it can be added without touching callers.

Legacy Vertex voice names (``vertex:Charon:warm``, bare ``Kore``, ...) are
ALIASED to Polly voices so existing demos keep regenerating.

Credentials: standard boto3 chain (env / profile / role). Region:
``CAPTURD_AWS_REGION`` or ``AWS_REGION`` or us-east-1.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("capturd.voice")


@dataclass(frozen=True)
class VoiceInfo:
    id: str            # canonical voice id, e.g. "polly:Ruth"
    label: str         # human name for the switcher
    provider: str      # polly | edge | nova-sonic
    description: str   # switcher copy
    style: str = ""    # taste tag: warm | firm | friendly | trailer | upbeat


# ---------------------------------------------------------------------------
# Legacy Vertex aliases — old demos must keep regenerating (backward compat)
# ---------------------------------------------------------------------------

_VERTEX_ALIASES: dict[str, str] = {
    "charon": "polly:Matthew",
    "kore": "polly:Danielle",
    "aoede": "polly:Olivia",
    "fenrir": "polly:Matthew",
    "zephyr": "polly:Kevin",
    "puck": "polly:Kevin",
    "leda": "polly:Ruth",
}


def normalize_voice(voice: str) -> str:
    """Map legacy/bare voice names to canonical provider ids."""
    v = (voice or "").strip()
    if not v:
        return DEFAULT_VOICE
    low = v.lower()
    if low.startswith("vertex:"):
        name = low.split(":")[1]
        return _VERTEX_ALIASES.get(name, DEFAULT_VOICE)
    if low in _VERTEX_ALIASES:
        return _VERTEX_ALIASES[low]
    if v.startswith(("polly:", "edge:", "nova-sonic:")):
        return v
    # Bare known polly voice id → canonical.
    if f"polly:{v}" in POLLY_VOICES:
        return f"polly:{v}"
    return v  # assume edge-style voice name (existing fallback behavior)


# ---------------------------------------------------------------------------
# Polly provider — AWS TTS with native word timings
# ---------------------------------------------------------------------------

#: Polly voices exposed in the switcher (engine, en). Generative where available.
POLLY_VOICES: dict[str, VoiceInfo] = {
    v.id: v for v in (
        VoiceInfo("polly:Ruth", "Ruth", "polly", "warm · natural read", "warm"),
        VoiceInfo("polly:Matthew", "Matthew", "polly", "deep · authoritative", "warm"),
        VoiceInfo("polly:Danielle", "Danielle", "polly", "firm · confident", "firm"),
        VoiceInfo("polly:Olivia", "Olivia", "polly", "breezy · friendly", "friendly"),
        VoiceInfo("polly:Amy", "Amy", "polly", "clear · en-GB polish", "friendly"),
        VoiceInfo("polly:Brian", "Brian", "polly", "steady · en-GB", "warm"),
        VoiceInfo("polly:Kevin", "Kevin", "polly", "bright · upbeat", "upbeat"),
    )
}

DEFAULT_VOICE = "polly:Ruth"


class PollyProvider:
    """AWS Polly. Two calls per synthesis: audio (mp3) + word speech-marks."""

    id = "polly"

    def __init__(self, region: str | None = None):
        self.region = region or os.environ.get("CAPTURD_AWS_REGION") \
            or os.environ.get("AWS_REGION") or "us-east-1"
        self._client = None

    def _polly(self):
        if self._client is None:
            import boto3  # lazy — optional dependency
            self._client = boto3.client("polly", region_name=self.region)
        return self._client

    def voices(self) -> list[VoiceInfo]:
        return list(POLLY_VOICES.values())

    def synthesize(self, text: str, voice: str) -> tuple[bytes, list[dict]]:
        """Return (mp3_bytes, [WordTimestamp-as-dict]) per the ai_pipeline
        contract. Raises RuntimeError on AWS errors (caller wraps)."""
        voice_id = voice.split(":", 1)[1] if ":" in voice else voice
        polly = self._polly()
        audio = polly.synthesize_speech(
            Engine="neural", OutputFormat="mp3", VoiceId=voice_id,
            Text=text, TextType="text",
        )
        audio_bytes = audio["AudioStream"].read()
        marks = polly.synthesize_speech(
            Engine="neural", OutputFormat="json", VoiceId=voice_id,
            Text=text, TextType="text", SpeechMarkTypes=["word"],
        )
        words: list[dict] = []
        for line in marks["AudioStream"].read().decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(m, dict):
                continue
            if m.get("type") == "word":
                # Polly speech marks: time and duration are BOTH milliseconds.
                t0 = int(m.get("time", 0))
                words.append({
                    "word": str(m.get("value", "")),
                    "tStartMs": t0,
                    "tEndMs": t0 + max(1, int(m.get("duration", 0) or 1)),
                })
        return audio_bytes, words


class NovaSonicProvider:
    """SEAM ONLY — Amazon Nova Sonic (Bedrock speech-to-speech streaming).

    Not wired: Nova Sonic's bidirectional stream does not expose the word-level
    audio timings this product's camera sync runs on. When AWS adds speech
    marks (or we adopt forced alignment), implement ``synthesize`` here and the
    registry picks it up — no caller changes.
    """

    id = "nova-sonic"

    def voices(self) -> list[VoiceInfo]:
        return []

    def synthesize(self, text: str, voice: str) -> tuple[bytes, list[dict]]:
        raise RuntimeError(
            "Nova Sonic TTS is not wired in this pass: its streaming output "
            "lacks the word timings the voice-synced camera needs. Use a "
            "polly: voice (see voice.list)."
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PROVIDERS = {"polly": PollyProvider, "nova-sonic": NovaSonicProvider}
_polly_instance: PollyProvider | None = None


def resolve_provider(canonical_voice: str):
    """Return (provider_instance, voice_id_for_provider). edge:* → (None, name)
    meaning "caller uses the existing edge-tts path"."""
    global _polly_instance
    if canonical_voice.startswith("polly:"):
        if _polly_instance is None:
            _polly_instance = PollyProvider()
        return _polly_instance, canonical_voice.split(":", 1)[1]
    if canonical_voice.startswith("nova-sonic"):
        return NovaSonicProvider(), canonical_voice.split(":", 1)[-1]
    if canonical_voice.startswith("edge:"):
        return None, canonical_voice.split(":", 1)[1]
    return None, canonical_voice  # edge fallback


def list_all_voices() -> list[dict]:
    """Switcher payload — Polly first (HD), then the edge fallbacks."""
    out = []
    try:
        p = resolve_provider(DEFAULT_VOICE)[0]
        out.extend({
            "id": v.id, "label": v.label, "provider": v.provider,
            "description": v.description, "style": v.style, "hd": True,
        } for v in p.voices())
    except Exception:                                    # noqa: BLE001
        log.warning("polly voices unavailable (boto3 missing?)", exc_info=True)
    out.extend([
        {"id": "edge:Aria", "label": "Aria", "provider": "edge",
         "description": "fallback · no AWS creds needed", "style": "warm",
         "hd": False},
        {"id": "edge:Guy", "label": "Guy", "provider": "edge",
         "description": "fallback · upbeat", "style": "upbeat", "hd": False},
    ])
    return out


__all__ = [
    "VoiceInfo", "VoiceProvider", "PollyProvider", "NovaSonicProvider",
    "POLLY_VOICES", "DEFAULT_VOICE", "normalize_voice", "resolve_provider",
    "list_all_voices",
]
