"""F99 — the coordinator maps the over-cap export refusal with ``size`` and ``cap``.

The exporter raises ``DemoExportError`` carrying ``size`` and ``cap``;
``DemoForge.export_demo`` is the boundary every job caller (MCP tool, HTTP
route, CLI) goes through, so that typed refusal must survive it. If the
handler ever moved behind a broader ``except Exception``, an over-cap export
would become a generic job error and nothing would notice — this file pins
the consumer side.

No browser, network or ffmpeg is touched: frame capture and the encoder are
monkeypatched, and only the real size check runs.
"""

from __future__ import annotations

import base64

import pytest

from capturd import engine
from capturd.walk import exporter
from capturd.walk.coordinator import DemoForge, DemoForgeError


def _step() -> dict:
    return {
        "index": 0,
        "timestamp": 0,
        "pageUrl": "https://example.com",
        "pageTitle": "t",
        "interaction": {
            "type": "click",
            "target": {"selector": "body", "tagName": "", "text": "down",
                       "boundingRect": {"x": 0, "y": 0, "width": 16, "height": 16}},
            "hotspot": {"xPct": 50, "yPct": 50},
        },
        "annotation": "Step 0.",
        "screenshotBase64": base64.b64encode(b"png-bytes").decode("ascii"),
        "screenshotPath": None,
    }


def _spec() -> dict:
    return {
        "version": 1,
        "id": "capped",
        "name": "capped",
        "goal": "capped",
        "createdAt": "2026-10-29T00:00:00Z",
        "viewport": {"width": 16, "height": 16},
        "startUrl": "https://example.com",
        "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
        "steps": [_step()],
    }


def _forge_with_capped_encoder(
    tmp_path, monkeypatch, *, artifact_bytes: int, cap: int,
) -> DemoForge:
    """A real DemoForge whose (faked) encoder writes ``artifact_bytes``."""
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", cap)
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")

    async def fake_capture_frames(viewer_html, viewport, frames_dir, fps):
        return 2, 1.0, []

    def fake_encode_mp4(ffmpeg, frames_dir, fps, duration_s, out_path, audio_clips):
        out_path.write_bytes(b"v" * artifact_bytes)

    monkeypatch.setattr(exporter, "_capture_frames", fake_capture_frames)
    monkeypatch.setattr(exporter, "_encode_mp4", fake_encode_mp4)

    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("capped", _spec())
    return forge


def test_the_coordinator_maps_an_over_cap_export_with_size_and_cap(tmp_path, monkeypatch):
    """The cap refusal reaching a job caller keeps its size and cap."""
    forge = _forge_with_capped_encoder(tmp_path, monkeypatch, artifact_bytes=24, cap=10)
    with pytest.raises(exporter.DemoExportError) as exc:
        forge.export_demo("capped", fmt="mp4")
    err = exc.value
    assert isinstance(err, DemoForgeError), "job callers catch DemoForgeError"
    assert type(err) is exporter.DemoExportError, (
        "the mapped error must stay the exporter's DemoExportError, not a re-typed generic one")
    assert err.size == 24, "the mapped error must carry the measured size"
    assert err.cap == 10, "the mapped error must carry the cap"
    assert "24" in str(err) and "10" in str(err)


def test_a_non_cap_export_failure_still_maps_to_a_generic_job_error(tmp_path, monkeypatch):
    """Edge 2: a failure that is not the cap stays DemoForgeError, without cap metadata."""
    def no_ffmpeg() -> str:
        raise exporter.DemoExportError("ffmpeg not found")

    monkeypatch.setattr(exporter, "find_ffmpeg", no_ffmpeg)
    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("capped", _spec())
    with pytest.raises(exporter.DemoExportError) as exc:
        forge.export_demo("capped", fmt="mp4")
    err = exc.value
    assert isinstance(err, DemoForgeError)
    assert err.size is None and err.cap is None
