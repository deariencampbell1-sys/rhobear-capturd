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
