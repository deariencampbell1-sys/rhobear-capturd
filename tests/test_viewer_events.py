"""Local viewer event contract (`capturd:event`) — neutral extraction.

The viewer emits normalized LOCAL events only: a `capturd:event` CustomEvent
on `window` plus the bounded `window.__capturdEvents` ring buffer. Nothing is
transmitted — every test in this module runs with external network blocked.

Covers:
- boot emits demo_open + step_view(step-0) with the full payload shape
- play/pause/resume emit demo_start (once) / viewer_pause / viewer_resume
- navigation emits step_complete (departed step) + step_view / step_replay
- natural playback end emits demo_complete exactly once
- branch/choice fields ride along when supplied
- duplicate eventIds are deduped in the ring buffer, stream order preserved
- export mode suppresses host events
- the vocabulary guard rejects unknown event names (emit and record paths)
- the viewer + event contract work fully offline
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "neutral-v2"

EXPECTED_VOCABULARY = [
    "demo_open", "demo_start",
    "step_view", "step_complete", "step_replay",
    "branch_view", "branch_select",
    "demo_complete", "demo_exit",
    "viewer_pause", "viewer_resume",
]

#: Every key a boot-time event may carry — no identity, token or URL data.
BOOT_PAYLOAD_KEYS = {
    "event", "eventId", "demoId", "versionId", "sessionId",
    "stepId", "branchId", "elapsedMs", "timestamp", "device",
}


def _spec() -> dict:
    return json.loads((FIXTURES / "spec_plain.json").read_text(encoding="utf-8"))


@pytest.fixture
def viewer_html_path(tmp_path) -> Path:
    """Render the viewer with the 3-step plain fixture spec."""
    from capturd.walk.viewer import render_viewer_to_file

    out = tmp_path / "viewer.html"
    render_viewer_to_file(_spec(), out)
    assert out.is_file()
    return out


# ---------------------------------------------------------------------------
# Static contract checks (no browser)
# ---------------------------------------------------------------------------


def test_template_ships_the_event_vocabulary(viewer_html_path: Path):
    html = viewer_html_path.read_text(encoding="utf-8")
    m = re.search(r"const EVENT_VOCABULARY = \[(.*?)\];", html, re.S)
    assert m, "viewer template is missing the event vocabulary guard"
    names = re.findall(r"'([^']+)'", m.group(1))
    assert names == EXPECTED_VOCABULARY


def test_event_emitter_adds_no_network_calls(viewer_html_path: Path):
    """The only network call left in the viewer is the pre-existing
    relative-path `?demo=` loader; events never leave the page."""
    html = viewer_html_path.read_text(encoding="utf-8")
    assert html.count("fetch(") == 1
    for banned in ("sendBeacon", "XMLHttpRequest", "WebSocket("):
        assert banned not in html, f"viewer gained a network API: {banned}"


# ---------------------------------------------------------------------------
# Playwright fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def page(viewer_html_path: Path):
    """Headless Chromium on the rendered viewer with external network
    blocked and event collection enabled."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        for pattern in ("http://**", "https://**", "ws://**"):
            pg.route(pattern, lambda route: route.abort())
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
    assert set(ev) == BOOT_PAYLOAD_KEYS, sorted(ev)
    assert ev["event"] == "demo_open"
    assert ev["demoId"] == "neutral-plain"
    assert ev["versionId"] == 1
    assert isinstance(ev["eventId"], str) and ev["eventId"]
    assert isinstance(ev["sessionId"], str) and len(ev["sessionId"]) >= 8
    assert ev["stepId"] is None
    assert ev["branchId"] is None
    assert isinstance(ev["elapsedMs"], int) and ev["elapsedMs"] >= 0
    assert "T" in ev["timestamp"]
    dev = ev["device"]
    assert dev["deviceClass"] in ("mobile", "desktop")
    assert dev["viewportW"] == 1280 and dev["viewportH"] == 900
    assert isinstance(dev["reducedMotion"], bool)

    sv = _events(page)[1]
    assert sv["event"] == "step_view"
    assert sv["stepId"] == "step-0"
    assert sv["sessionId"] == ev["sessionId"]  # one session across events


@pytest.mark.slow
def test_payload_carries_branch_and_choice_fields_when_provided(page):
    payload = page.evaluate(
        "window.__demoViewer.emitEvent('branch_select',"
        " {stepIndex: 1, branchId: 'branch-a', choiceId: 'choice-automation'})"
    )
    assert payload["branchId"] == "branch-a"
    assert payload["choiceId"] == "choice-automation"
    assert payload["stepId"] == "step-1"
    assert payload["event"] == "branch_select"
    # No identity, token or URL data rides along.
    for banned in ("prospect", "email", "token", "url"):
        assert banned not in payload


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
    ], names


@pytest.mark.slow
def test_navigation_view_complete_replay(page):
    """Forward nav completes the departed step + views the next; backward
    replays instead of viewing the same step twice."""
    page.evaluate("window.__demoViewer.next()")
    page.wait_for_timeout(700)  # TRANSITION_MS + margin
    page.evaluate("window.__demoViewer.prev()")
    page.wait_for_timeout(700)

    seq = [(e["event"], e["stepId"]) for e in _events(page)]
    assert ("step_complete", "step-0") in seq, seq
    assert ("step_view", "step-1") in seq, seq
    assert ("step_complete", "step-1") in seq, seq
    assert ("step_replay", "step-0") in seq, seq
    views0 = [s for s in seq if s == ("step_view", "step-0")]
    assert len(views0) == 1, seq


@pytest.mark.slow
def test_demo_complete_fires_once_at_natural_end(page):
    """Playing through the run fires demo_complete exactly once."""
    page.evaluate("window.__demoViewer.goToStep(2, 1, true)")
    page.wait_for_timeout(700)
    page.evaluate("window.__demoViewer.play()")
    # play() at the last step restarts from 0 (product behavior), so the run
    # is 3 step dwells (~1.9s each) + 3 transitions (~0.3s each).
    page.wait_for_timeout(9500)

    completes = [e for e in _events(page) if e["event"] == "demo_complete"]
    assert len(completes) == 1, \
        [e["event"] for e in _events(page)]
    assert completes[0]["stepId"] == "step-2"


# ---------------------------------------------------------------------------
# Exit signal (pagehide)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_pagehide_emits_demo_exit_once(page):
    """One pagehide → exactly one demo_exit with the vocabulary payload;
    a second pagehide without an intervening pageshow (bfcache double fire)
    never records a second exit for the same hide — and a real page show
    arms the next one (one demo_exit per page show)."""
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    exits = [e for e in _events(page) if e["event"] == "demo_exit"]
    assert len(exits) == 1, [e["event"] for e in _events(page)]
    ev = exits[0]
    assert set(ev) == BOOT_PAYLOAD_KEYS, sorted(ev)
    assert ev["demoId"] == "neutral-plain"
    assert ev["versionId"] == 1
    assert ev["stepId"] is None
    assert ev["branchId"] is None
    assert "choiceId" not in ev
    assert isinstance(ev["eventId"], str) and ev["eventId"]
    assert isinstance(ev["sessionId"], str) and len(ev["sessionId"]) >= 8
    assert isinstance(ev["elapsedMs"], int) and ev["elapsedMs"] >= 0
    assert "T" in ev["timestamp"]
    assert ev["device"]["deviceClass"] in ("mobile", "desktop")

    # Edge case 4 — pagehide fired twice for one page show: still one exit.
    page.wait_for_timeout(5)  # cross a wall-clock ms so dedupe can't hide it
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    exits = [e for e in _events(page) if e["event"] == "demo_exit"]
    assert len(exits) == 1, [e["event"] for e in _events(page)]

    # ...and a real page show arms the next hide: one exit per page show.
    page.evaluate("window.dispatchEvent(new Event('pageshow'))")
    page.wait_for_timeout(5)
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    exits = [e for e in _events(page) if e["event"] == "demo_exit"]
    assert len(exits) == 2, [e["event"] for e in _events(page)]


# ---------------------------------------------------------------------------
# Dedup — a stream with duplicate eventIds
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_duplicate_event_ids_are_deduped_order_preserved(page):
    result = page.evaluate(
        """() => {
          window.__capturdEvents = [];
          const V = window.__demoViewer;
          const e1 = V.emitEvent('demo_open', {});
          const e2 = V.emitEvent('step_view', { stepIndex: 0 });
          const dup = V.recordEvent(JSON.parse(JSON.stringify(e1)));
          const e3 = V.emitEvent('branch_view', { stepIndex: 1, branchId: 'b1' });
          const bad = V.recordEvent({ event: 'made_up_event', eventId: 'x' });
          return {
            ids: window.__capturdEvents.map(e => e.eventId),
            names: window.__capturdEvents.map(e => e.event),
            e1: e1.eventId, e2: e2.eventId, e3: e3.eventId,
            dup: dup, bad: bad,
          };
        }"""
    )
    # The duplicate id was dropped — first occurrence wins.
    assert result["dup"] is None
    # Unknown names are rejected on the record path too.
    assert result["bad"] is None
    # Order of the rest is preserved, exactly one copy of each id.
    assert result["ids"] == [result["e1"], result["e2"], result["e3"]]
    assert result["names"] == ["demo_open", "step_view", "branch_view"]
    assert len(set(result["ids"])) == 3


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
    """The deterministic export renderer owns the DOM — no host events."""
    page.evaluate("window.__demoViewer.STATE.exportMode = true")
    n_before = len(_events(page))
    page.evaluate("window.__demoViewer.next()")
    page.wait_for_timeout(400)
    page.evaluate("window.__demoViewer.pause()")
    page.evaluate("window.__demoViewer.emitEvent('step_view', {stepIndex: 1})")
    page.evaluate(
        "window.__demoViewer.recordEvent("
        "{event: 'step_view', eventId: 'export-dup'})")
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

        blocked: list[str] = []

        def _block(route):
            blocked.append(route.request().url)
            route.abort()

        pg.route("http://**", _block)
        pg.route("https://**", _block)
        pg.route("ws://**", _block)

        pg.goto(viewer_html_path.as_uri())
        pg.wait_for_function("window.__demoViewer && window.__capturdEvents")
        pg.evaluate("window.__demoViewer.play()")
        pg.wait_for_timeout(150)

        names = _names(pg)
        assert names[:2] == ["demo_open", "step_view"], names
        assert "demo_start" in names, names
        assert blocked == [], f"viewer made network requests: {blocked}"
        browser.close()
