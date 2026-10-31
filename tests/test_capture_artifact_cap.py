"""W3-06f-r1 — the artifact-cap funnel maps its sibling filesystem error.

F106 (`capturd/walk/exporter.py`): a file that vanishes between the output
existence check and the size read must come out of ``export_video`` as the
module's typed refusal (``DemoExportError``), never a bare ``OSError`` — and
the completion log must carry the size that was actually read.

F107 (`capturd/shots/capture.py`): the one still funnel maps an ``OSError``
from the cap read the way it maps ``ArtifactTooLarge`` — unlink the partial
still, then raise ``RestedCaptureError`` — so the sweep records a real
failure for the state and ``_capture_one`` never retries a vanished file.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from capturd import engine
from capturd.shots import capture
from capturd.shots.capture import build_capture_plan
from capturd.walk import exporter

# One DNS stub for the capture-path tests (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("_offline_test_dns")


# ---------------------------------------------------------------------------
# F106 — capturd/walk/exporter.py
# ---------------------------------------------------------------------------

def _run_export(tmp_path, monkeypatch, *, output: bytes | None):
    """Drive the real export_video size step with capture and encoding faked."""
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")

    async def fake_capture_frames(viewer_html, viewport, frames_dir, fps):
        return 2, 1.0, []

    def fake_encode_mp4(ffmpeg, frames_dir, fps, duration_s, out_path, audio_clips):
        if output is None:
            out_path.mkdir()  # a directory, not a video
        else:
            out_path.write_bytes(output)

    monkeypatch.setattr(exporter, "_capture_frames", fake_capture_frames)
    monkeypatch.setattr(exporter, "_encode_mp4", fake_encode_mp4)

    viewer = tmp_path / "viewer.html"
    viewer.write_text("<html>demo</html>", encoding="utf-8")
    spec = {"steps": [{"kind": "scroll"}], "viewport": {"width": 16, "height": 16}}
    return exporter.export_video(spec, viewer, tmp_path / "demo.mp4", fmt="mp4")


def _vanish_after_existence_check(monkeypatch, out: Path) -> None:
    """Delete the output the moment ``is_file()`` confirms it — the F106 race."""
    real_is_file = Path.is_file
    state = {"vanished": False}

    def is_file_then_vanish(self: Path) -> bool:
        exists = real_is_file(self)
        if self == out and exists and not state["vanished"]:
            state["vanished"] = True
            self.unlink()
        return exists

    monkeypatch.setattr(Path, "is_file", is_file_then_vanish)


def test_export_maps_a_vanishing_output_file(tmp_path, monkeypatch):
    out = tmp_path / "demo.mp4"
    _vanish_after_existence_check(monkeypatch, out)
    with pytest.raises(exporter.DemoExportError) as exc:
        _run_export(tmp_path, monkeypatch, output=b"v" * 8)
    assert type(exc.value) is exporter.DemoExportError
    assert not isinstance(exc.value, OSError), "a filesystem failure never escapes raw"
    assert "demo.mp4" in str(exc.value)


def test_export_logs_the_size_it_read(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(exporter, "check_artifact_size", lambda path: 4096)
    with caplog.at_level(logging.INFO, logger="capturd.walk.exporter"):
        out = _run_export(tmp_path, monkeypatch, output=b"v" * 8)
    assert out.is_file()
    completions = [r.getMessage() for r in caplog.records if "export complete" in r.getMessage()]
    assert len(completions) == 1, "the completion log is written exactly once"
    assert "4.0 KB" in completions[0], "the log carries the size the reader returned"


def test_export_directory_output_is_a_typed_refusal(tmp_path, monkeypatch):
    with pytest.raises(exporter.DemoExportError) as exc:
        _run_export(tmp_path, monkeypatch, output=None)
    assert type(exc.value) is exporter.DemoExportError
    assert not isinstance(exc.value, OSError)
    assert "demo.mp4" in str(exc.value)


def test_export_zero_byte_output_still_refuses(tmp_path, monkeypatch):
    with pytest.raises(exporter.DemoExportError) as exc:
        _run_export(tmp_path, monkeypatch, output=b"")
    assert "produced no output" in str(exc.value)


# ---------------------------------------------------------------------------
# F107 — capturd/shots/capture.py
# ---------------------------------------------------------------------------

def _vanish_before_the_size_read(still):
    """The F107 race: the file is gone by the time the cap reads its size."""
    Path(still).unlink()
    return engine.check_artifact_size(still)  # the real read raises FileNotFoundError


def _capture_one_page(tmp_path, monkeypatch, *, retries: int = 0):
    """Mock the browser only; drive the real _capture_one."""
    _, targets, settings = build_capture_plan(
        {"urls": ["https://example.com/"], "viewports": ["desktop"],
         "schemes": ["light"], "retries": retries},
    )
    target = dataclasses.replace(targets[0], trigger_selector="")
    page = AsyncMock()
    page.url = "https://example.com/"
    page.set_default_timeout = Mock()
    written: list[Path] = []

    async def screenshot(**kwargs):
        path = Path(kwargs["path"])
        path.write_bytes(b"x" * 8)
        written.append(path)

    page.screenshot.side_effect = screenshot
    monkeypatch.setattr(capture, "_settle_page", AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    return context, target, settings, tmp_path / "baseline.png", page, written


def _sweep_page(monkeypatch):
    """Mock the browser only; drive the real _sweep_states."""
    page = AsyncMock()
    page.url = "https://example.com/"
    fingerprint = ["baseline"]
    element = SimpleNamespace(count=AsyncMock(return_value=1),
                              is_visible=AsyncMock(return_value=True))

    async def click(**kwargs):
        fingerprint[0] = "opened"

    element.click = AsyncMock(side_effect=click)
    page.locator = Mock(return_value=SimpleNamespace(first=element))

    async def evaluate(script):
        if script == capture._MAP_CONTROLS_JS:
            return [{"id": "0", "label": "Open menu", "tag": "button"}]
        return fingerprint[0]

    page.evaluate.side_effect = evaluate

    async def press(key):
        fingerprint[0] = "baseline"

    page.keyboard.press.side_effect = press

    async def screenshot(**kwargs):
        Path(kwargs["path"]).write_bytes(b"x" * 8)

    page.screenshot.side_effect = screenshot
    monkeypatch.setattr(capture, "_dismiss_overlays", AsyncMock())
    return page


def test_write_still_maps_a_vanishing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "check_artifact_size", _vanish_before_the_size_read)
    page = AsyncMock()

    async def screenshot(**kwargs):
        Path(kwargs["path"]).write_bytes(b"x" * 8)

    page.screenshot.side_effect = screenshot
    path = tmp_path / "vanishing.png"
    with pytest.raises(capture.RestedCaptureError) as exc:
        asyncio.run(capture._write_still(page, path, type="png"))
    assert type(exc.value) is capture.RestedCaptureError
    assert not isinstance(exc.value, OSError)
    assert str(path) in str(exc.value)
    assert not path.exists(), "the unreadable still is not left on disk"


def test_the_sweep_records_a_failure_rather_than_skipping(tmp_path, monkeypatch):
    page = _sweep_page(monkeypatch)
    monkeypatch.setattr(capture, "check_artifact_size", _vanish_before_the_size_read)
    _, targets, settings = build_capture_plan(
        {"urls": ["https://example.com/"], "viewports": ["desktop"],
         "schemes": ["light"], "states": True},
    )
    target = dataclasses.replace(targets[0], trigger_selector="")
    shots = asyncio.run(
        capture._sweep_states(page, target, settings, tmp_path / "baseline.png"))
    assert len(shots) == 1, "a vanished state is recorded, not skipped"
    state = shots[0]
    assert state["status"] == "failed"
    assert state["file"] is None
    assert "unreadable" in state["error"]
    assert not [p for p in tmp_path.rglob("*") if p.is_file()], "nothing stays on disk"


def test_capture_one_does_not_retry_a_vanished_file(tmp_path, monkeypatch):
    context, target, settings, path, page, written = _capture_one_page(
        tmp_path, monkeypatch, retries=2)
    monkeypatch.setattr(capture, "check_artifact_size", _vanish_before_the_size_read)
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    assert result["status"] == "failed"
    assert result["attempts"] == 1, "a vanished file cannot come back on a retry"
    assert page.goto.await_count == 1, "exactly one navigation"
    assert len(written) == 1, "exactly one write attempt"
    assert not [p for p in tmp_path.rglob("*") if p.is_file()], "nothing stays on disk"


def test_the_cap_still_bites(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", 10)
    page = AsyncMock()

    async def screenshot(**kwargs):
        Path(kwargs["path"]).write_bytes(b"x" * 24)

    page.screenshot.side_effect = screenshot
    path = tmp_path / "big.png"
    with pytest.raises(engine.ArtifactTooLarge) as exc:
        asyncio.run(capture._write_still(page, path, type="png"))
    assert exc.value.size == 24 and exc.value.cap == 10
    assert not path.exists(), "the over-cap still is still unlinked"
    assert [p.name for p in tmp_path.iterdir()] == [], "no other file was touched"


def test_a_screenshot_oserror_is_not_remapped_or_unlinked(tmp_path, monkeypatch):
    """Edge: the screenshot itself fails — no file existed, so nothing is unlinked."""
    calls: list[Path] = []
    monkeypatch.setattr(capture, "check_artifact_size", lambda path: calls.append(path))
    page = AsyncMock()

    async def screenshot(**kwargs):
        raise OSError("disk full")

    page.screenshot.side_effect = screenshot
    path = tmp_path / "never.png"
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(capture._write_still(page, path, type="png"))
    assert calls == [], "the cap check ran for a still that was never written"
    assert not path.exists()
