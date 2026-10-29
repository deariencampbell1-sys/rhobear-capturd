"""Animation timeline generator tests — W2: semantic zoom + spotlight.

Tests the deterministic fallback (no LLM needed) and validates the
full keyframe contract end-to-end.
"""

from __future__ import annotations

import pytest

from capturd.walk.schema import CameraAction


# ---------------------------------------------------------------------------
# Helpers — call the deterministic generator directly
# ---------------------------------------------------------------------------


def _make_spec(steps_data, *, style="smooth", viewport=None):
    """Build a minimal DemoSpec dict with steps."""
    spec = {
        "version": 1,
        "id": "test-timeline",
        "name": "Timeline test",
        "goal": "Validate keyframe generation",
        "viewport": viewport or {"width": 1440, "height": 900},
        "startUrl": "https://example.com",
        "steps": steps_data,
        "aiAnnotations": {"style": style},
    }
    return spec


def _make_step(index, selector, bounding_rect=None, hotspot=None, annotation=None,
               voiceover_words=None):
    """Build a minimal step dict."""
    rect = bounding_rect or {"x": 100, "y": 200, "width": 200, "height": 50}
    hs = hotspot or {"xPct": 50, "yPct": 50}
    step = {
        "index": index,
        "timestamp": index * 1000,
        "pageUrl": f"https://example.com/step{index}",
        "pageTitle": f"Step {index}",
        "interaction": {
            "type": "click",
            "target": {
                "selector": selector,
                "tagName": "button",
                "text": selector,
                "boundingRect": rect,
            },
            "hotspot": hs,
        },
        "annotation": annotation or f"Click {selector}.",
    }
    if voiceover_words:
        step["voiceoverWords"] = [
            {"word": w["word"], "tStartMs": w["start"], "tEndMs": w["end"]}
            for w in voiceover_words
        ]
    return step


# ---------------------------------------------------------------------------
# Tests — deterministic fallback shape
# ---------------------------------------------------------------------------


def test_deterministic_timeline_emits_spotlight_bracketing():
    """Each step must emit SPOTLIGHT_ON → ZOOM_TO → HOLD → SPOTLIGHT_OFF."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    steps = [
        _make_step(0, "#get-started"),
        _make_step(1, ".cta-button", bounding_rect={"x": 300, "y": 400, "width": 160, "height": 44}),
        _make_step(2, "#export-pdf"),
    ]
    spec = _make_spec(steps, style="smooth")
    timeline = _deterministic_timeline(spec)

    # Should have 4 keyframes per step = 12 total
    assert len(timeline) >= 3 * 4

    for i in range(3):
        step_kfs = [k for k in timeline if k["stepIndex"] == i]
        actions = [k["action"] for k in step_kfs]
        assert actions == [
            CameraAction.SPOTLIGHT_ON.value,
            CameraAction.ZOOM_TO.value,
            CameraAction.HOLD.value,
            CameraAction.SPOTLIGHT_OFF.value,
        ], f"Step {i}: expected SPOTLIGHT_ON → ZOOM_TO → HOLD → SPOTLIGHT_OFF, got {actions}"


def test_deterministic_timeline_zoom_level_adaptive():
    """Small targets get higher zoom levels."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    small_step = _make_step(0, ".tiny-btn",
                            bounding_rect={"x": 10, "y": 10, "width": 20, "height": 20})
    large_step = _make_step(1, ".big-hero",
                            bounding_rect={"x": 0, "y": 0, "width": 800, "height": 600})

    spec = _make_spec([small_step, large_step])
    timeline = _deterministic_timeline(spec)

    small_zoom = [k for k in timeline if k["stepIndex"] == 0 and k["action"] == "zoomTo"]
    large_zoom = [k for k in timeline if k["stepIndex"] == 1 and k["action"] == "zoomTo"]

    assert small_zoom, "small target missing zoomTo"
    assert large_zoom, "large target missing zoomTo"

    assert small_zoom[0]["zoomLevel"] > large_zoom[0]["zoomLevel"], (
        f"Small target zoom {small_zoom[0]['zoomLevel']} should be > "
        f"large target zoom {large_zoom[0]['zoomLevel']}"
    )


# ---------------------------------------------------------------------------
# Style parameter tests
# ---------------------------------------------------------------------------


def test_cinematic_style_durations():
    """Cinematic style produces zoom durations in 700-1000ms range."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    steps = [_make_step(0, "#hero")]
    spec = _make_spec(steps, style="cinematic")
    timeline = _deterministic_timeline(spec)

    zoom_kfs = [k for k in timeline if k["action"] == "zoomTo"]
    for kf in zoom_kfs:
        assert 700 <= kf["duration"] <= 1000, (
            f"cinematic zoom duration {kf['duration']} not in 700-1000 range"
        )


def test_snappy_style_durations():
    """Snappy style produces zoom durations in 300-400ms range."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    steps = [_make_step(0, "#hero")]
    spec = _make_spec(steps, style="snappy")
    timeline = _deterministic_timeline(spec)

    zoom_kfs = [k for k in timeline if k["action"] == "zoomTo"]
    for kf in zoom_kfs:
        assert 300 <= kf["duration"] <= 400, (
            f"snappy zoom duration {kf['duration']} not in 300-400 range"
        )


def test_cinematic_spotlight_intensity_higher_than_snappy():
    """Cinematic spotlights should have higher intensity than snappy."""
    from capturd.walk.ai_pipeline import _STYLE_PARAMS

    assert _STYLE_PARAMS["cinematic"]["spotlight_intensity"] > _STYLE_PARAMS["snappy"]["spotlight_intensity"]


def test_style_easing_propagates():
    """Each style has a distinct easing curve."""
    from capturd.walk.ai_pipeline import _deterministic_timeline, _STYLE_PARAMS

    for style in ("snappy", "smooth", "professional", "cinematic"):
        steps = [_make_step(0, "#hero")]
        spec = _make_spec(steps, style=style)
        timeline = _deterministic_timeline(spec)
        zoom_kfs = [k for k in timeline if k["action"] == "zoomTo"]
        assert len(zoom_kfs) > 0
        expected = _STYLE_PARAMS[style]["easing"]
        actual = zoom_kfs[0].get("easing")
        assert actual == expected, f"Style {style}: expected easing '{expected}', got '{actual}'"


# ---------------------------------------------------------------------------
# Voice-over alignment tests
# ---------------------------------------------------------------------------


def test_voiceover_words_align_tStartMs():
    """When voiceoverWords are present, the zoomTo keyframe's tStartMs
    should align to the focus noun's word offset."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    steps = [_make_step(
        0, "#buy-now",
        annotation="Click Buy Now to complete purchase",
        voiceover_words=[
            {"word": "Click",  "start": 0,   "end": 200},
            {"word": "Buy",    "start": 250, "end": 400},
            {"word": "Now",    "start": 450, "end": 600},
            {"word": "to",     "start": 650, "end": 750},
            {"word": "complete","start": 800, "end": 1000},
            {"word": "purchase","start": 1050,"end": 1300},
        ],
    )]
    spec = _make_spec(steps, style="smooth")
    timeline = _deterministic_timeline(spec)

    zoom_kfs = [k for k in timeline if k["action"] == "zoomTo"]
    assert len(zoom_kfs) > 0, "Expected at least one zoomTo keyframe"

    tStart = zoom_kfs[0].get("tStartMs")
    assert tStart is not None, "zoomTo should have tStartMs when voiceoverWords present"

    # "Buy" starts at 250ms — tStartMs should be at or near 250ms
    assert abs(tStart - 250) <= 200, (
        f"tStartMs {tStart} should align to 'Buy' at 250ms (tolerance ±200ms)"
    )


def test_voiceover_words_no_match_fallback():
    """When no focus noun is found, tStartMs should be omitted."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    steps = [_make_step(
        0, "#nav",
        annotation="Navigate to the page",
        voiceover_words=[
            {"word": "Navigate", "start": 0,   "end": 300},
            {"word": "to",       "start": 350, "end": 450},
            {"word": "the",      "start": 500, "end": 550},
            {"word": "page",     "start": 600, "end": 800},
        ],
    )]
    spec = _make_spec(steps, style="smooth")
    timeline = _deterministic_timeline(spec)

    zoom_kfs = [k for k in timeline if k["action"] == "zoomTo"]
    assert len(zoom_kfs) > 0

    # No action verb-noun pair to extract — tStartMs should be absent
    assert "tStartMs" not in zoom_kfs[0], (
        "tStartMs should be omitted when no focus noun matches"
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_deterministic_timeline_empty_steps():
    """Empty step list produces empty timeline."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    spec = _make_spec([])
    timeline = _deterministic_timeline(spec)
    assert timeline == []


def test_deterministic_timeline_missing_selector():
    """Step with missing selector falls back to 'body'."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    step = {
        "index": 0,
        "timestamp": 0,
        "pageUrl": "https://example.com",
        "pageTitle": "Example",
        "interaction": {
            "type": "navigate",
            "target": {"boundingRect": {"x": 0, "y": 0, "width": 1440, "height": 900}},
            "hotspot": {"xPct": 50, "yPct": 50},
        },
        "annotation": "View the page.",
    }
    spec = _make_spec([step])
    timeline = _deterministic_timeline(spec)

    spotlight_kf = [k for k in timeline if k["action"] == "spotlightOn"]
    assert len(spotlight_kf) > 0
    assert spotlight_kf[0]["target"] == "body", "missing selector should fall back to 'body'"


def test_timeline_steps_sorted_by_stepIndex():
    """Timeline entries must be sorted by stepIndex even if LLM emits them out of order."""
    from capturd.walk.ai_pipeline import _validate_timeline

    raw = [
        {"stepIndex": 2, "action": "spotlightOn", "target": "#c"},
        {"stepIndex": 0, "action": "spotlightOn", "target": "#a"},
        {"stepIndex": 1, "action": "spotlightOn", "target": "#b"},
    ]
    validated = _validate_timeline(raw, 3)
    indices = [k["stepIndex"] for k in validated]
    assert indices == [0, 1, 2], f"Expected sorted by stepIndex, got {indices}"


# ---------------------------------------------------------------------------
# CameraAction contract — ensure all actions are valid enum values
# ---------------------------------------------------------------------------


def test_camera_action_enum_values():
    """All CameraAction values used in the timeline must be valid enum members."""
    valid = {m.value for m in CameraAction}
    # We use these in the code
    used = {"zoomTo", "panTo", "zoomToFit", "reset", "hold", "spotlightOn", "spotlightOff"}
    missing = used - valid
    assert not missing, f"CameraAction enum missing values: {missing}"


def test_camera_action_values_match_schema():
    """Verify CameraAction enum matches the agreed contract."""
    expected = {"zoomTo", "panTo", "zoomToFit", "reset", "hold", "spotlightOn", "spotlightOff"}
    actual = {m.value for m in CameraAction}
    assert actual == expected, f"CameraAction: expected {expected}, got {actual}"


# ---------------------------------------------------------------------------
# W1-03-r1 — semantic camera timeline regressions
# ---------------------------------------------------------------------------


def test_suppressed_targets_hold_wide_and_result_targets_zoom():
    """body / full-viewport / scrollbar-gutter / degenerate targets hold WIDE
    (no zoomTo keyframe); result (dialog) and meaningful targets are framed."""
    from capturd.walk.ai_pipeline import _deterministic_timeline

    body = _make_step(0, "body", bounding_rect={"x": 0, "y": 0, "width": 1024, "height": 768})
    modal = _make_step(1, "#modal", bounding_rect={"x": 200, "y": 150, "width": 624, "height": 468})
    modal["interaction"]["target"]["role"] = "dialog"
    gutter = _make_step(2, "html", bounding_rect={"x": 1010, "y": 0, "width": 14, "height": 768})
    tiny_gone = _make_step(3, "#gone", bounding_rect={"x": 5, "y": 5, "width": 0, "height": 0})
    cta = _make_step(4, "#cta", bounding_rect={"x": 400, "y": 300, "width": 200, "height": 80})

    spec = _make_spec([body, modal, gutter, tiny_gone, cta])
    timeline = _deterministic_timeline(spec)

    def actions_of(i):
        return [k["action"] for k in timeline if k["stepIndex"] == i]

    for i in (0, 2, 3):  # body, scrollbar gutter, degenerate → hold wide
        assert "zoomTo" not in actions_of(i), (
            f"step {i} is a meaningless target but zooms: {actions_of(i)}"
        )
    for i in (1, 4):  # result + meaningful → framed
        assert "zoomTo" in actions_of(i), f"step {i} should be framed: {actions_of(i)}"

    modal_zoom = [k for k in timeline if k["stepIndex"] == 1 and k["action"] == "zoomTo"][0]
    assert modal_zoom["zoomLevel"] > 1.0


def test_full_page_steps_keep_page_coordinate_focus():
    """Full-page captures keep their page-space coordinates (y can exceed the
    viewport height) and stay associated with the step's source viewport."""
    from capturd.walk.ai_pipeline import _deterministic_timeline, camera_focus

    far = _make_step(0, "#far", bounding_rect={"x": 300, "y": 4200, "width": 300, "height": 120})
    far["interaction"]["target"]["captureSpace"] = "full_page"
    far["interaction"]["target"]["viewport"] = {"width": 1024, "height": 768,
                                                "deviceScaleFactor": 1}
    spec = _make_spec([far], viewport={"width": 1024, "height": 768})
    timeline = _deterministic_timeline(spec)

    zoom = [k for k in timeline if k["action"] == "zoomTo"]
    assert zoom, "full-page step should still be framed"
    kf = zoom[0]
    focus = camera_focus(
        far["interaction"]["target"],
        keyframe_target=kf.get("target"),
        offset=kf.get("offset"),
    )
    assert focus["captureSpace"] == "full_page"
    assert focus["focus"]["y"] == pytest.approx(4260.0), (
        "full-page focus must stay in page coordinates, not be clamped"
    )


def test_step_windows_agree_with_html_and_mp4_chapters(tmp_path):
    """Export chapter windows must equal the HTML player's step windows and
    the produced MP4's duration (chapter agreement HTML ↔ MP4)."""
    import base64
    import shutil
    import subprocess

    from playwright.sync_api import sync_playwright

    from capturd.walk.exporter import export_video
    from capturd.walk.viewer import render_viewer_to_file

    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    src_dir = __import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "visual-regressions"
    shots = []
    for name in ("scroll-source-0.png", "scroll-source-1.png"):
        p = fixtures / name
        shutil.copy(src_dir / name, p)
        shots.append(base64.b64encode(p.read_bytes()).decode("ascii"))

    steps = [
        _make_step(0, "#cta", bounding_rect={"x": 400, "y": 300, "width": 200, "height": 80}),
        _make_step(1, "body", bounding_rect={"x": 0, "y": 0, "width": 1024, "height": 768}),
    ]
    for s, shot in zip(steps, shots):
        s["screenshotBase64"] = shot
    spec = _make_spec(steps, style="snappy", viewport={"width": 1024, "height": 768})

    viewer = tmp_path / "viewer.html"
    render_viewer_to_file(spec, viewer)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1024, "height": 768})
        page.goto(viewer.resolve().as_uri(), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.__demoExport && window.__demoViewer && window.__demoViewer.STATE.spec",
            timeout=15000,
        )
        plan = page.evaluate("() => window.__demoExport.prepare()")
        html_windows = page.evaluate(
            "() => [0, 1].map(i => window.__demoViewer.stepDwellMs(i) + 300)"
        )
        browser.close()

    wins = plan["stepWindows"]
    assert [w["tEnd"] - w["tStart"] for w in wins] == html_windows, (
        "export chapter windows must equal the HTML player's step windows"
    )
    # Adjacent windows are contiguous and the tail closes the timeline.
    assert wins[0]["tStart"] == 0
    assert wins[1]["tStart"] == wins[0]["tEnd"]
    assert plan["totalMs"] == wins[-1]["tEnd"] + 950  # TAIL

    out = tmp_path / "chapters.mp4"
    export_video(spec, viewer, out, fmt="mp4", fps=8)
    assert out.is_file()

    ffprobe = shutil.which("ffprobe")
    assert ffprobe, "ffprobe required for chapter agreement"
    dur_s = float(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.strip())
    assert abs(dur_s * 1000 - plan["totalMs"]) <= 400, (
        f"MP4 duration {dur_s}s disagrees with HTML chapters {plan['totalMs']}ms"
    )
