"""STT provider — AWS Transcribe primary, whisper fallback, per directive.

* resolve_backend honours CAPTURD_STT_BACKEND and the credential chain
* AwsTranscribeSTT streams PCM16 chunks and joins final transcripts
  (partials skipped)
* VoiceLoop._transcribe: backend=aws hard-fails on AWS errors (no silent
  downgrade when the operator asked for the account); auto falls back to
  whisper
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capturd.walk import stt  # noqa: E402
from capturd.walk import voice as voice_mod  # noqa: E402
from capturd.walk.stt import AwsTranscribeSTT, resolve_backend  # noqa: E402


class _FakeAudioStream:
    def __init__(self, log):
        self.log = log

    def send_audio_event(self, event):
        self.log.append(event["AudioChunk"])

    def flush(self):
        self.log.append(b"FLUSH")


class _FakeClient:
    def __init__(self, finals, partials=None):
        self.finals = finals or []
        self.partials = partials or []
        self.chunks = []

    def start_stream_transcription(self, **kwargs):
        self.cfg = kwargs
        stream = _FakeAudioStream(self.chunks)

        events = []
        for p in self.partials:
            events.append({"TranscriptEvent": {"Transcript": {"Results": [p]}}})
        for f in self.finals:
            events.append({"TranscriptEvent": {"Transcript": {"Results": [f]}}})

        class _Iter:
            def __iter__(self_inner):
                return iter(events)

        resp = {"AudioStream": stream, "TranscriptResultStream": _Iter()}
        return resp


@pytest.fixture
def fake_aws(monkeypatch):
    log = []

    class _Holder:
        client = None
        log = None

    holder = _Holder()
    holder.log = log

    def fake_client_factory(self):
        return holder.client

    monkeypatch.setattr(AwsTranscribeSTT, "_transcribe_client", fake_client_factory)
    return holder


def _final(text, is_partial=False):
    return {"IsPartial": is_partial,
            "Alternatives": [{"Transcript": text}]}


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


def test_resolve_backend_explicit_whisper(monkeypatch):
    monkeypatch.setenv("CAPTURD_STT_BACKEND", "whisper")
    assert resolve_backend() == "whisper"


def test_resolve_backend_auto_without_creds(monkeypatch):
    import boto3
    monkeypatch.setattr(boto3.session.Session, "get_credentials",
                        lambda self: None, raising=True)
    monkeypatch.delenv("CAPTURD_STT_BACKEND", raising=False)
    assert resolve_backend() == "whisper"


def test_resolve_backend_aws_insisted_even_without_creds(monkeypatch):
    import boto3
    monkeypatch.setenv("CAPTURD_STT_BACKEND", "aws")
    monkeypatch.setattr(boto3.session.Session, "get_credentials",
                        lambda self: None, raising=True)
    assert resolve_backend() == "aws"


# ---------------------------------------------------------------------------
# AwsTranscribeSTT
# ---------------------------------------------------------------------------


def test_transcribe_streams_pcm_and_joins_finals(fake_aws):
    fake_aws.client = _FakeClient(
        finals=[_final("Hello"), _final("world")],
        partials=[_final("Hel", is_partial=True)],
    )
    stt = AwsTranscribeSTT(region="us-east-1")
    audio = np.randint = np.arange(0, 16000, dtype=np.int16)  # 1s of audio
    out = stt.transcribe_pcm16(audio)
    assert out == "Hello world", "partials must be skipped, finals joined"
    assert fake_aws.client.cfg["MediaEncoding"] == "pcm"
    assert fake_aws.client.cfg["MediaSampleRateHertz"] == 16000
    assert fake_aws.client.cfg["LanguageCode"] == "en-US"
    # PCM16 little-endian bytes streamed in bounded chunks
    pcm = audio.astype("<i2").tobytes()
    sent = b"".join(c for c in fake_aws.client.chunks if c != b"FLUSH")
    assert sent == pcm
    assert all(len(c) <= 16384 for c in fake_aws.client.chunks)
    assert b"FLUSH" in fake_aws.client.chunks


def test_transcribe_empty_audio_short_circuits(fake_aws):
    fake_aws.client = _FakeClient(finals=[_final("nope")])
    stt = AwsTranscribeSTT()
    assert stt.transcribe_pcm16(np.zeros(0, dtype=np.int16)) == ""
    assert not fake_aws.client.chunks


# ---------------------------------------------------------------------------
# VoiceLoop wiring
# ---------------------------------------------------------------------------


def test_voiceloop_aws_backend_returns_aws_text(fake_aws, monkeypatch):
    fake_aws.client = _FakeClient(finals=[_final("clicked the house button")])
    loop = voice_mod.VoiceLoop(voice_mod.VoiceConfig(model="tiny.en"))
    monkeypatch.setenv("CAPTURD_STT_BACKEND", "aws")
    audio = np.zeros(32000, dtype=np.int16)          # 2s
    out = loop._transcribe(audio)
    assert out == "clicked the house button"


def test_voiceloop_aws_backend_hard_fails_on_error(fake_aws, monkeypatch):
    fake_aws.client = _FakeClient(finals=[_final("x")])

    def boom(**kwargs):
        raise RuntimeError("credentials expired")

    fake_aws.client.start_stream_transcription = boom
    loop = voice_mod.VoiceLoop(voice_mod.VoiceConfig(model="tiny.en"))
    monkeypatch.setenv("CAPTURD_STT_BACKEND", "aws")
    audio = np.zeros(16000, dtype=np.int16)
    with pytest.raises(voice_mod.VoiceLoopError):
        loop._transcribe(audio)


def test_voiceloop_auto_falls_back_to_whisper_on_aws_error(fake_aws, monkeypatch):
    fake_aws.client = _FakeClient(finals=[_final("x")])

    def boom(**kwargs):
        raise RuntimeError("network down")

    fake_aws.client.start_stream_transcription = boom
    loop = voice_mod.VoiceLoop(voice_mod.VoiceConfig(model="tiny.en"))

    class _FakeWhisper:
        def transcribe(self, audio_float, beam_size, vad_filter):
            class _Seg:
                text = "  local whisper text  "
            return [_Seg()], None

    monkeypatch.setattr(loop, "_load_model", lambda: None)
    monkeypatch.setattr(loop, "_model", _FakeWhisper())
    monkeypatch.delenv("CAPTURD_STT_BACKEND", raising=False)
    out = loop._transcribe(np.zeros(16000, dtype=np.int16))
    assert out == "local whisper text", "auto must fall back to whisper"
