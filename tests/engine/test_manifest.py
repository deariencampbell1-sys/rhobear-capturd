"""W3-06a — the engine manifest is packaged, truthful, and the cap is real.

Everything here is driven by the briefs (W3-06a-r1..r5): the packaged manifest
carries every ADOPTION.md field, `load_manifest` refuses incomplete manifests,
`check_artifact_size` enforces `MAX_ARTIFACT_BYTES` on the exporter's size step
and on the one still funnel in `capturd/shots/capture.py`, no `page.screenshot`
call lives outside that funnel, an over-cap state is recorded and the page
continues, an over-cap still never stays on disk, `ArtifactTooLarge` is never
retried, and the packaging claim is proven against a real wheel. Local stills
run with no account, as today.
"""

from __future__ import annotations

import asyncio
import ast
import base64
import dataclasses
import importlib.resources
import importlib.util
import inspect
import itertools
import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from capturd import engine
from capturd.shots import capture
from capturd.shots.capture import build_capture_plan
from capturd.walk import exporter

PROJECT_ROOT = Path(engine.__file__).resolve().parents[2]
PORT_TYPES = {"text", "image", "video", "audio", "model3d", "diagram", "artifact", "json", "number"}
NAMED_LIMITS = {"maxInputBytes", "maxArtifactBytes", "maxActiveJobsPerAccount", "maxJobDurationMs"}
# Every format token the capabilities may advertise maps to a listed kind.
FORMAT_KINDS = {"mp4": "video/mp4", "gif": "image/gif", "png": "image/png", "jpeg": "image/jpeg"}

# One DNS stub for the capture-path tests (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("_offline_test_dns")


# ---------------------------------------------------------------------------
# The manifest itself (r1 tests 1-5, r1 edge 3-4, r2 test 9, r4 items 2/8/9)
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
    for schema in (manifest["inputSchema"], manifest["outputSchema"]):
        assert isinstance(schema, dict) and schema.get("type") == "object", schema


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
    good = engine.load_manifest()

    def bad(payload) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                        encoding="utf-8")
        monkeypatch.setattr(engine, "_MANIFEST_PATH", path)
        with pytest.raises(engine.ManifestError):
            engine.load_manifest()

    bad("{not json")                                    # not JSON at all
    bad([])                                             # a JSON list (r4 edge 3)
    bad("just a string")                                # a JSON string (r4 edge 3)
    bad({})                                             # every required key missing
    missing = dict(good)
    del missing["artifactKinds"]
    bad(missing)                                        # one required key missing
    mistyped = dict(good)
    mistyped["capabilities"] = "not-a-list"
    bad(mistyped)                                       # one required key of the wrong type


def test_load_manifest_refuses_a_bool_integer_field():
    """JSON true is not an integer: no required key is a bool (r5 item 3)."""
    good = engine.load_manifest()
    data = json.dumps({**good, "schemaVersion": True}).encode("utf-8")
    with pytest.raises(engine.ManifestError, match="schemaVersion"):
        engine.load_manifest(data)
    assert engine.load_manifest(json.dumps({**good, "schemaVersion": 1}).encode("utf-8"))[
        "schemaVersion"] == 1, "the integer 1 still loads"


def test_engine_version_matches_pyproject():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert engine.load_manifest()["engineVersion"] == pyproject["project"]["version"]


def test_dev_extra_declares_build():
    """The wheel test runs `python -m build`, so the dev extra must declare it."""
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(str(entry).startswith("build") for entry in dev), dev


def test_max_active_jobs_per_account_matches_the_render_budget():
    assert engine.load_manifest()["limits"]["maxActiveJobsPerAccount"] \
        == _service_render_max_concurrent()


def test_service_default_ignores_a_stray_env(monkeypatch):
    """A stray CAPTURD_RENDER_MAX_CONCURRENT must not leak into the default read."""
    monkeypatch.setenv("CAPTURD_RENDER_MAX_CONCURRENT", "99")
    value = _service_render_max_concurrent()
    assert value != 99
    assert value == engine.load_manifest()["limits"]["maxActiveJobsPerAccount"]


def _service_render_max_concurrent() -> int:
    """Read service/app/config.py's RENDER_MAX_CONCURRENT default (read only, never edit)."""
    config_path = PROJECT_ROOT / "service" / "app" / "config.py"
    spec = importlib.util.spec_from_file_location("capturd_service_config_readonly", config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.RENDER_MAX_CONCURRENT)


def test_every_advertised_format_maps_to_a_listed_artifact_kind():
    manifest = engine.load_manifest()
    kinds = set(manifest["artifactKinds"])
    advertised = 0
    for capability in manifest["capabilities"]:
        for name, spec in capability["params"].items():
            tokens = [token.strip() for token in str(spec).split("|")]
            if tokens[0] not in FORMAT_KINDS:
                continue
            advertised += len(tokens)
            for token in tokens:
                assert token in FORMAT_KINDS, f"{capability['id']}.{name} advertises {token!r}"
                assert FORMAT_KINDS[token] in kinds, (
                    f"{capability['id']}.{name}={token} has no listed artifact kind")
    assert advertised >= 4, "stills formats and walk-export fmt must both be advertised"
    assert "image/gif" in kinds


# ---------------------------------------------------------------------------
# The wheel really packages the manifest (r4 item 1)
# ---------------------------------------------------------------------------

def test_the_published_wheel_carries_a_loadable_manifest(tmp_path):
    """Prove packaging against a real wheel. A missing build tool or a failed
    build fails loudly with the command's output below — this never skips."""
    out_dir = tmp_path / "dist"
    build_dir = PROJECT_ROOT / "build"
    build_dir_preexisting = build_dir.exists()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out_dir)],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=600,
        )
    finally:
        if not build_dir_preexisting:
            shutil.rmtree(build_dir, ignore_errors=True)
    assert proc.returncode == 0, (
        "python -m build --wheel failed — the packaged manifest is unproven\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    wheels = sorted(out_dir.glob("*.whl"))
    assert wheels, f"build produced no wheel\n{proc.stdout}\n{proc.stderr}"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        assert "capturd/engine/manifest.json" in names, names
        packaged = wheel.read("capturd/engine/manifest.json")
    manifest = engine.load_manifest(packaged)
    assert manifest["engine"] == "capturd"
    assert manifest["limits"]["maxArtifactBytes"] == engine.MAX_ARTIFACT_BYTES


# ---------------------------------------------------------------------------
# check_artifact_size (r2 test 6, r2 edge 1-2)
# ---------------------------------------------------------------------------

def test_check_artifact_size_at_cap_passes_one_byte_over_raises(tmp_path, monkeypatch):
    cap = 128  # a patched cap: no 1 GiB sparse files needed
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", cap)
    at_cap = tmp_path / "at-cap.bin"
    at_cap.write_bytes(b"\0" * cap)
    assert engine.check_artifact_size(at_cap) == cap

    over = tmp_path / "one-over.bin"
    over.write_bytes(b"\0" * (cap + 1))
    with pytest.raises(engine.ArtifactTooLarge) as exc:
        engine.check_artifact_size(over)
    # the error names its size and the cap, and the file stays on disk
    assert str(cap + 1) in str(exc.value)
    assert str(cap) in str(exc.value)
    assert over.exists()


def test_check_artifact_size_missing_file_never_passes(tmp_path):
    with pytest.raises(OSError):
        engine.check_artifact_size(tmp_path / "missing.bin")


# ---------------------------------------------------------------------------
# The exporter's size step (r2 change 2, r2 test 7, r4 item 6)
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


def test_export_over_the_cap_refuses_as_a_demo_export_error(tmp_path, monkeypatch):
    with pytest.raises(exporter.DemoExportError) as exc:
        _export_over_env(tmp_path, monkeypatch, artifact_bytes=24, cap=10)
    assert "10" in str(exc.value) and "24" in str(exc.value)
    assert exc.value.size == 24 and exc.value.cap == 10
    assert not isinstance(exc.value, ValueError), "the caller sees DemoExportError, not ValueError"
    assert (tmp_path / "demo.mp4").exists(), "the too-large export stays on disk"


def test_export_under_the_cap_still_succeeds(tmp_path, monkeypatch):
    out = _export_over_env(tmp_path, monkeypatch, artifact_bytes=8, cap=10)
    assert out.is_file() and out.stat().st_size == 8


def test_export_vanished_output_is_a_demo_export_error(tmp_path, monkeypatch):
    """An OSError from the size step is mapped too — a refusal never escapes raw (r5 item 2)."""

    def vanished(path):
        raise FileNotFoundError(f"output vanished: {path}")

    monkeypatch.setattr(exporter, "check_artifact_size", vanished)
    with pytest.raises(exporter.DemoExportError) as exc:
        _export_over_env(tmp_path, monkeypatch, artifact_bytes=24, cap=10)
    assert exc.value.size is None and exc.value.cap is None
    assert "demo.mp4" in str(exc.value)


def _forge_with_overcap_demo(tmp_path, monkeypatch, *, artifact_bytes, cap):
    """A real DemoForge demo whose (faked) encoder writes an over-cap video."""
    from capturd.walk.coordinator import DemoForge

    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", cap)
    monkeypatch.setattr(exporter, "find_ffmpeg", lambda: "ffmpeg")

    async def fake_capture_frames(viewer_html, viewport, frames_dir, fps):
        return 2, 1.0, []

    def fake_encode_mp4(ffmpeg, frames_dir, fps, duration_s, out_path, audio_clips):
        out_path.write_bytes(b"v" * artifact_bytes)

    monkeypatch.setattr(exporter, "_capture_frames", fake_capture_frames)
    monkeypatch.setattr(exporter, "_encode_mp4", fake_encode_mp4)

    forge = DemoForge(demos_dir=tmp_path / "demos")
    step = {
        "index": 0, "timestamp": 0, "pageUrl": "https://example.com", "pageTitle": "t",
        "interaction": {"type": "click",
                        "target": {"selector": "body", "tagName": "", "text": "down",
                                   "boundingRect": {"x": 0, "y": 0, "width": 16, "height": 16}},
                        "hotspot": {"xPct": 50, "yPct": 50}},
        "annotation": "Step 0.",
        "screenshotBase64": base64.b64encode(b"png-bytes").decode("ascii"),
        "screenshotPath": None,
    }
    spec = {"version": 1, "id": "capped", "name": "capped", "goal": "capped",
            "createdAt": "2026-10-29T00:00:00Z", "viewport": {"width": 16, "height": 16},
            "startUrl": "https://example.com",
            "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
            "steps": [step]}
    forge.save_spec("capped", spec)
    return forge


def test_export_demo_refuses_an_oversized_video_as_a_demo_export_error(tmp_path, monkeypatch):
    forge = _forge_with_overcap_demo(tmp_path, monkeypatch, artifact_bytes=24, cap=10)
    with pytest.raises(exporter.DemoExportError) as exc:
        forge.export_demo("capped", fmt="mp4")
    assert type(exc.value) is exporter.DemoExportError
    assert not isinstance(exc.value, ValueError), "the caller sees DemoExportError, not ValueError"
    assert exc.value.size == 24 and exc.value.cap == 10
    assert "24" in str(exc.value) and "10" in str(exc.value)


# ---------------------------------------------------------------------------
# The one still funnel (r3, r4 rulings R1/R2/R3)
# ---------------------------------------------------------------------------

def _capture_stack(tmp_path, monkeypatch, *, states=False, screenshot_sizes=(24,),
                   retries=0, controls=None):
    """Mock browser I/O only. Drive the real _capture_one AND _sweep_states."""
    _, targets, settings = build_capture_plan(
        {"urls": ["https://example.com/"], "states": states, "retries": retries,
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
    clicks = itertools.count(1)
    element = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))

    async def click(**kwargs):
        fingerprint[0] = f"opened-{next(clicks)}"

    element.click = AsyncMock(side_effect=click)
    page.locator = Mock(return_value=SimpleNamespace(first=element))

    async def evaluate(script):
        if script == capture._MAP_CONTROLS_JS:
            return list(controls if controls is not None
                        else [{"id": "0", "label": "Open menu", "tag": "button"}])
        assert script == capture._FINGERPRINT_JS
        return fingerprint[0]

    async def escape(key):
        fingerprint[0] = "baseline"

    page.keyboard.press.side_effect = escape
    page.evaluate.side_effect = evaluate
    page.query_selector.return_value = element
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    return context, target, settings, tmp_path / "baseline.png", page, written


def test_still_over_cap_fails_the_baseline_without_retry(tmp_path, monkeypatch):
    context, target, settings, path, page, written = _capture_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", 10)
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    assert result["status"] == "failed"
    assert result["error"] == "artifact_too_large"
    assert result["bytes"] == 24 and result["cap"] == 10
    assert result["attempts"] == 1
    assert not path.exists(), "the over-cap baseline never stays on disk"
    assert [p.name for p in tmp_path.iterdir()] == [], "no other file was left behind"


@pytest.mark.parametrize("retries", [1, 2])
def test_artifact_too_large_on_the_baseline_is_never_retried(tmp_path, monkeypatch, retries):
    context, target, settings, path, page, written = _capture_stack(
        tmp_path, monkeypatch, retries=retries, screenshot_sizes=(24,))
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", 10)
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    assert result["status"] == "failed" and result["error"] == "artifact_too_large"
    assert result["attempts"] == 1, "the cap refusal is never retried"
    assert result["bytes"] == 24 and result["cap"] == 10
    assert page.goto.await_count == 1, "exactly one navigation"
    assert len(written) == 1, "exactly one write attempt"
    assert not [p for p in tmp_path.rglob("*") if p.is_file()], "nothing stays on disk"


def test_exhausted_retries_report_the_whole_budget(tmp_path, monkeypatch):
    """A failure that survives every retry reports the full attempts budget (r5 item 5)."""
    context, target, settings, path, page, _written = _capture_stack(
        tmp_path, monkeypatch, retries=2)
    page.screenshot.side_effect = RuntimeError("boom")
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    assert result["status"] == "failed"
    assert result["attempts"] == 3, "retries=2 means the whole budget is 3 attempts"


def test_still_over_cap_in_the_sweep_is_recorded_and_the_sweep_continues(tmp_path, monkeypatch):
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
    shots = asyncio.run(capture._sweep_states(page, target, settings, tmp_path / "baseline.png"))
    assert shots == [{
        "url": target.url, "state_id": target.state_id,
        "viewport": target.viewport_id, "scheme": target.scheme,
        "width": target.width, "height": target.height,
        "status": "failed", "error": "artifact_too_large",
        "bytes": 24, "cap": 10,
        "state": "open", "control": "Open menu", "control_tag": "button",
    }]
    assert written == [out] and not out.exists(), "the over-cap sweep still never stays on disk"


def test_two_over_cap_states_and_three_good_ones_on_one_page(tmp_path, monkeypatch):
    """r4 edge 1: an over-cap state is recorded, the page keeps going."""
    context, target, settings, path, page, written = _capture_stack(
        tmp_path, monkeypatch, states=True,
        controls=[{"id": str(i), "label": f"Control {i}", "tag": "button"} for i in range(5)],
        screenshot_sizes=(8, 200, 24, 24, 24, 200))
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", 100)
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    assert result["status"] == "ok", result
    assert result["bytes"] == 8 and path.exists(), "the baseline still is kept"
    states = result["states"]
    assert [s["status"] for s in states] == ["failed", "ok", "ok", "ok", "failed"]
    for state in states:
        if state["status"] == "failed":
            assert state["error"] == "artifact_too_large"
            assert state["bytes"] == 200 and state["cap"] == 100
        else:
            assert state["bytes"] == 24
    survivors = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(survivors) == 4, survivors  # baseline + three good states
    assert all(p.stat().st_size <= 100 for p in survivors), "no oversized file on disk"


def test_write_still_deletes_the_oversized_file_and_touches_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MAX_ARTIFACT_BYTES", 10)
    page = AsyncMock()

    async def screenshot(**kwargs):
        Path(kwargs["path"]).write_bytes(b"x" * 24)

    page.screenshot.side_effect = screenshot
    with pytest.raises(engine.ArtifactTooLarge) as exc:
        asyncio.run(capture._write_still(page, tmp_path / "big.png", type="png"))
    assert exc.value.size == 24 and exc.value.cap == 10
    assert not (tmp_path / "big.png").exists(), "the over-cap file is deleted"
    assert [p.name for p in tmp_path.iterdir()] == [], "no other file was touched"


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
    """AST walk: every screenshot call lives inside _write_still (r4 item 10)."""
    tree = ast.parse(inspect.getsource(capture))

    def screenshot_calls(node):
        return [n for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "screenshot"]

    funnels = [n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_write_still"]
    assert len(funnels) == 1, "the one still funnel must exist exactly once"
    inside = screenshot_calls(funnels[0])
    assert inside, "the funnel itself must write the still"
    assert len(screenshot_calls(tree)) == len(inside), "a still write point escaped the funnel"
