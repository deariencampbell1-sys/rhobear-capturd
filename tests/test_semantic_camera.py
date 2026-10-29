"""Semantic camera — target metadata, precedence, suppression, finite transforms.

Contract under test (W1-03-r1):

* ``Interaction.target`` gains OPTIONAL ``role``, source ``viewport``
  ``{width, height, deviceScaleFactor}``, ``captureSpace`` (``viewport`` |
  ``full_page``) and ``region`` (``meaningful`` | ``result`` | ``body`` |
  ``full_viewport`` | ``scrollbar_gutter`` | ``degenerate``) keys. Old specs
  missing these keys stay valid.
* Camera resolution order: **target selector first, recorded rect second,
  wide hold last**. A moved selector target still centers the recorded rect.
  Raw click coordinates are never the primary zoom target.
* Zoom is suppressed for body / full-viewport / scrollbar-gutter /
  zero-size-degenerate / missing-meaningful targets.
* All scale, translation and crop values stay finite; full-page pixel
  coordinates stay associated with their source viewport/rect.
"""

from __future__ import annotations

import math

import pytest

from capturd.walk.schema import (
    CAPTURE_SPACES,
    TARGET_REGIONS,
    classify_target_region,
    normalize_target,
)
from capturd.walk.ai_pipeline import camera_focus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _target(**over) -> dict:
    t = {
        "selector": "#buy-now",
        "tagName": "button",
        "text": "Buy now",
        "boundingRect": {"x": 400, "y": 300, "width": 200, "height": 80},
        "role": "button",
        "viewport": {"width": 1024, "height": 768, "deviceScaleFactor": 1},
        "captureSpace": "viewport",
        "region": "meaningful",
    }
    t.update(over)
    return t


def _legacy_target() -> dict:
    """Exactly the pre-W1-03 shape — no optional keys at all."""
    return {
        "selector": "#get-started",
        "tagName": "a",
        "text": "Get Started",
        "boundingRect": {"x": 100, "y": 200, "width": 240, "height": 60},
    }


def _assert_finite(value) -> None:
    assert isinstance(value, (int, float)), f"not numeric: {value!r}"
    assert math.isfinite(value), f"not finite: {value!r}"


# ---------------------------------------------------------------------------
# Field vocabulary
# ---------------------------------------------------------------------------


def test_target_metadata_vocabulary_is_locked() -> None:
    assert CAPTURE_SPACES == ("viewport", "full_page")
    assert TARGET_REGIONS == (
        "meaningful", "result", "body", "full_viewport",
        "scrollbar_gutter", "degenerate",
    )


# ---------------------------------------------------------------------------
# Every new field round-trips
# ---------------------------------------------------------------------------


def test_every_new_target_field_round_trips() -> None:
    from capturd.walk.schema import DemoSpec, DemoStep, Interaction

    raw = _target()
    t = normalize_target(raw)

    assert t["selector"] == "#buy-now"
    assert t["tagName"] == "button"
    assert t["text"] == "Buy now"
    assert t["boundingRect"] == {"x": 400, "y": 300, "width": 200, "height": 80}
    assert t["role"] == "button"
    assert t["viewport"] == {"width": 1024, "height": 768, "deviceScaleFactor": 1}
    assert t["captureSpace"] == "viewport"
    assert t["region"] == "meaningful"

    step = DemoStep(
        index=0, timestamp=0, pageUrl="https://example.com", pageTitle="t",
        interaction=Interaction(type="click", target=t,
                                hotspot={"xPct": 50, "yPct": 50}),
    )
    spec = DemoSpec(id="s", steps=[step], viewport={"width": 1024, "height": 768})
    out = spec.to_dict()["steps"][0]["interaction"]["target"]
    for key in ("role", "viewport", "captureSpace", "region"):
        assert out[key] == t[key], f"lost {key} across DemoSpec.to_dict()"


def test_legacy_spec_without_new_keys_stays_valid() -> None:
    """Old specs missing every optional key must read cleanly."""
    legacy = _legacy_target()
    t = normalize_target(legacy)

    # Legacy keys preserved verbatim.
    assert t["selector"] == "#get-started"
    assert t["tagName"] == "a"
    assert t["text"] == "Get Started"
    assert t["boundingRect"] == {"x": 100, "y": 200, "width": 240, "height": 60}
    # Optional keys are optional: no role/captureSpace invented beyond
    # defaults, and the region is derived from the recorded geometry.
    assert t["region"] == "meaningful"
    assert t.get("role") in (None, "")
    # ...and the legacy dict itself is never mutated.
    assert legacy == {
        "selector": "#get-started",
        "tagName": "a",
        "text": "Get Started",
        "boundingRect": {"x": 100, "y": 200, "width": 240, "height": 60},
    }


def test_capture_space_defaults_to_viewport_for_legacy_specs() -> None:
    t = normalize_target(_legacy_target())
    assert t.get("captureSpace") == "viewport"
    # Source viewport comes from the caller's fallback when absent.
    t2 = normalize_target(_legacy_target(), {"width": 800, "height": 600})
    assert t2["viewport"]["width"] == 800
    assert t2["viewport"]["deviceScaleFactor"] == 1


# ---------------------------------------------------------------------------
# Region classification — every suppression class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"tagName": "body", "selector": "body"}, "body"),
        ({"selector": "html", "tagName": "html"}, "body"),
        (
            {
                "selector": "#page",
                "boundingRect": {"x": 0, "y": 0, "width": 1024, "height": 768},
            },
            "full_viewport",
        ),
        (
            {
                "selector": "html",
                "boundingRect": {"x": 1010, "y": 0, "width": 14, "height": 768},
            },
            "scrollbar_gutter",
        ),
        (
            {
                "selector": "#hidden",
                "boundingRect": {"x": 10, "y": 10, "width": 0, "height": 0},
            },
            "degenerate",
        ),
        ({"boundingRect": None}, "degenerate"),
        (
            {
                "selector": "#modal",
                "role": "dialog",
                "boundingRect": {"x": 200, "y": 150, "width": 624, "height": 468},
            },
            "result",
        ),
        (
            {
                "selector": "#cta",
                "boundingRect": {"x": 400, "y": 300, "width": 200, "height": 80},
            },
            "meaningful",
        ),
    ],
)
def test_classify_target_region_all_classes(over: dict, expected: str) -> None:
    t = _target(**over)
    assert classify_target_region(t) == expected
    assert normalize_target(t)["region"] == expected


def test_empty_target_is_degenerate() -> None:
    assert classify_target_region({}) == "degenerate"
    assert normalize_target({})["region"] == "degenerate"


# ---------------------------------------------------------------------------
# Selector → rect → wide precedence
# ---------------------------------------------------------------------------


def test_precedence_selector_first() -> None:
    focus = camera_focus(
        _target(),
        keyframe_target="#buy-now",
        offset={"x": 50, "y": 50},
    )
    assert focus["source"] == "selector"
    assert focus["focus"] == {"x": 500.0, "y": 340.0}  # rect centre


def test_precedence_recorded_rect_second_when_selector_missing() -> None:
    legacy = _legacy_target()
    del legacy["selector"]
    focus = camera_focus(legacy, keyframe_target="#somewhere-else")
    assert focus["source"] == "rect"
    assert focus["focus"] == {"x": 220.0, "y": 230.0}


def test_precedence_recorded_rect_when_element_moved() -> None:
    """Selector no longer matches (element moved/renamed) → still the rect."""
    focus = camera_focus(_target(), keyframe_target="#moved-away")
    assert focus["source"] == "rect"
    assert focus["focus"] == {"x": 500.0, "y": 340.0}


def test_precedence_wide_hold_last() -> None:
    focus = camera_focus({}, keyframe_target="#gone")
    assert focus["source"] == "wide"
    assert focus["suppressed"] is True
    assert focus["rect"] is None
    _assert_finite(focus["focus"]["x"])
    _assert_finite(focus["focus"]["y"])
    assert focus["zoomLevel"] == 1.0


def test_moved_selector_target_still_centers_recorded_rect() -> None:
    """Even when the recorded rect contradicts the hotspot (raw click px),
    the camera centers the recorded rect — never the raw click point."""
    t = _target(hotspot={"xPct": 0, "yPct": 0})
    focus = camera_focus(t, keyframe_target="#buy-now")
    assert focus["source"] == "selector"
    assert focus["focus"] == {"x": 500.0, "y": 340.0}


def test_raw_click_coordinates_are_not_the_zoom_target() -> None:
    t = _target(boundingRect={"x": 10, "y": 10, "width": 40, "height": 40})
    # No keyframe offset → rect centre, regardless of hotspot (click px).
    focus = camera_focus(t, keyframe_target="#buy-now")
    assert focus["focus"] == {"x": 30.0, "y": 30.0}
    # Explicit offset → rect-relative percentages, not viewport pixels.
    focus2 = camera_focus(t, keyframe_target="#buy-now", offset={"x": 10, "y": 20})
    assert focus2["focus"] == {"x": 14.0, "y": 18.0}


# ---------------------------------------------------------------------------
# Suppression classes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"tagName": "body", "selector": "body"},                        # body
        {"boundingRect": {"x": 0, "y": 0, "width": 1024, "height": 768}},  # full viewport
        {"boundingRect": {"x": 1010, "y": 0, "width": 14, "height": 768}},  # scrollbar gutter
        {"boundingRect": {"x": 5, "y": 5, "width": 0, "height": 20}},   # zero-size
        {"boundingRect": {"x": 5, "y": 5, "width": 20, "height": 0}},   # zero-size
        {"boundingRect": {"x": float("nan"), "y": 0, "width": 10, "height": 10}},  # degenerate
    ],
)
def test_zoom_suppressed_for_meaningless_targets(over: dict) -> None:
    focus = camera_focus(_target(**over), keyframe_target=_target(**over).get("selector"))
    assert focus["suppressed"] is True, focus
    assert focus["zoomLevel"] == 1.0
    _assert_finite(focus["focus"]["x"])
    _assert_finite(focus["focus"]["y"])


def test_missing_meaningful_target_suppresses_zoom() -> None:
    focus = camera_focus({}, keyframe_target="#x")
    assert focus["suppressed"] is True


def test_meaningful_and_result_regions_are_framed_not_suppressed() -> None:
    for region in ("meaningful", "result"):
        t = _target(region=region)
        if region == "result":
            t["role"] = "dialog"
        focus = camera_focus(t, keyframe_target=t["selector"])
        assert focus["suppressed"] is False
        assert focus["zoomLevel"] > 1.0
        assert focus["region"] == region


def test_region_hints_survive_even_when_geometry_is_bounded() -> None:
    """A result/modal region is framed with bounded padding, not suppressed,
    even when the dialog is large relative to the viewport."""
    t = _target(
        selector="#modal", role="dialog",
        boundingRect={"x": 50, "y": 40, "width": 924, "height": 688},
    )
    focus = camera_focus(t, keyframe_target="#modal")
    assert focus["region"] == "result"
    assert focus["suppressed"] is False


# ---------------------------------------------------------------------------
# Finite transforms + source-space association
# ---------------------------------------------------------------------------


def test_all_camera_values_finite_under_adverse_inputs() -> None:
    nasty = [
        {},
        {"boundingRect": {"x": float("inf"), "y": float("-inf"),
                          "width": float("nan"), "height": 5}},
        {"boundingRect": {"x": -1e18, "y": -1e18, "width": 1e18, "height": 1e18}},
        _target(boundingRect={"x": "oops", "y": None, "width": "w", "height": []}),
    ]
    for target in nasty:
        for offset in (None, {"x": 50, "y": 50}, {"x": float("nan"), "y": "z"}):
            focus = camera_focus(target, keyframe_target="#x", offset=offset)
            _assert_finite(focus["focus"]["x"])
            _assert_finite(focus["focus"]["y"])
            _assert_finite(focus["zoomLevel"])
            assert focus["zoomLevel"] >= 1.0


def test_full_page_capture_space_keeps_page_coordinates() -> None:
    """Full-page pixels stay in their source page space (y beyond the
    viewport height) and stay associated with the source viewport."""
    t = _target(
        captureSpace="full_page",
        boundingRect={"x": 300, "y": 4200, "width": 300, "height": 120},
    )
    focus = camera_focus(t, keyframe_target="#buy-now")
    assert focus["captureSpace"] == "full_page"
    assert focus["viewport"] == {"width": 1024, "height": 768, "deviceScaleFactor": 1}
    assert focus["focus"] == {"x": 450.0, "y": 4260.0}  # page space, not clamped
    _assert_finite(focus["zoomLevel"])


def test_resize_source_viewport_preserves_finite_focus() -> None:
    """1024x768 → 1920x1080 source viewports: focus stays finite and the
    source viewport is carried on the resolution."""
    for vp in ({"width": 1024, "height": 768, "deviceScaleFactor": 1},
               {"width": 1920, "height": 1080, "deviceScaleFactor": 1}):
        t = _target(viewport=vp,
                    boundingRect={"x": vp["width"] * 0.4, "y": vp["height"] * 0.4,
                                  "width": 120, "height": 60})
        focus = camera_focus(t, keyframe_target="#buy-now")
        assert focus["viewport"] == vp
        _assert_finite(focus["focus"]["x"])
        _assert_finite(focus["focus"]["y"])
        _assert_finite(focus["zoomLevel"])
        assert focus["suppressed"] is False


# ---------------------------------------------------------------------------
# Chromium: compiled export plan is finite and suppresses meaningless zoom
# ---------------------------------------------------------------------------


def _render_viewer(tmp_path, spec):
    from capturd.walk.viewer import render_viewer_to_file

    out = tmp_path / "viewer.html"
    render_viewer_to_file(spec, out)
    assert out.is_file()
    return out


def _spec_with(steps) -> dict:
    return {
        "version": 1, "id": "semantic-cam", "name": "semantic cam",
        "goal": "plan", "createdAt": "2026-10-29T00:00:00Z",
        "viewport": {"width": 1024, "height": 768},
        "startUrl": "https://example.com",
        "aiAnnotations": {"style": "snappy", "summary": "s", "animationTimeline": []},
        "steps": steps,
    }


_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


def _step(index: int, target: dict) -> dict:
    return {
        "index": index, "timestamp": index,
        "pageUrl": "https://example.com", "pageTitle": "p",
        "interaction": {
            "type": "click", "target": target, "hotspot": {"xPct": 50, "yPct": 50},
        },
        "annotation": f"Step {index}.",
        "screenshotBase64": _TINY_PNG_B64,
    }


def test_export_plan_is_finite_and_suppresses_meaningless_zoom(tmp_path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    body_target = {"selector": "body", "tagName": "body", "text": "down",
                   "boundingRect": {"x": 0, "y": 0, "width": 1024, "height": 768},
                   "region": "body", "captureSpace": "viewport",
                   "viewport": {"width": 1024, "height": 768, "deviceScaleFactor": 1}}
    cta_target = _target(boundingRect={"x": 400, "y": 300, "width": 200, "height": 80})
    deg_target = _target(selector="#gone", boundingRect={"x": 0, "y": 0, "width": 0, "height": 0})

    spec = _spec_with([_step(0, body_target), _step(1, cta_target), _step(2, deg_target)])
    viewer = _render_viewer(tmp_path, spec)

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1024, "height": 768})
        page.goto(viewer.resolve().as_uri(), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.__demoExport && window.__demoViewer && window.__demoViewer.STATE.spec",
            timeout=15000,
        )
        plan = page.evaluate("() => window.__demoExport.prepare()")
        browser.close()

    fit = plan["fit"]
    for wp in plan["camWps"]:
        for key in ("scale", "x", "y"):
            assert math.isfinite(wp["cam"][key]), f"non-finite cam {key}: {wp}"
        assert wp["cam"]["scale"] > 0

    wins = {w["index"]: w for w in plan["stepWindows"]}
    # Body (step 0) and degenerate (step 2) hold wide: their cameras never
    # leave the fit transform. Meaningful CTA (step 1) zooms in.
    zoomed = [
        wp for wp in plan["camWps"]
        if wins[1]["tStart"] <= wp["t"] <= wins[1]["tEnd"] and wp["cam"]["scale"] > fit["scale"] * 1.05
    ]
    assert zoomed, "meaningful target window should contain zoomed camera waypoints"
    suppressed_zooms = [
        wp for wp in plan["camWps"]
        if wins[0]["tStart"] <= wp["t"] <= wins[0]["tEnd"] and wp["cam"]["scale"] > fit["scale"] * 1.05
    ]
    assert not suppressed_zooms, "body target window must not zoom"
