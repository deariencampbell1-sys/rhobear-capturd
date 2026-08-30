"""Playwright tests for the viewer event contract (`capturd:event`).

Verifies the Phase-1 normalized event vocabulary:

- boot emits demo_open + step_view(step-0) with the full payload shape
- play/pause/resume emit demo_start (once) / viewer_pause / viewer_resume
- navigation emits step_complete (departed step) + step_view / step_replay
- natural playback end emits demo_complete exactly once
- export mode suppresses all telemetry (the export renderer owns the DOM)
- the vocabulary guard rejects unknown event names
- the viewer + event contract work fully offline (no network requests)

Core never sends network traffic — the contract is local CustomEvents on
`window` plus the window.__capturdEvents ring buffer. A host listens.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _placeholder_screenshot_b64() -> str:
    """Minimal valid 1×1 white PNG, base64 (no real image needed)."""
    return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="


def _step(i: int, label: str) -> dict:
    return {
        "index": i, "timestamp": i * 1000,
        "pageUrl": "https://example.com/", "pageTitle": label,
        "interaction": {
            "type": "click",
            "target": {
                "selector": f"#btn-{i}",
                "tagName": "button",
                "text": label,
                "boundingRect": {"x": 380, "y": 320, "width": 264, "height": 64},
            },
            "hotspot": {"xPct": 50, "yPct": 50},
        },
        "annotation": f"Step {i}: {label}",
        "screenshotBase64": _placeholder_screenshot_b64(),
    }


@pytest.fixture
def viewer_html_path(tmp_path) -> Path:
    """Render the viewer with a 3-step synthetic DemoSpec."""
    from capturd.walk.viewer import render_viewer_to_file

    spec = {
        "version": 1,
        "id": "events-test-demo",
        "name": "Event Contract Test Demo",
        "goal": "Verify the capturd:event contract",
        "createdAt": "2026-08-29T00:00:00Z",
        "viewport": {"width": 1024, "height": 768},
        "startUrl": "https://example.com",
        "steps": [_step(0, "First"), _step(1, "Second"), _step(2, "Third")],
    }

    out = tmp_path / "viewer.html"
    render_viewer_to_file(spec, out)
    assert out.is_file()
    return out


@pytest.fixture
def page(viewer_html_path: Path):
    """Headless Chromium on the rendered viewer with event collection."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(viewer_html_path.as_uri())
        pg.wait_for_function("window.__demoViewer && window.__capturdEvents")
        yield pg
        browser.close()


def _events(page) -> list[dict]:
    return page.evaluate("window.__capturdEvents")


def _names(page) -> list[str]:
    return [e["event"] for e in _events(page)]


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_boot_emits_demo_open_and_first_step_view(page):
    """demo_open then step_view(step-0), with the full normalized payload."""
    names = _names(page)
    assert names == ["demo_open", "step_view"], f"boot events wrong: {names}"

    ev = _events(page)[0]
    assert ev["demoId"] == "events-test-demo"
    assert ev["versionId"] == 1
    assert isinstance(ev["sessionId"], str) and len(ev["sessionId"]) >= 8
    assert ev["stepId"] is None
    assert ev["branchId"] is None
    assert isinstance(ev["elapsedMs"], int) and ev["elapsedMs"] >= 0
    assert ev["timestamp"].endswith("Z") or "+" in ev["timestamp"] or "T" in ev["timestamp"]
    dev = ev["device"]
    assert dev["deviceClass"] in ("mobile", "desktop")
    assert dev["viewportW"] == 1280 and dev["viewportH"] == 900
    assert isinstance(dev["reducedMotion"], bool)

    sv = _events(page)[1]
    assert sv["stepId"] == "step-0"
    # Same session across events.
    assert sv["sessionId"] == ev["sessionId"]


@pytest.mark.slow
def test_payload_carries_branch_and_choice_fields_when_provided(page):
    """branchId/choiceId/ctaId ride along when the caller supplies them."""
    payload = page.evaluate(
        "window.__demoViewer.emitEvent('branch_select',"
        " {stepIndex: 1, branchId: 'branch-a', choiceId: 'choice-automation'})"
    )
    assert payload["branchId"] == "branch-a"
    assert payload["choiceId"] == "choice-automation"
    assert payload["stepId"] == "step-1"


# ---------------------------------------------------------------------------
# Lifecycle ordering
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_play_pause_resume_ordering(page):
    """demo_start once, then viewer_pause / viewer_resume alternate."""
    page.evaluate("window.__demoViewer.play()")
    page.wait_for_timeout(80)
    page.evaluate("window.__demoViewer.pause()")
    page.wait_for_timeout(80)
    page.evaluate("window.__demoViewer.play()")
    page.wait_for_timeout(80)

    names = _names(page)
    assert names.count("demo_start") == 1, f"demo_start must fire once: {names}"
    assert names.count("viewer_pause") == 1
    assert names.count("viewer_resume") == 1
    # Ordering: demo_open < step_view < demo_start < viewer_pause < viewer_resume
    assert names == [
        "demo_open", "step_view", "demo_start", "viewer_pause", "viewer_resume",
    ]


@pytest.mark.slow
def test_navigation_view_complete_replay(page):
    """Forward nav completes the departed step + views the next; backward replays."""
    page.evaluate("window.__demoViewer.next()")
    page.wait_for_timeout(700)  # TRANSITION_MS + margin
    page.evaluate("window.__demoViewer.prev()")
    page.wait_for_timeout(700)

    seq = [(e["event"], e["stepId"]) for e in _events(page)]
    assert ("step_complete", "step-0") in seq
    assert ("step_view", "step-1") in seq
    assert ("step_complete", "step-1") in seq
    # Returning to step-0 is a replay, NOT a second step_view.
    assert ("step_replay", "step-0") in seq
    assert [s for s in seq if s[0] == "step_view"].count(("step_view", "step-0")) == 1


@pytest.mark.slow
def test_demo_complete_fires_once_at_natural_end(page):
    """Playing through the last step fires demo_complete exactly once."""
    page.evaluate("window.__demoViewer.goToStep(2, 1, true)")
    page.wait_for_timeout(700)
    page.evaluate("window.__demoViewer.play()")
    # play() at the last step loops to step 0 (product behavior), so full
    # completion = 3 step dwells (~1.9s each) + 3 transitions (~0.3s each).
    page.wait_for_timeout(9500)

    completes = [e for e in _events(page) if e["event"] == "demo_complete"]
    assert len(completes) == 1, f"demo_complete expected once: {[e['event'] for e in _events(page)]}"
    assert completes[0]["stepId"] == "step-2"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_vocabulary_guard_rejects_unknown_events(page):
    """emitEvent drops names outside the contract vocabulary."""
    n_before = len(_events(page))
    result = page.evaluate("window.__demoViewer.emitEvent('made_up_event', {})")
    assert result is None
    assert len(_events(page)) == n_before


@pytest.mark.slow
def test_export_mode_suppresses_events(page):
    """The deterministic export renderer owns the DOM — no telemetry."""
    page.evaluate("window.__demoViewer.STATE.exportMode = true")
    n_before = len(_events(page))
    page.evaluate("window.__demoViewer.next()")
    page.evaluate("window.__demoViewer.pause()")
    page.evaluate("window.__demoViewer.emitEvent('step_view', {stepIndex: 1})")
    assert len(_events(page)) == n_before, "no events may fire in export mode"


# ---------------------------------------------------------------------------
# Offline resilience
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_viewer_and_events_fully_offline(viewer_html_path: Path):
    """Block ALL network (http/https/ws) — the self-contained viewer still
    boots and the event contract still fires. Core never needs the network."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})

        blocked = []
        def _block(route):
            blocked.append(route.request().url)
            route.abort()
        # Abort every non-file scheme request.
        pg.route("http://**", _block)
        pg.route("https://**", _block)
        pg.route("ws://**", _block)

        pg.goto(viewer_html_path.as_uri())
        pg.wait_for_function("window.__demoViewer && window.__capturdEvents")
        pg.evaluate("window.__demoViewer.play()")
        pg.wait_for_timeout(120)

        names = _names(pg)
        assert names[:2] == ["demo_open", "step_view"]
        assert "demo_start" in names
        # Nothing tried to phone home.
        assert blocked == [], f"viewer made network requests: {blocked}"
        browser.close()
