"""VoiceProvider layer — Bedrock-era voice wiring (Vertex legacy removed).

Covers: legacy voice-name aliases (old demos keep regenerating), the Polly
provider (mocked boto3 — word speech-marks in, voiceoverWords out), the Nova
Sonic seam (honest refusal, no fake), registry resolution, the MCP voice.*
tools, and the ai_pipeline cutover (polly path + edge fallback preserved).
"""

from __future__ import annotations

import json

import pytest

from capturd.walk import voice_provider as vp


# ---------------------------------------------------------------------------
# Voice-name normalization / aliases
# ---------------------------------------------------------------------------


def test_legacy_vertex_names_are_aliased_to_polly():
    assert vp.normalize_voice("vertex:Charon:warm") == "polly:Matthew"
    assert vp.normalize_voice("vertex:Kore:warm") == "polly:Danielle"
    assert vp.normalize_voice("vertex:Aoede:warm") == "polly:Olivia"
    assert vp.normalize_voice("vertex:Fenrir:trailer") == "polly:Matthew"
    assert vp.normalize_voice("vertex:Zephyr:warm") == "polly:Kevin"
    # Bare legacy names too (old demo specs store these).
    assert vp.normalize_voice("Charon") == "polly:Matthew"
    assert vp.normalize_voice("Kore") == "polly:Danielle"


def test_canonical_and_edge_names_pass_through():
    assert vp.normalize_voice("polly:Ruth") == "polly:Ruth"
    assert vp.normalize_voice("edge:Aria") == "edge:Aria"
    assert vp.normalize_voice("en-US-AriaNeural") == "en-US-AriaNeural"
    assert vp.normalize_voice("Ruth") == "polly:Ruth"
    assert vp.normalize_voice("") == vp.DEFAULT_VOICE
    assert vp.normalize_voice(None) == vp.DEFAULT_VOICE


def test_list_all_voices_has_polly_hd_and_edge_fallback():
    voices = vp.list_all_voices()
    ids = [v["id"] for v in voices]
    assert "polly:Ruth" in ids and "polly:Matthew" in ids
    assert "edge:Aria" in ids
    polly = [v for v in voices if v["provider"] == "polly"]
    assert all(v["hd"] for v in polly)
    edge = [v for v in voices if v["provider"] == "edge"]
    assert all(not v["hd"] for v in edge)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_resolve_provider_registry():
    provider, vid = vp.resolve_provider("polly:Ruth")
    assert isinstance(provider, vp.PollyProvider) and vid == "Ruth"
    provider, vid = vp.resolve_provider("edge:Aria")
    assert provider is None and vid == "Aria"      # caller uses the edge path
    provider, vid = vp.resolve_provider("nova-sonic:default")
    assert isinstance(provider, vp.NovaSonicProvider)


def test_nova_sonic_seam_refuses_honestly():
    p = vp.NovaSonicProvider()
    assert p.voices() == []
    with pytest.raises(RuntimeError, match="not wired"):
        p.synthesize("hello", "default")


# ---------------------------------------------------------------------------
# Polly provider (mocked boto3)
# ---------------------------------------------------------------------------


class _FakeAudioStream:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _FakePolly:
    """Captures calls; returns mp3-ish audio + word speech-marks JSON lines."""

    def __init__(self):
        self.calls = []

    def synthesize_speech(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("OutputFormat") == "mp3":
            return {"AudioStream": _FakeAudioStream(b"ID3fake-mp3-audio")}
        marks = [
            {"type": "word", "time": 0, "duration": 50, "value": "Your"},
            {"type": "word", "time": 500, "duration": 42, "value": "product"},
            {"type": "sentence", "time": 0, "duration": 900, "value": "Your product"},
            "not-json",
        ]
        return {"AudioStream": _FakeAudioStream(
            "\n".join(json.dumps(m) for m in marks).encode())}


@pytest.fixture
def polly(monkeypatch):
    provider = vp.PollyProvider(region="us-east-1")
    fake = _FakePolly()
    monkeypatch.setattr(provider, "_polly", lambda: fake)
    return provider, fake


def test_polly_synthesize_returns_audio_and_word_timings(polly):
    provider, fake = polly
    audio, words = provider.synthesize("Your product", "polly:Ruth")
    assert audio == b"ID3fake-mp3-audio"
    assert words == [
        {"word": "Your", "tStartMs": 0, "tEndMs": 50},
        {"word": "product", "tStartMs": 500, "tEndMs": 542},
    ]
    engines = {c["Engine"] for c in fake.calls}
    assert engines == {"neural"}
    assert any(c.get("SpeechMarkTypes") == ["word"] for c in fake.calls)


def test_polly_uses_region_config(monkeypatch):
    monkeypatch.setenv("CAPTURD_AWS_REGION", "eu-central-1")
    p = vp.PollyProvider()
    assert p.region == "eu-central-1"


# ---------------------------------------------------------------------------
# ai_pipeline cutover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_routes_polly_voices_through_provider(monkeypatch):
    """A polly voice name must hit the provider, not the edge path; a legacy
    Vertex name must alias into the same provider (old demos keep working)."""
    import capturd.walk.ai_pipeline as ai

    seen = []

    class FakeProvider:
        id = "polly"

        def synthesize(self, text, voice):
            seen.append((text, voice))
            return b"mp3-bytes", [{"word": "hi", "tStartMs": 0, "tEndMs": 40}]

    monkeypatch.setattr(vp, "resolve_provider",
                        lambda v: (FakeProvider(), v.split(":", 1)[1]))
    monkeypatch.setattr(vp, "normalize_voice", vp.normalize_voice)

    audio, words = await ai._synthesize_one("hi there", "polly:Ruth")
    assert audio == b"mp3-bytes" and words[0].word == "hi"
    assert seen and seen[0][1] == "Ruth"

    seen.clear()
    audio, words = await ai._synthesize_one("hi there", "vertex:Kore:warm")
    assert seen and seen[0][1] == "Danielle", "legacy Vertex name must alias"


# ---------------------------------------------------------------------------
# MCP voice.* tools
# ---------------------------------------------------------------------------


def _tools():
    import asyncio
    from capturd.mcp.server import _build_server
    srv = _build_server()
    return srv, {t.name: t for t in asyncio.run(srv.list_tools())}


def test_mcp_voice_tools_registered():
    _, tools = _tools()
    assert {"voice.list", "voice.preview", "voice.synthesize"} <= set(tools)


@pytest.mark.asyncio
async def test_mcp_voice_list_works():
    from capturd.mcp.server import _build_server
    srv = _build_server()
    tools = {t.name: t for t in await srv.list_tools()}
    fn = getattr(tools["voice.list"], "fn", tools["voice.list"])
    out = await fn()
    assert out["ok"] is True
    assert any(v["id"] == "polly:Ruth" for v in out["voices"])
    assert out["default"] == "polly:Ruth"
