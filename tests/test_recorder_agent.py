"""W1 integration tests — agent-driven recording + content-mode detection.

These are REAL Playwright tests (launch a browser, navigate local HTML
fixtures, exercise the recorder code). They are NOT mock/smoke tests.

Test C requires RHOBEAR_GW_API_KEY in the environment; it is skipped
when the key is absent.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _fixture_url(name: str) -> str:
    p = FIXTURES_DIR / name
    assert p.is_file(), f"fixture missing: {p}"
    return f"file://{p.resolve()}"


# ---------------------------------------------------------------------------
# Test A — canvas page (80% viewport → video mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_mode_video_canvas_80pct() -> None:
    """Canvas filling 80% of viewport → contentMode == 'video', canvasAreaPct >= 30."""
    from capturd.walk.recorder import _detect_content_mode, _classify_content_mode
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, channel="chrome")
    page = await browser.new_page(viewport={"width": 1280, "height": 720})
    await page.goto(_fixture_url("canvas-page.html"), wait_until="domcontentloaded")
    # Let the canvas render.
    await asyncio.sleep(0.5)

    cm = await _detect_content_mode(page)
    mode = _classify_content_mode(cm)

    # Assertions
    assert cm.hasCanvas is True, f"expected hasCanvas=True, got {cm}"
    assert cm.canvasAreaPct >= 30, f"canvasAreaPct={cm.canvasAreaPct:.1f}%, expected >= 30"
    assert mode == "video", f"expected video mode, got {mode}"

    await browser.close()
    await pw.stop()


# ---------------------------------------------------------------------------
# Test B — mixed page (form + 15% canvas → hybrid mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_mode_hybrid_mixed_page() -> None:
    """Form + 15% canvas → contentMode == 'hybrid'."""
    from capturd.walk.recorder import _detect_content_mode, _classify_content_mode
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, channel="chrome")
    page = await browser.new_page(viewport={"width": 1280, "height": 720})
    await page.goto(_fixture_url("mixed-page.html"), wait_until="domcontentloaded")
    await asyncio.sleep(0.5)

    cm = await _detect_content_mode(page)
    mode = _classify_content_mode(cm)

    # Assertions
    assert cm.hasCanvas is True, f"expected hasCanvas=True, got {cm}"
    assert 0 < cm.canvasAreaPct < 30, (
        f"canvasAreaPct={cm.canvasAreaPct:.1f}%, expected 0 < pct < 30"
    )
    assert mode == "hybrid", f"expected hybrid mode, got {mode}"

    await browser.close()
    await pw.stop()


# ---------------------------------------------------------------------------
# Test C — agent-record a 2-step flow (Sign Up → Confirm)
# ---------------------------------------------------------------------------


_HAS_GW_KEY = bool(os.environ.get("RHOBEAR_GW_API_KEY", "").strip())


@pytest.mark.skipif(
    not _HAS_GW_KEY,
    reason="RHOBEAR_GW_API_KEY not set — LLM call would fail",
)
@pytest.mark.asyncio
async def test_agent_record_signup_flow(tmp_path) -> None:
    """Agent drives a 2-click flow: Sign Up → Confirm. Asserts correct steps."""
    from capturd.walk.recorder import DemoRecorder

    url = _fixture_url("signup-flow.html")
    out = tmp_path / "demos" / "test-agent"

    recorder = DemoRecorder(
        session_id="test-agent",
        url=url,
        name="Sign Up Flow",
        goal="Click 'Sign Up' then 'Confirm' to complete account creation.",
        viewport={"width": 1280, "height": 720},
        output_dir=out,
    )

    spec = await recorder.agent_record()

    # Must have steps.
    assert len(spec.steps) >= 1, f"expected >= 1 steps, got {len(spec.steps)}"

    # Check step types — all should be 'click'.
    click_steps = [s for s in spec.steps if s.interaction.type == "click"]
    assert len(click_steps) >= 1, (
        f"expected >= 1 click steps, got {len(click_steps)}"
    )

    # Content metadata should be populated on steps.
    for step in spec.steps:
        assert step.contentMetadata is not None, (
            f"step {step.index}: contentMetadata is None"
        )
        assert isinstance(step.contentMetadata, dict), (
            f"step {step.index}: contentMetadata is not a dict"
        )

    # Screenshots should exist on disk.
    for step in spec.steps:
        if step.screenshotPath:
            shot = out.parent / step.screenshotPath
            assert shot.is_file(), f"screenshot missing: {shot}"

    # Demo JSON was written.
    assert (out / "demo.json").is_file(), "demo.json missing"


# ---------------------------------------------------------------------------
# Test D — content-mode detection on pure DOM page (no canvas → dom mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_mode_dom_no_canvas() -> None:
    """Pure HTML page with no canvas → contentMode == 'dom'."""
    from capturd.walk.recorder import _detect_content_mode, _classify_content_mode
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, channel="chrome")
    page = await browser.new_page(viewport={"width": 1280, "height": 720})
    await page.goto(_fixture_url("signup-flow.html"), wait_until="domcontentloaded")
    await asyncio.sleep(0.5)

    cm = await _detect_content_mode(page)
    mode = _classify_content_mode(cm)

    assert cm.hasCanvas is False
    assert cm.canvasAreaPct == 0.0
    assert mode == "dom", f"expected dom mode, got {mode}"

    await browser.close()
    await pw.stop()


# ---------------------------------------------------------------------------
# Test E — _parse_agent_reply unit tests
# ---------------------------------------------------------------------------


def test_parse_agent_reply_click() -> None:
    from capturd.walk.recorder import _parse_agent_reply

    action, sel, val = _parse_agent_reply(
        '{"action": "click", "selector": "#signup-btn"}'
    )
    assert action == "click"
    assert sel == "#signup-btn"
    assert val is None


def test_parse_agent_reply_input() -> None:
    from capturd.walk.recorder import _parse_agent_reply

    action, sel, val = _parse_agent_reply(
        '{"action":"input","selector":"#username","value":"alice"}'
    )
    assert action == "input"
    assert sel == "#username"
    assert val == "alice"


def test_parse_agent_reply_navigate() -> None:
    from capturd.walk.recorder import _parse_agent_reply

    action, sel, val = _parse_agent_reply(
        '{"action":"navigate","selector":"body","value":"https://example.com/next"}'
    )
    assert action == "navigate"
    assert val == "https://example.com/next"


def test_parse_agent_reply_done() -> None:
    from capturd.walk.recorder import _parse_agent_reply

    action, sel, val = _parse_agent_reply(
        '{"action": "done", "selector": ""}'
    )
    assert action == "done"


def test_parse_agent_reply_with_fences() -> None:
    from capturd.walk.recorder import _parse_agent_reply

    action, sel, val = _parse_agent_reply(
        '```json\n{"action": "click", "selector": "#btn"}\n```'
    )
    assert action == "click"
    assert sel == "#btn"


def test_parse_agent_reply_garbage() -> None:
    from capturd.walk.recorder import _parse_agent_reply

    action, sel, val = _parse_agent_reply("just some prose, no JSON here")
    assert action is None
    assert sel is None
    assert val is None


def test_parse_agent_reply_empty() -> None:
    from capturd.walk.recorder import _parse_agent_reply

    action, sel, val = _parse_agent_reply("")
    assert action is None


# ---------------------------------------------------------------------------
# Test F — _classify_content_mode threshold edges
# ---------------------------------------------------------------------------


def test_classify_content_mode_video_threshold() -> None:
    from capturd.walk.recorder import _classify_content_mode
    from capturd.walk.schema import ContentMetadata

    cm = ContentMetadata(hasCanvas=True, canvasAreaPct=30.0)
    assert _classify_content_mode(cm) == "video"

    cm = ContentMetadata(hasCanvas=True, canvasAreaPct=50.0)
    assert _classify_content_mode(cm) == "video"


def test_classify_content_mode_hybrid() -> None:
    from capturd.walk.recorder import _classify_content_mode
    from capturd.walk.schema import ContentMetadata

    cm = ContentMetadata(hasCanvas=True, canvasAreaPct=15.0)
    assert _classify_content_mode(cm) == "hybrid"

    cm = ContentMetadata(hasCanvas=True, canvasAreaPct=0.1)
    assert _classify_content_mode(cm) == "hybrid"


def test_classify_content_mode_dom() -> None:
    from capturd.walk.recorder import _classify_content_mode
    from capturd.walk.schema import ContentMetadata

    cm = ContentMetadata(hasCanvas=False, canvasAreaPct=0.0)
    assert _classify_content_mode(cm) == "dom"


# ---------------------------------------------------------------------------
# W1-03-r1 — semantic target metadata + scroll integrity
# ---------------------------------------------------------------------------


def _tmp_page(tmp_path: Path, body_html: str) -> str:
    p = tmp_path / "page.html"
    p.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>t</title>"
        f"</head><body>{body_html}</body></html>",
        encoding="utf-8",
    )
    return f"file://{p.resolve()}"


async def _recording_session(url: str, tmp_path: Path, viewport=None):
    from capturd.walk.recorder import DemoRecorder

    rec = DemoRecorder(
        session_id="w1-03-r1",
        url=url,
        name="W1-03-r1",
        goal="semantic target metadata",
        viewport=viewport or {"width": 1280, "height": 720},
        output_dir=tmp_path / "demos" / "w1-03-r1",
        headful=False,
    )
    await rec.start()
    try:
        # Deterministic readiness instead of a fixed sleep: the installed flag
        # alone is set BEFORE the click bridge attaches and the HUD badge
        # exists, so wait for the connected badge + both exposed functions,
        # then give the installed overlay two animation frames to paint.
        await rec._page.wait_for_function(
            """() => {
                const badge = document.getElementById('__demo-recorder-indicator');
                return !!window.__demoRecorderInstalled
                    && !!badge && badge.isConnected
                    && typeof window.recordClick === 'function'
                    && typeof window.__demoRecorderSetStepCount === 'function';
            }""",
            timeout=5000,
        )
        await rec._page.evaluate(
            "() => new Promise((resolve) => {"
            "  requestAnimationFrame(() => {"
            "    requestAnimationFrame(() => resolve());"
            "  });"
            "})"
        )
    except Exception:
        await rec._stop_async()
        raise
    return rec


@pytest.mark.asyncio
async def test_recorded_target_carries_semantic_metadata(tmp_path) -> None:
    """Click-recorded targets carry role, source viewport, captureSpace and
    region alongside the legacy selector/text/rect fields."""
    url = _tmp_page(
        tmp_path,
        "<button id='go' style='margin:40px;width:220px;height:64px'>Go</button>",
    )
    rec = await _recording_session(url, tmp_path)
    try:
        await rec._act_async("click", "#go", None, None)
    finally:
        await rec._stop_async()

    assert len(rec.spec.steps) >= 1
    target = rec.spec.steps[-1].interaction.target
    for key in ("selector", "tagName", "text", "boundingRect"):
        assert key in target, f"legacy field {key} lost"
    assert target["selector"] == "#go"
    assert target["role"] in ("button", "Button"), target
    assert target["viewport"] == {"width": 1280, "height": 720, "deviceScaleFactor": 1}
    assert target["captureSpace"] == "viewport"
    assert target["region"] == "meaningful", target


@pytest.mark.asyncio
async def test_scroll_refused_when_page_cannot_scroll(tmp_path) -> None:
    """A page that cannot scroll refuses the scroll action instead of
    recording identical screenshots — and nothing is recorded for it."""
    from capturd.walk.recorder import DemoRecorderError

    url = _tmp_page(tmp_path, "<p style='margin:20px'>no scroll here</p>")
    rec = await _recording_session(url, tmp_path)
    try:
        with pytest.raises(DemoRecorderError):
            await rec._act_async("scroll", None, "down", None)
    finally:
        await rec._stop_async()

    assert len(rec.spec.steps) == 0, (
        f"refused scroll must not record a step: {rec.spec.steps}"
    )


@pytest.mark.asyncio
async def test_consecutive_scroll_steps_have_different_screenshots(tmp_path) -> None:
    """Two scrolls on a scrollable page yield two steps whose screenshots
    differ — no identical consecutive scroll frames."""
    url = _tmp_page(
        tmp_path,
        "<div style='height:400px;background:#cfe'>top</div>"
        "<div style='height:400px;background:#efc'>mid</div>"
        "<div style='height:400px;background:#fce'>bottom</div>",
    )
    rec = await _recording_session(url, tmp_path, viewport={"width": 800, "height": 400})
    try:
        await rec._act_async("scroll", None, "500", None)
        await rec._act_async("scroll", None, "500", None)
    finally:
        await rec._stop_async()

    steps = [s for s in rec.spec.steps if s.interaction.type == "scroll"]
    assert len(steps) == 2, f"expected 2 scroll steps, got {rec.spec.steps}"
    shots = []
    for s in steps:
        assert s.screenshotPath, f"scroll step missing screenshot: {s}"
        shot = rec.output_dir.parent / s.screenshotPath
        assert shot.is_file(), shot
        shots.append(shot.read_bytes())
    assert shots[0] != shots[1], (
        "consecutive scroll steps recorded identical screenshots"
    )
    for s in steps:
        assert s.interaction.target["captureSpace"] == "viewport"
        assert s.interaction.target["region"] in ("body", "full_viewport")


# ---------------------------------------------------------------------------
# W1-03-r2 — scrolling inside an SPA pane and valid edge no-ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scroll_inside_nested_pane_preserves_window_offset(tmp_path) -> None:
    """Wheel a scrollable app pane, even though the document itself is fixed."""
    url = _tmp_page(
        tmp_path,
        "<style>html,body{margin:0;height:100%;overflow:hidden}</style>"
        "<div id='pane' style='position:absolute;left:40px;top:40px;"
        "width:330px;height:200px;overflow:auto'>"
        "<div style='height:900px;background:linear-gradient(red,blue)'>"
        "inside scroll pane</div></div>",
    )
    rec = await _recording_session(url, tmp_path, viewport={"width": 800, "height": 400})
    try:
        before = await rec._page.evaluate(
            "() => ({inner: document.querySelector('#pane').scrollTop, outer: window.scrollY})"
        )
        assert before == {"inner": 0, "outer": 0}
        await rec._act_async("scroll", "#pane", "down", None)
        after = await rec._page.evaluate(
            "() => ({inner: document.querySelector('#pane').scrollTop, outer: window.scrollY})"
        )
        assert after["inner"] > 0, after
        assert after["outer"] == 0, after
        assert len(rec.spec.steps) == 1
        assert rec.spec.steps[0].interaction.type == "scroll"
    finally:
        await rec._stop_async()


@pytest.mark.asyncio
async def test_scroll_top_and_bottom_at_document_edges_are_valid(tmp_path) -> None:
    """Already-at-edge top/bottom actions are valid, even at zero delta."""
    url = _tmp_page(
        tmp_path,
        "<div style='height:1600px;background:linear-gradient(red,blue)'>long page</div>",
    )
    rec = await _recording_session(url, tmp_path, viewport={"width": 800, "height": 400})
    try:
        await rec._act_async("scroll", "body", "top", None)
        assert await rec._page.evaluate("() => window.scrollY") == 0
        await rec._page.evaluate(
            "() => window.scrollTo(0, document.scrollingElement.scrollHeight)"
        )
        at_bottom = await rec._page.evaluate(
            "() => document.scrollingElement.scrollTop"
        )
        assert at_bottom > 0
        await rec._act_async("scroll", "body", "bottom", None)
        assert await rec._page.evaluate(
            "() => document.scrollingElement.scrollTop"
        ) == at_bottom
        assert len(rec.spec.steps) == 2
        assert all(s.interaction.type == "scroll" for s in rec.spec.steps)
    finally:
        await rec._stop_async()


@pytest.mark.asyncio
async def test_scroll_nested_pane_top_and_bottom_edges(tmp_path) -> None:
    """Top/bottom commands respect an explicitly selected nested scroll pane."""
    url = _tmp_page(
        tmp_path,
        "<style>body{overflow:hidden}</style>"
        "<div id='pane' style='height:180px;width:320px;overflow-y:auto'>"
        "<div style='height:1000px'>long pane</div></div>",
    )
    rec = await _recording_session(url, tmp_path, viewport={"width": 800, "height": 400})
    try:
        await rec._act_async("scroll", "#pane", "top", None)
        assert await rec._page.evaluate(
            "() => document.querySelector('#pane').scrollTop"
        ) == 0
        await rec._act_async("scroll", "#pane", "bottom", None)
        at_bottom = await rec._page.evaluate(
            "() => document.querySelector('#pane').scrollTop"
        )
        assert at_bottom > 0
        await rec._act_async("scroll", "#pane", "bottom", None)
        assert await rec._page.evaluate(
            "() => document.querySelector('#pane').scrollTop"
        ) == at_bottom
        assert len(rec.spec.steps) == 3
    finally:
        await rec._stop_async()


# ---------------------------------------------------------------------------
# W1-03-r3 — wheel movement governs refusal; run_async guard coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface,edge",
    [("document", "top"), ("document", "bottom"),
     ("pane", "top"), ("pane", "bottom")],
)
@pytest.mark.asyncio
async def test_wheel_at_scroll_target_edge_is_refused(tmp_path, surface, edge) -> None:
    """A WHEEL whose target does not move is refused even on a scrollable
    target — including an outward wheel at the target's own edge. Explicit
    normalized top/bottom edge commands stay recorded no-ops; only wheel
    movement governs refusal."""
    from capturd.walk.recorder import DemoRecorderError

    if surface == "document":
        url = _tmp_page(
            tmp_path,
            "<div style='height:1600px;background:linear-gradient(red,blue)'>"
            "long page</div>",
        )
        selector = "body"
        target_js = "document.scrollingElement"
    else:
        url = _tmp_page(
            tmp_path,
            "<style>html,body{margin:0;height:100%;overflow:hidden}</style>"
            "<div id='pane' style='position:absolute;left:40px;top:40px;"
            "width:330px;height:200px;overflow:auto'>"
            "<div style='height:900px;background:linear-gradient(red,blue)'>"
            "inside scroll pane</div></div>",
        )
        selector = "#pane"
        target_js = "document.querySelector('#pane')"

    rec = await _recording_session(
        url, tmp_path, viewport={"width": 800, "height": 400}
    )
    try:
        # Put the ACTUAL wheel target at the requested edge and prove it has
        # more than one pixel of scroll range (a real, scrollable target).
        placed = await rec._page.evaluate(
            "(edge) => {"
            " const el = " + target_js + ";"
            " const range = el.scrollHeight - el.clientHeight;"
            " el.scrollTop = edge === 'top' ? 0 : el.scrollHeight;"
            " return {range: range, y: el.scrollTop};"
            "}",
            edge,
        )
        assert placed["range"] > 1, f"target needs scroll range: {placed}"
        y_before = await rec._page.evaluate(
            "() => (" + target_js + ").scrollTop"
        )
        with pytest.raises(
            DemoRecorderError,
            match="refusing to record an identical scroll screenshot",
        ):
            await rec._act_async(
                "scroll", selector, "up" if edge == "top" else "down", None
            )
        y_after = await rec._page.evaluate("() => (" + target_js + ").scrollTop")
        assert y_after == y_before, "refused wheel must not move the target"
        assert len(rec.spec.steps) == 0, (
            f"refused wheel must not record a step: {rec.spec.steps}"
        )
        if surface == "pane" and edge == "bottom":
            # Numeric zero is a wheel value too: same refusal, same count.
            with pytest.raises(
                DemoRecorderError,
                match="refusing to record an identical scroll screenshot",
            ):
                await rec._act_async("scroll", selector, "0", None)
            assert await rec._page.evaluate(
                "() => (" + target_js + ").scrollTop"
            ) == y_before
            assert len(rec.spec.steps) == 0
    finally:
        await rec._stop_async()


def test_run_async_preserves_started_runtime_error(monkeypatch) -> None:
    """An application RuntimeError raised by a coroutine that already started
    surfaces as the identical exception object — the CORO_CREATED guard
    re-raises instead of re-running it through asyncio.run."""
    from capturd.walk.recorder import run_async

    loop = asyncio.new_event_loop()
    started: list[int] = []
    err = RuntimeError("boom from started coroutine")

    async def coro():
        started.append(1)
        await asyncio.sleep(0)
        raise err

    runs: list[int] = []
    real_run = asyncio.run

    def counting_run(c, *args, **kwargs):
        runs.append(1)
        return real_run(c, *args, **kwargs)

    monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)
    monkeypatch.setattr(asyncio, "run", counting_run)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            run_async(coro())
        assert excinfo.value is err, "started-coroutine error must surface verbatim"
        assert str(excinfo.value) == "boom from started coroutine"
        assert started == [1], "the coroutine must run exactly once"
        assert runs == [], "the asyncio.run fallback must not re-run a started coroutine"
    finally:
        monkeypatch.undo()
        loop.close()


def test_run_async_falls_back_for_created_coroutine(monkeypatch) -> None:
    """A never-started coroutine against a closed loop runs exactly once
    through the asyncio.run fallback and returns its result."""
    from capturd.walk.recorder import run_async

    loop = asyncio.new_event_loop()
    loop.close()
    started: list[int] = []

    async def coro():
        started.append(1)
        return 37

    runs: list[int] = []
    real_run = asyncio.run

    def counting_run(c, *args, **kwargs):
        runs.append(1)
        return real_run(c, *args, **kwargs)

    monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)
    monkeypatch.setattr(asyncio, "run", counting_run)
    try:
        assert run_async(coro()) == 37
        assert started == [1], "the coroutine must run exactly once"
        assert len(runs) == 1, "exactly one asyncio.run fallback call"
    finally:
        monkeypatch.undo()
        loop.close()
