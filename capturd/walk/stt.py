"""STT provider — Amazon Transcribe (the Nova/AWS account) as PRIMARY.

Directive: audio transcription runs on the AWS account, not on local fallback
models. The local faster-whisper path stays as the zero-config FALLBACK
(``capturd[voice]`` extras), selected automatically when no AWS credentials
are resolvable — so the program always works, but the account does the heavy
lifting wherever it can.

Backend selection (``CAPTURD_STT_BACKEND``):

* ``auto`` (default) — AWS when boto3 + credentials are resolvable, else
  whisper.
* ``aws``            — hard-require AWS; a failure raises (no silent
  downgrade when the operator explicitly asked for the account).
* ``whisper``        — force the local fallback.

Speech: PCM16 mono 16 kHz over ``start_stream_transcription`` (no S3 needed —
the push-to-talk buffer streams straight up). Language: ``en-US`` unless
``CAPTURD_STT_LANGUAGE`` says otherwise.
"""
from __future__ import annotations

import logging
import os

import numpy as np

log = logging.getLogger("capturd.stt")

_CHUNK_BYTES = 16384


class TranscribeUnavailable(RuntimeError):
    """Raised when AWS is explicitly required but unusable."""


class AwsTranscribeSTT:
    """Amazon Transcribe streaming STT. PCM16 mono in, transcript text out."""

    id = "aws-transcribe"

    def __init__(self, region: str | None = None,
                 language: str | None = None) -> None:
        self.region = (region or os.environ.get("CAPTURD_AWS_REGION")
                       or os.environ.get("AWS_REGION") or "us-east-1")
        self.language = (language or os.environ.get("CAPTURD_STT_LANGUAGE")
                         or "en-US")
        self._client = None

    def _transcribe_client(self):
        if self._client is None:
            import boto3  # lazy — optional dependency
            self._client = boto3.client("transcribe", region_name=self.region)
        return self._client

    @staticmethod
    def available() -> bool:
        """True when boto3 is importable AND the default chain has creds."""
        try:
            import boto3
            session = boto3.session.Session()
            return session.get_credentials() is not None
        except Exception:                                # noqa: BLE001
            return False

    def transcribe_pcm16(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe an int16 mono numpy array. Returns the final transcript.

        Raises TranscribeUnavailable/ServiceError-shaped exceptions to the
        caller, which decides between hard-fail (backend=aws) and fallback.
        """
        client = self._transcribe_client()
        pcm = np.ascontiguousarray(audio.astype("<i2")).tobytes()
        if not pcm:
            return ""

        resp = client.start_stream_transcription(
            LanguageCode=self.language,
            MediaEncoding="pcm",
            MediaSampleRateHertz=int(sample_rate),
        )
        audio_stream = resp["AudioStream"]

        def _feed() -> None:
            for i in range(0, len(pcm), _CHUNK_BYTES):
                audio_stream.send_audio_event(
                    {"AudioChunk": pcm[i:i + _CHUNK_BYTES]})
            audio_stream.flush()

        import threading
        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()

        final: list[str] = []
        for event in resp["TranscriptResultStream"]:
            if "TranscriptEvent" not in event:
                continue
            results = (event["TranscriptEvent"].get("Transcript") or {}).get("Results") or []
            for res in results:
                if res.get("IsPartial"):
                    continue                      # finals only
                for alt in res.get("Alternatives") or []:
                    text = (alt.get("Transcript") or "").strip()
                    if text:
                        final.append(text)
        feeder.join(timeout=5)
        return " ".join(final).strip()


def resolve_backend(explicit: str | None = None) -> str:
    """Return 'aws' or 'whisper' per CAPTURD_STT_BACKEND / credential state."""
    backend = (explicit or os.environ.get("CAPTURD_STT_BACKEND")
               or "auto").strip().lower()
    if backend in ("aws", "auto"):
        from capturd.walk.voice_provider import PollyProvider  # creds probe reuse
        _ = PollyProvider  # same boto3 chain; check creds the cheap way
        try:
            import boto3
            if boto3.session.Session().get_credentials() is not None:
                return "aws"
        except Exception:                                # noqa: BLE001
            pass
        if backend == "aws":
            return "aws"                                 # operator insisted
        return "whisper"
    return backend if backend in ("aws", "whisper") else "whisper"


__all__ = ["AwsTranscribeSTT", "TranscribeUnavailable", "resolve_backend"]
