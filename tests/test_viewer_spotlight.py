"""Playwright tests for viewer spotlight + panzoom engine.

Renders the viewer against a synthetic DemoSpec with aiAnnotations.animationTimeline
and verifies:
- Spotlight overlay is present and has correct CSS properties mid-step
- Panzoom layer has a CSS transform (zoom is active)
- The viewer doesn't error on load (console check)
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def viewer_html_path(tmp_path) -> Path:
    """Render the viewer template with a synthetic DemoSpec and return the HTML path."""
    from capturd.walk.viewer import render_viewer_to_file

    spec = {
        "version": 1,
        "id": "spotlight-test",
        "name": "Spotlight Test Demo",
        "goal": "Verify spotlight overlay + zoom in Playwright",
        "createdAt": "2026-07-02T00:00:00Z",
        "viewport": {"width": 1024, "height": 768},
        "startUrl": "https://example.com",
        "aiAnnotations": {
            "style": "cinematic",
            "summary": "A test flow.",
            "animationTimeline": _make_test_timeline(),
        },
        "steps": _make_test_steps(),
    }

    out = tmp_path / "viewer.html"
    render_viewer_to_file(spec, out)
    assert out.is_file(), f"Viewer did not render to {out}"
    return out


def _make_test_steps() -> list[dict]:
    return [
        {
            "index": 0, "timestamp": 0,
            "pageUrl": "https://example.com/", "pageTitle": "Welcome",
            "interaction": {
                "type": "click",
                "target": {
                    "selector": "#get-started",
                    "tagName": "button",
                    "text": "Get Started",
                    "boundingRect": {"x": 380, "y": 320, "width": 264, "height": 64},
                },
                "hotspot": {"xPct": 50, "yPct": 50},
            },
            "annotation": "Click Get Started to begin.",
            "screenshotBase64": _placeholder_screenshot_b64(1024, 768),
        },
    ]


def _make_test_timeline() -> list[dict]:
    """Manual cinematic timeline for Playwright verification."""
    return [
        {
            "stepIndex": 0,
            "action": "spotlightOn",
            "target": "#get-started",
            "duration": 200,
            "easing": "ease-out",
            "intensity": 0.9,
        },
        {
            "stepIndex": 0,
            "action": "zoomTo",
            "target": "#get-started",
            "offset": {"x": 50, "y": 50},
            "zoomLevel": 1.5,
            "duration": 800,
            "easing": "ease-in-out",
        },
        {
            "stepIndex": 0,
            "action": "hold",
            "duration": 9999,  # long hold so the test can capture mid-step
        },
        {
            "stepIndex": 0,
            "action": "spotlightOff",
            "duration": 200,
        },
    ]


def _placeholder_screenshot_b64(width: int, height: int) -> str:
    """Return a base64 placeholder PNG-like data URL.

    We don't need a real image — just a non-empty string the viewer can
    display. Using a tiny valid 1×1 PNG as base64.
    """
    # Minimal valid 1×1 white PNG
    return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_viewer_loads_without_console_errors(viewer_html_path: Path):
    """Viewer should load without console errors."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []

        def on_error(msg):
            errors.append(msg.text)

        page.on("console", lambda msg: on_error(msg) if msg.type == "error" else None)

        page.goto(f"file://{viewer_html_path}")
        page.wait_for_timeout(2000)  # Wait for boot + initial timeline to fire

        browser.close()

    # The viewer may log innocuous errors (e.g., cannot load real screenshots)
    # but should NOT have any unrecoverable JS errors.
    critical = [e for e in errors if "Cannot read property" in e or "is not defined" in e or "undefined" in e]
    assert len(critical) == 0, f"Critical JS errors: {critical}"


@pytest.mark.slow
def test_spotlight_overlay_present_mid_step(viewer_html_path: Path):
    """Spotlight overlay should be visible (active class + non-zero opacity) after timeline starts."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"file://{viewer_html_path}")

        # Wait for boot + timeline to execute SPOTLIGHT_ON
        page.wait_for_timeout(3000)

        # Check spotlight overlay is active
        is_active = page.evaluate("""() => {
            const el = document.getElementById('spotlight-overlay');
            return el ? el.classList.contains('active') : false;
        }""")
        assert is_active, "spotlight-overlay should have 'active' class mid-step"

        # Check it has a clip-path (not 'none')
        clip_path = page.evaluate("""() => {
            const el = document.getElementById('spotlight-overlay');
            return el ? el.style.clipPath : null;
        }""")
        assert clip_path and clip_path != "none", (
            f"spotlight clip-path should not be 'none', got: {clip_path}"
        )

        browser.close()


@pytest.mark.slow
def test_panzoom_layer_has_transform(viewer_html_path: Path):
    """Panzoom layer should have a CSS transform after zoomTo keyframe executes."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"file://{viewer_html_path}")

        # Wait for zoomTo to fire (800ms duration)
        page.wait_for_timeout(3000)

        transform = page.evaluate("""() => {
            const el = document.getElementById('panzoom-layer');
            return el ? el.style.transform : null;
        }""")
        assert transform and "matrix" in (transform or ""), (
            f"panzoom-layer should have a matrix transform, got: {transform}"
        )

        browser.close()


@pytest.mark.slow
def test_spotlight_off_hides_overlay(viewer_html_path: Path):
    """After SPOTLIGHT_OFF, the overlay should lose the 'active' class."""
    from playwright.sync_api import sync_playwright

    # Create a spec with short hold so SPOTLIGHT_OFF fires quickly
    specs_path = viewer_html_path.parent / "viewer_fast.json"
    viewer_fast = viewer_html_path.parent / "viewer_fast.html"

    spec = {
        "version": 1,
        "id": "fast-spotlight-test",
        "name": "Fast Spotlight Test",
        "goal": "Verify spotlight off",
        "viewport": {"width": 1024, "height": 768},
        "startUrl": "https://example.com",
        "aiAnnotations": {
            "style": "snappy",
            "animationTimeline": [
                {"stepIndex": 0, "action": "spotlightOn", "target": "#btn",
                 "duration": 100, "easing": "ease-out", "intensity": 0.7},
                {"stepIndex": 0, "action": "zoomTo", "target": "#btn",
                 "offset": {"x": 50, "y": 50}, "zoomLevel": 1.3, "duration": 200, "easing": "ease-out"},
                {"stepIndex": 0, "action": "hold", "duration": 100},
                {"stepIndex": 0, "action": "spotlightOff", "duration": 100},
            ],
        },
        "steps": [{
            "index": 0, "timestamp": 0,
            "pageUrl": "https://example.com/", "pageTitle": "Btn",
            "interaction": {
                "type": "click",
                "target": {
                    "selector": "#btn", "tagName": "button", "text": "Click",
                    "boundingRect": {"x": 100, "y": 100, "width": 200, "height": 50},
                },
                "hotspot": {"xPct": 50, "yPct": 50},
            },
            "annotation": "Click the button.",
            "screenshotBase64": _placeholder_screenshot_b64(1024, 768),
        }],
    }

    from capturd.walk.viewer import render_viewer_to_file
    render_viewer_to_file(spec, viewer_fast)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"file://{viewer_fast}")

        # Wait long enough for the full short timeline to play (spotlightOff should fire)
        page.wait_for_timeout(2000)

        is_active = page.evaluate("""() => {
            const el = document.getElementById('spotlight-overlay');
            return el ? el.classList.contains('active') : null;
        }""")
        assert is_active is False, (
            f"spotlight-overlay should NOT be active after SPOTLIGHT_OFF, got: {is_active}"
        )

        browser.close()


# ---------------------------------------------------------------------------
# W1-03-r1 — resize / result-target / moved-target / corner-hotspot regressions
# ---------------------------------------------------------------------------


def _full_target(selector, rect, *, role="", region="meaningful",
                 viewport=None, capture_space="viewport"):
    vp = viewport or {"width": 1024, "height": 768, "deviceScaleFactor": 1}
    return {
        "selector": selector,
        "tagName": "button" if role != "dialog" else "div",
        "text": selector,
        "boundingRect": rect,
        "role": role,
        "viewport": vp,
        "captureSpace": capture_space,
        "region": region,
    }


def _mk_step(index, selector, rect, hotspot=None, **kw):
    step = {
        "index": index, "timestamp": index * 1000,
        "pageUrl": f"https://example.com/step{index}", "pageTitle": f"Step {index}",
        "interaction": {
            "type": "click",
            "target": _full_target(selector, rect, **kw),
            "hotspot": hotspot or {"xPct": 50, "yPct": 50},
        },
        "annotation": f"Click {selector}.",
    }
    return step


def _spec_for(steps, viewport):
    from capturd.walk.ai_pipeline import _deterministic_timeline

    spec = {
        "version": 1, "id": "spot-cam", "name": "spot cam",
        "goal": "semantic camera",
        "createdAt": "2026-10-29T00:00:00Z",
        "viewport": dict(viewport), "startUrl": "https://example.com",
        "aiAnnotations": {"style": "snappy"},
        "steps": steps,
    }
    spec["aiAnnotations"]["animationTimeline"] = _deterministic_timeline(spec)
    return spec


def _probe_export(viewer, viewport, seeks=(), resolves=()):
    """prepare() the export, seek each time (reading the live matrix), and
    resolve cameras through the viewer's semantic camera entry point."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=dict(viewport))
        page.goto(viewer.resolve().as_uri(), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.__demoExport && window.__demoViewer && window.__demoViewer.STATE.spec",
            timeout=15000,
        )
        plan = page.evaluate("() => window.__demoExport.prepare()")
        matrices = []
        for t in seeks:
            page.evaluate("(t) => window.__demoExport.seek(t)", t)
            matrices.append(page.evaluate(
                "() => { const m = new DOMMatrix(getComputedStyle("
                "document.getElementById('panzoom-layer')).transform);"
                " return [m.a, m.b, m.c, m.d, m.e, m.f]; }"
            ))
        resolved = [
            page.evaluate(
                "(a) => window.__demoViewer.resolveCamera(a[0], a[1], a[2])",
                [target, keyframe_target, offset],
            )
            for target, keyframe_target, offset in resolves
        ]
        browser.close()
    return plan, matrices, resolved


def _rendered(tmp_path, spec):
    from capturd.walk.viewer import render_viewer_to_file

    out = tmp_path / "spot.html"
    render_viewer_to_file(spec, out)
    assert out.is_file()
    return out


def _assert_finite_matrix(m):
    for v in m:
        assert math.isfinite(v), f"non-finite camera matrix: {m}"
    assert math.isfinite(m[0]) and m[0] > 0, f"degenerate camera scale: {m}"


def _camera_center(m, vw, vh):
    """Layer point at the viewport centre — inverse of the camera matrix."""
    a, b, c, d, e, f = m
    det = a * d - b * c
    assert det != 0
    lx = ((d) * (vw / 2 - e) + (-c) * (vh / 2 - f)) / det
    ly = ((-b) * (vw / 2 - e) + (a) * (vh / 2 - f)) / det
    return lx, ly


def _window_waypoints(plan, step_index):
    wins = {w["index"]: w for w in plan["stepWindows"]}
    w = wins[step_index]
    return [wp for wp in plan["camWps"] if w["tStart"] <= wp["t"] <= w["tEnd"]]


def test_resize_keeps_hotspot_alignment_and_finite_matrix(tmp_path):
    """1024x768 → 1920x1080 renders: hotspot-aligned camera at both sizes,
    finite matrices everywhere."""
    rect = {"x": 640, "y": 420, "width": 220, "height": 120}
    steps = [
        _mk_step(0, "#cta", rect, hotspot={"xPct": 25, "yPct": 10}),
    ]
    spec = _spec_for(steps, {"width": 1024, "height": 768})
    viewer = _rendered(tmp_path, spec)

    for vp in ({"width": 1024, "height": 768}, {"width": 1920, "height": 1080}):
        plan, matrices, _ = _probe_export(viewer, vp, seeks=(0, 1600, 3000))
        for m in matrices:
            _assert_finite_matrix(m)

        peak = max(_window_waypoints(plan, 0), key=lambda wp: wp["cam"]["scale"])
        cx, cy = _camera_center(
            [peak["cam"]["scale"], 0, 0, peak["cam"]["scale"],
             peak["cam"]["x"], peak["cam"]["y"]],
            vp["width"], vp["height"],
        )
        # The camera centres the recorded hotspot point (rect + pct offset).
        assert cx == pytest.approx(rect["x"] + rect["width"] * 0.25, abs=2)
        assert cy == pytest.approx(rect["y"] + rect["height"] * 0.10, abs=2)


def test_result_target_is_framed_with_bounded_padding(tmp_path):
    """A modal/result target is framed (zoom > fit) with BOUNDED padding —
    never an unbounded zoom of the whole viewport."""
    rect = {"x": 112, "y": 84, "width": 800, "height": 600}
    step = _mk_step(0, "#modal", rect, role="dialog", region="result")
    spec = _spec_for([step], {"width": 1024, "height": 768})
    viewer = _rendered(tmp_path, spec)

    plan, matrices, resolved = _probe_export(
        viewer, {"width": 1024, "height": 768},
        seeks=(1600,),
        resolves=[(step["interaction"]["target"], "#modal", {"x": 50, "y": 50})],
    )
    cam = resolved[0]
    assert cam["region"] == "result"
    assert cam["suppressed"] is False
    _assert_finite_matrix(matrices[0])

    fit_scale = plan["fit"]["scale"]
    pad = max(8.0, min(48.0, min(rect["width"], rect["height"]) * 0.08))
    pad_scale = min(1024 / (rect["width"] + 2 * pad), 768 / (rect["height"] + 2 * pad))

    peak = max(_window_waypoints(plan, 0), key=lambda wp: wp["cam"]["scale"])
    scale = peak["cam"]["scale"]
    assert scale > fit_scale * 1.02, "result target must be framed, not held wide"
    assert scale <= pad_scale + 1e-6, (
        f"result framing {scale} exceeds bounded padding {pad_scale}"
    )
    cx, cy = _camera_center(
        [scale, 0, 0, scale, peak["cam"]["x"], peak["cam"]["y"]], 1024, 768
    )
    assert cx == pytest.approx(rect["x"] + rect["width"] / 2, abs=2)
    assert cy == pytest.approx(rect["y"] + rect["height"] / 2, abs=2)


def test_moved_target_still_centers_recorded_rect(tmp_path):
    """When the keyframe's selector no longer matches the recorded step
    target (element moved/renamed), the camera falls back to the recorded
    rect — never the raw click point, never a lost zoom."""
    rect = {"x": 400, "y": 300, "width": 200, "height": 80}
    step = _mk_step(0, "#cta", rect, hotspot={"xPct": 0, "yPct": 0})
    spec = _spec_for([step], {"width": 1024, "height": 768})

    # Simulate the move: the timeline points at a selector that is gone.
    for kf in spec["aiAnnotations"]["animationTimeline"]:
        if kf["action"] == "zoomTo":
            kf["target"] = "#moved-away"

    viewer = _rendered(tmp_path, spec)
    plan, matrices, resolved = _probe_export(
        viewer, {"width": 1024, "height": 768},
        seeks=(1600,),
        resolves=[(step["interaction"]["target"], "#moved-away", {"x": 0, "y": 0})],
    )
    cam = resolved[0]
    assert cam["source"] == "rect", cam
    assert cam["focus"] == {"x": 400.0, "y": 300.0}, (
        "moved target must center the recorded rect (hotspot corner), "
        f"got {cam['focus']}"
    )
    _assert_finite_matrix(matrices[0])

    peak = max(_window_waypoints(plan, 0), key=lambda wp: wp["cam"]["scale"])
    cx, cy = _camera_center(
        [peak["cam"]["scale"], 0, 0, peak["cam"]["scale"],
         peak["cam"]["x"], peak["cam"]["y"]], 1024, 768
    )
    assert cx == pytest.approx(rect["x"], abs=2)
    assert cy == pytest.approx(rect["y"], abs=2)


def test_corner_hotspot_stays_finite_and_aligned(tmp_path):
    """A target pinned at the viewport corner (hotspot 0%, 0%) keeps a
    finite, stable camera at both viewport sizes."""
    rect = {"x": 0, "y": 0, "width": 80, "height": 40}
    step = _mk_step(0, "#corner", rect, hotspot={"xPct": 0, "yPct": 0})
    spec = _spec_for([step], {"width": 1024, "height": 768})
    viewer = _rendered(tmp_path, spec)

    for vp in ({"width": 1024, "height": 768}, {"width": 1920, "height": 1080}):
        plan, matrices, _ = _probe_export(viewer, vp, seeks=(0, 800, 1600, 3000))
        for m in matrices:
            _assert_finite_matrix(m)
        peak = max(_window_waypoints(plan, 0), key=lambda wp: wp["cam"]["scale"])
        cx, cy = _camera_center(
            [peak["cam"]["scale"], 0, 0, peak["cam"]["scale"],
             peak["cam"]["x"], peak["cam"]["y"]], vp["width"], vp["height"]
        )
        assert cx == pytest.approx(0.0, abs=2), f"corner hotspot drifted: {cx}"
        assert cy == pytest.approx(0.0, abs=2), f"corner hotspot drifted: {cy}"


def test_body_target_holds_wide_in_export(tmp_path):
    """Body targets never zoom in the export camera path (wide hold)."""
    step = _mk_step(0, "body", {"x": 0, "y": 0, "width": 1024, "height": 768},
                    region="body", role="")
    spec = _spec_for([step], {"width": 1024, "height": 768})
    viewer = _rendered(tmp_path, spec)

    plan, matrices, _ = _probe_export(viewer, {"width": 1024, "height": 768},
                                      seeks=(1600,))
    fit_scale = plan["fit"]["scale"]
    for wp in _window_waypoints(plan, 0):
        cam = wp["cam"]
        assert abs(cam["scale"] - fit_scale) < 1e-6, (
            f"body target zoomed: {cam} vs fit {fit_scale}"
        )
    _assert_finite_matrix(matrices[0])
