"""W3-06a — the engine manifest is packaged, truthful, and the cap is real.

Everything here is driven by the briefs (W3-06a-r1/r2/r3): the packaged
manifest carries every ADOPTION.md field, `check_artifact_size` enforces
`MAX_ARTIFACT_BYTES` on the exporter's size step and on the one still funnel
in `capturd/shots/capture.py`, and no `page.screenshot` call lives outside
that funnel. Local stills run with no account, as today.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.resources
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from capturd import engine
from capturd.shots import capture
from capturd.shots.capture import build_capture_plan
from capturd.walk import exporter

PORT_TYPES = {"text", "image", "video", "audio", "model3d", "diagram", "artifact", "json", "number"}
NAMED_LIMITS = {"maxInputBytes", "maxArtifactBytes", "maxActiveJobsPerAccount", "maxJobDurationMs"}

# One DNS stub for the capture-path tests (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("_offline_test_dns")


# ---------------------------------------------------------------------------
# The manifest itself (r1 tests 1-5, r1 edge 3-4, r2 test 9)
# ---------------------------------------------------------------------------

def test_load_manifest_returns_every_adoption_field():
    manifest = engine.load_manifest()
    for field in (
        "schemaVersion", "contractVersion", "engine", "engineVersion", "displayName",
        "inputSchema", "outputSchema", "artifactKinds", "mark", "capabilities",
        "limits", "execution",
    ):
        assert field in manifest, f"missing ADOPTION.md field: {field}"
    assert manifest["schemaVersion"] == 1
    assert manifest["contractVersion"] == "1.0"
    assert isinstance(manifest["inputSchema"], object) and isinstance(manifest["outputSchema"], object)


def test_engine_identity_and_port_types_are_truthful():
    manifest = engine.load_manifest()
    assert manifest["engine"] == "capturd"
    assert manifest["mark"] == "capturd"          # an L3 id, never a URL
    assert "://" not in manifest["mark"]
    assert manifest["capabilities"], "a manifest with no capabilities is not deployable"
    for capability in manifest["capabilities"]:
        for port in capability["inputPorts"] + capability["outputPorts"]:
            assert port["type"] in PORT_TYPES, f"untyped port: {port}"
            assert port["name"] and isinstance(port["required"], bool)


def test_every_capability_declares_boolean_cancellable_and_loops_allowed():
    manifest = engine.load_manifest()
    ids = set()
    for capability in manifest["capabilities"]:
        assert isinstance(capability["cancellable"], bool)
        assert isinstance(capability["loopsAllowed"], bool)
        assert capability["id"] not in ids, "duplicate capability id"
        ids.add(capability["id"])
        assert capability["summary"] and capability["version"]
        assert isinstance(capability["params"], dict)


def test_every_limit_is_a_positive_number():
    limits = engine.load_manifest()["limits"]
    assert NAMED_LIMITS <= set(limits), "a named limit is missing"
    for key, value in limits.items():
        assert isinstance(value, int) and value > 0, f"limit {key} must be a positive number"


def test_max_artifact_bytes_limit_equals_the_code_constant():
    assert engine.load_manifest()["limits"]["maxArtifactBytes"] == engine.MAX_ARTIFACT_BYTES


def test_manifest_found_from_installed_package_not_only_source_tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # cwd independence: the file ships with the package
    packaged = importlib.resources.files("capturd.engine").joinpath("manifest.json")
    assert packaged.is_file()
    assert engine.load_manifest()["engine"] == "capturd"


def test_load_manifest_twice_is_equal_and_mutation_isolated():
    first = engine.load_manifest()
    second = engine.load_manifest()
    assert first == second
    first["capabilities"].clear()
    first["limits"]["maxArtifactBytes"] = 0
    assert engine.load_manifest() == second, "a caller's copy leaked into the next load"


def test_corrupt_packaged_manifest_raises_a_named_error(tmp_path, monkeypatch):
    bad = tmp_path / "manifest.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(engine, "_MANIFEST_PATH", bad)
    with pytest.raises(engine.ManifestError):
        engine.load_manifest()
    broken = tmp_path / "list.json"
    broken.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(engine, "_MANIFEST_PATH", broken)
    with pytest.raises(engine.ManifestError):
        engine.load_manifest()


# ---------------------------------------------------------------------------
# check_artifact_size (r2 test 6, r2 edge 1-2)
# ---------------------------------------------------------------------------

def test_check_artifact_size_at_cap_passes_one_byte_over_raises(tmp_path):
    at_cap = tmp_path / "at-cap.bin"
    with open(at_cap, "wb") as fh:
        fh.seek(engine.MAX_ARTIFACT_BYTES - 1)
        fh.write(b"\0")
    assert engine.check_artifact_size(at_cap) == engine.MAX_ARTIFACT_BYTES

    over = tmp_path / "one-over.bin"
    with open(over, "wb") as fh:
        fh.seek(engine.MAX_ARTIFACT_BYTES)
        fh.write(b"\0")
    assert over.stat().st_size == engine.MAX_ARTIFACT_BYTES + 1
    with pytest.raises(engine.ArtifactTooLarge) as exc:
        engine.check_artifact_size(over)
    # the error names its size and the cap, and the file stays on disk
    assert str(engine.MAX_ARTIFACT_BYTES + 1) in str(exc.value)
    assert str(engine.MAX_ARTIFACT_BYTES) in str(exc.value)
    assert over.exists()


def test_check_artifact_size_missing_file_never_passes(tmp_path):
    with pytest.raises(OSError):
        engine.check_artifact_size(tmp_path / "missing.bin")


# ---------------------------------------------------------------------------
# The exporter's size step (r2 change 2, r2 test 7)
# ---------------------------------------------------------------------------

def _export_over_env(tmp_path, monkeypatch, *, artifact_bytes, cap):
    """Drive the real export_video size step with the encoding faked away."""
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", cap)
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")

    async def fake_capture_frames(viewer_html, viewport, frames_dir, fps):
        return 2, 1.0, []

    def fake_encode_mp4(ffmpeg, frames_dir, fps, duration_s, out_path, audio_clips):
        out_path.write_bytes(b"v" * artifact_bytes)

    monkeypatch.setattr(exporter, "_capture_frames", fake_capture_frames)
    monkeypatch.setattr(exporter, "_encode_mp4", fake_encode_mp4)

    viewer = tmp_path / "viewer.html"
    viewer.write_text("<html>demo</html>", encoding="utf-8")
    out = tmp_path / "demo.mp4"
    spec = {"steps": [{"kind": "scroll"}], "viewport": {"width": 16, "height": 16}}
    return exporter.export_video(spec, viewer, out, fmt="mp4")


def test_export_over_the_cap_raises_from_the_size_step(tmp_path, monkeypatch):
    with pytest.raises(engine.ArtifactTooLarge) as exc:
        _export_over_env(tmp_path, monkeypatch, artifact_bytes=24, cap=10)
    assert "10" in str(exc.value) and "24" in str(exc.value)
    assert (tmp_path / "demo.mp4").exists(), "the too-large export stays on disk"


def test_export_under_the_cap_still_succeeds(tmp_path, monkeypatch):
    out = _export_over_env(tmp_path, monkeypatch, artifact_bytes=8, cap=10)
    assert out.is_file() and out.stat().st_size == 8


# ---------------------------------------------------------------------------
# The one still funnel (r2 change 3 as ruled in r3, r2 test 8, r3 edge 1-3)
# ---------------------------------------------------------------------------

def _capture_stack(tmp_path, monkeypatch, *, states=False, screenshot_sizes=(24,)):
    """Mock browser I/O only. Drive the real _capture_one AND _sweep_states."""
    _, targets, settings = build_capture_plan(
        {"urls": ["https://example.com/"], "states": states, "retries": 0,
         "viewports": ["desktop"], "schemes": ["light"]},
    )
    target = dataclasses.replace(targets[0], trigger_selector="")
    page = AsyncMock()
    page.url = "https://example.com/"
    page.set_default_timeout = Mock()
    written: list[Path] = []

    async def screenshot(**kwargs):
        size = screenshot_sizes[min(len(written), len(screenshot_sizes) - 1)]
        path = Path(kwargs["path"])
        path.write_bytes(b"x" * size)
        written.append(path)

    page.screenshot.side_effect = screenshot
    monkeypatch.setattr(capture, "_settle_page", AsyncMock())
    monkeypatch.setattr(capture, "_dismiss_overlays", AsyncMock())
    fingerprint = ["baseline"]
    element = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))

    async def click(**kwargs):
        fingerprint[0] = "opened"

    element.click = AsyncMock(side_effect=click)
    page.locator = Mock(return_value=SimpleNamespace(first=element))

    async def evaluate(script):
        if script == capture._MAP_CONTROLS_JS:
            return [{"id": "0", "label": "Open menu", "tag": "button"}]
        assert script == capture._FINGERPRINT_JS
        return fingerprint[0]

    async def escape(key):
        fingerprint[0] = "baseline"

    page.keyboard.press.side_effect = escape
    page.evaluate.side_effect = evaluate
    page.query_selector.return_value = element
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    return context, target, settings, tmp_path / "baseline.png", page, written


def test_still_over_cap_raises_through_the_baseline_path(tmp_path, monkeypatch):
    context, target, settings, path, page, written = _capture_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", 10)
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    assert result["status"] == "failed"
    assert "over the 10-byte cap" in result["error"]
    assert written and written[0].exists(), "the too-large still stays on disk"


def test_still_over_cap_raises_through_the_sweep_path(tmp_path, monkeypatch):
    _, _, settings, _, _, _ = _capture_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", 10)
    page = AsyncMock()
    page.url = "https://example.com/"
    written: list[Path] = []

    async def screenshot(**kwargs):
        path = Path(kwargs["path"])
        path.write_bytes(b"x" * 24)
        written.append(path)

    page.screenshot.side_effect = screenshot
    monkeypatch.setattr(capture, "_dismiss_overlays", AsyncMock())
    fingerprint = ["baseline"]
    element = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
    element.click = AsyncMock(side_effect=lambda **kw: fingerprint.__setitem__(0, "opened"))
    page.locator = Mock(return_value=SimpleNamespace(first=element))

    async def evaluate(script):
        if script == capture._MAP_CONTROLS_JS:
            return [{"id": "0", "label": "Open menu", "tag": "button"}]
        return fingerprint[0]

    page.evaluate.side_effect = evaluate
    page.keyboard.press.side_effect = lambda key: fingerprint.__setitem__(0, "baseline")
    target = dataclasses.replace(build_capture_plan(
        {"urls": ["https://example.com/"], "viewports": ["desktop"], "schemes": ["light"]})[1][0])
    out = tmp_path / "baseline--open--open-menu.png"
    with pytest.raises(engine.ArtifactTooLarge):
        asyncio.run(capture._sweep_states(page, target, settings, tmp_path / "baseline.png"))
    assert written == [out] and out.exists(), "the too-large sweep still stays on disk"


def test_too_large_sweep_still_stops_the_capture_as_a_still_failure(tmp_path, monkeypatch):
    context, target, settings, path, page, written = _capture_stack(
        tmp_path, monkeypatch, states=True, screenshot_sizes=(8, 200))
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", 100)
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    assert result["status"] == "failed"
    assert "over the 100-byte cap" in result["error"]
    assert all(p.exists() for p in written), "both stills stay on disk"


def test_both_call_paths_record_the_same_bytes_as_today(tmp_path, monkeypatch):
    # Runs with no account configured: local stills still work, as today (r2 edge 3).
    context, target, settings, path, page, written = _capture_stack(
        tmp_path, monkeypatch, states=True, screenshot_sizes=(24, 24))
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    assert result["status"] == "ok"
    assert result["bytes"] == path.stat().st_size == 24
    state = result["states"][0]
    assert state["bytes"] == (path.parent / state["file"]).stat().st_size == 24


def test_a_raising_screenshot_runs_no_check_and_the_error_is_unchanged(tmp_path, monkeypatch):
    calls: list[Path] = []

    def spy(path):
        calls.append(Path(path))
        return 0

    monkeypatch.setattr(capture, "check_artifact_size", spy)
    page = AsyncMock()

    async def screenshot(**kwargs):
        raise RuntimeError("boom")

    page.screenshot.side_effect = screenshot
    with pytest.raises(RuntimeError, match="^boom$"):
        asyncio.run(capture._write_still(page, tmp_path / "never.png"))
    assert calls == [], "the cap check ran for a still that was never written"


def test_no_page_screenshot_call_outside_the_funnel():
    module_source = inspect.getsource(capture)
    funnel_source = inspect.getsource(capture._write_still)
    assert ".screenshot(" in funnel_source, "the funnel itself must write the still"
    assert ".screenshot(" not in module_source.replace(funnel_source, ""), \
        "a still write point escaped the funnel"
