"""Semantic camera targeting — regression suite for the click-focus bug.

The camera must frame the recorded ELEMENT (padded), never a raw/stale click
coordinate. Covers the owner's demanded regressions:

* recorded 1024×768 played in a 1920×1080 viewport (resize remap)
* 1440×900 recording (scrollbar-gutter coordinates rejected)
* degenerate / body / non-meaningful targets -> zoom suppressed, wide shot held
* modal-opened case -> camera frames the RESULT region (next step's element)
* element moved after interaction -> camera centers the recorded rect
* keyframe selector that matches nothing -> holds, no crash
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _shot() -> str:
    return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="


def _step(i: int, label: str, *, rect=None, meaningful=True, tag="button") -> dict:
    target = {
        "selector": f"#el-{i}",
        "tagName": tag,
        "text": label,
        "boundingRect": rect or {"x": 200, "y": 300, "width": 260, "height": 60},
        "meaningful": meaningful,
    }
    return {
        "index": i, "timestamp": i * 1000, "pageUrl": "https://x.test",
        "pageTitle": label,
        "interaction": {"type": "click", "target": target,
                        "hotspot": {"xPct": 50, "yPct": 50}},
        "annotation": label,
        "screenshotBase64": _shot(),
    }


def _spec(steps: list[dict], *, vp_w=1024, vp_h=768) -> dict:
    return {
        "version": 1, "id": "semcam", "name": "Semantic Camera", "goal": "g",
        "createdAt": "2026-08-30T00:00:00Z",
        "viewport": {"width": vp_w, "height": vp_h}, "startUrl": "https://x.test",
        "steps": steps,
    }


class Cam:
    """Render + drive the viewer; read back the panzoom transform."""

    def __init__(self, pw, spec, tmp_path, out_w=1920, out_h=1080):
        from capturd.walk.viewer import render_viewer_to_file
        self.path = tmp_path / "viewer.html"
        render_viewer_to_file(spec, self.path)
        self.browser = pw.chromium.launch()
        self.ctx = self.browser.new_context(
            viewport={"width": out_w, "height": out_h})
        self.page = self.ctx.new_page()

    def goto(self):
        self.page.goto(self.path.as_uri(), wait_until="load")
        self.page.wait_for_function("window.__demoViewer")
        self.page.wait_for_timeout(400)

    def transform(self):
        tr = self.page.evaluate(
            "window.__demoViewer.STATE.panzoom.getTransform()")
        rect = self.page.evaluate(
            "(() => { const r = document.getElementById("
            "'screenshot-container').getBoundingClientRect();"
            " return {cx: r.x + r.width/2, cy: r.y + r.height/2}; })()")
        tr["ccx"], tr["ccy"] = rect["cx"], rect["cy"]
        return tr

    def fit_transform(self):
        return self.page.evaluate(
            "(() => { const f = window.__demoViewer.STATE.fit;"
            " return {x: f.x, y: f.y, scale: f.scale}; })()")

    def zoom_step(self, i, level=2.0):
        self.page.evaluate(f"window.__demoViewer.goToStep({i}, 1, true)")
        self.page.wait_for_timeout(700)      # transition + timeline kick
        self.page.evaluate(
            "window.__demoViewer.playTimeline(window.__demoViewer.STATE.index)")
        self.page.wait_for_timeout(900)      # camera animation (500ms)

    def close(self):
        self.browser.close()


def _center_of(tr, cx, cy):
    return (cx * tr["scale"] + tr["x"], cy * tr["scale"] + tr["y"])


# ---------------------------------------------------------------------------
# 1440×900 recording (scrollbar-gutter garbage) -> zoom suppressed
# ---------------------------------------------------------------------------


def test_gutter_coordinate_zoom_suppressed(tmp_path):
    """The exact footage bug: a click recorded at (1437, 322) — scrollbar
    gutter of a 1440×900 viewport — must NOT zoom. Wide shot held."""
    from playwright.sync_api import sync_playwright
    spec = _spec([_step(0, "Edge click",
                        rect={"x": 1420, "y": 310, "width": 18, "height": 24},
                        meaningful=False, tag="body")])
    with sync_playwright() as pw:
        cam = Cam(pw, spec, tmp_path)
        cam.goto()
        fit = cam.fit_transform()
        cam.zoom_step(0, level=2.4)
        tr = cam.transform()
        cam.close()
    assert tr["scale"] == pytest.approx(fit["scale"]), \
        "gutter/garbage target must hold the wide shot"


# ---------------------------------------------------------------------------
# Modal opened by click -> frame the RESULT region (next step's element)
# ---------------------------------------------------------------------------


def test_modal_result_region_becomes_focus(tmp_path):
    from playwright.sync_api import sync_playwright
    spec = _spec([
        _step(0, "Open dialog",
              rect={"x": 1437, "y": 20, "width": 3, "height": 3},
              meaningful=False),
        _step(1, "Dialog content",
              rect={"x": 380, "y": 160, "width": 420, "height": 300}),
    ])
    with sync_playwright() as pw:
        cam = Cam(pw, spec, tmp_path)
        cam.goto()
        fit = cam.fit_transform()
        cam.zoom_step(0, level=2.0)     # degenerate -> falls to step 1's rect
        tr = cam.transform()
        ccx, ccy = tr["ccx"], tr["ccy"]
        cam.close()

    sx, sy = _center_of(tr, 380 + 210, 160 + 150)   # result rect centre
    assert tr["scale"] > fit["scale"], "result region should be zoomed"
    assert abs(sx - ccx) < 30 and abs(sy - ccy) < 30,         f"result region must be centred, got ({sx:.0f},{sy:.0f})"


# ---------------------------------------------------------------------------
# Resize remap: 1024×768 recording played at 1920×1080
# ---------------------------------------------------------------------------


def test_recorded_rect_remaps_to_output_viewport(tmp_path):
    from playwright.sync_api import sync_playwright
    spec = _spec([_step(0, "Target control",
                        rect={"x": 300, "y": 220, "width": 260, "height": 60})],
                 vp_w=1024, vp_h=768)
    with sync_playwright() as pw:
        cam = Cam(pw, spec, tmp_path, 1920, 1080)
        cam.goto()
        cam.zoom_step(0, level=2.0)
        tr = cam.transform()
        cam.close()
    sx, sy = _center_of(tr, 300 + 130, 220 + 30)
    assert abs(sx - tr["ccx"]) < 30 and abs(sy - tr["ccy"]) < 30, \
        "recorded element centre must map to output centre across resize"


# ---------------------------------------------------------------------------
# Element moved after interaction -> camera centers the RECORDED rect
# ---------------------------------------------------------------------------


def test_hotspot_vs_rect_camera_centers_rect(tmp_path):
    """Hotspot (click point) at the element's corner; the camera must centre
    the element's bounding box, not the pointer coordinate."""
    from playwright.sync_api import sync_playwright
    step = _step(0, "Moved target",
                 rect={"x": 200, "y": 300, "width": 260, "height": 60})
    step["interaction"]["hotspot"] = {"xPct": 98, "yPct": 4}
    with sync_playwright() as pw:
        cam = Cam(pw, _spec([step]), tmp_path)
        cam.goto()
        cam.zoom_step(0, level=2.0)
        tr = cam.transform()
        cam.close()
    sx, sy = _center_of(tr, 200 + 130, 300 + 30)   # RECT centre, not corner
    assert abs(sx - tr["ccx"]) < 30 and abs(sy - tr["ccy"]) < 30


# ---------------------------------------------------------------------------
# Selector resolution failure -> hold, no crash
# ---------------------------------------------------------------------------


def test_unresolvable_keyframe_selector_frames_recorded_element(tmp_path):
    """Architectural guarantee: the recorded element is the source of truth.
    A keyframe selector matching nothing still frames the step's recorded
    element rect (padded) — never a crash, never raw click coordinates."""
    from playwright.sync_api import sync_playwright
    spec = _spec([_step(0, "A", rect={"x": 200, "y": 300,
                                      "width": 260, "height": 60}),
                  _step(1, "B")])
    spec["aiAnnotations"] = {"animationTimeline": [
        {"stepIndex": 0, "action": "zoomTo",
         "target": "#does-not-exist-anywhere", "duration": 400},
    ]}
    with sync_playwright() as pw:
        cam = Cam(pw, spec, tmp_path)
        cam.goto()
        fit = cam.fit_transform()
        cam.page.evaluate("window.__demoViewer.playTimeline(0)")
        cam.page.wait_for_timeout(900)
        tr = cam.transform()
        ccx, ccy = tr["ccx"], tr["ccy"]
        cam.close()

    assert tr["scale"] > fit["scale"], "recorded element should be zoomed"
    sx, sy = _center_of(tr, 200 + 130, 300 + 30)
    assert abs(sx - ccx) < 30 and abs(sy - ccy) < 30
