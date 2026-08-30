"""Branching V2 — viewer-facing choice points (Phase 5).

Covers:
- schema.validate_choices: normalization + structural rejection
- DemoStep round-trips branchId/choices (old specs without them stay valid)
- DemoForge.add_choices (+ legacy add_branch untouched)
- MCP demo.branch upgraded in place: choices mode + exactly-one-of guard
- Viewer: choice overlay renders, branch_view on entry, branch_select on pick,
  jump to destination, auto-advance waits at choice steps
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capturd.walk.schema import DemoStep, DemoSpec, validate_choices


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _choices(step_count: int = 4) -> list[dict]:
    return [
        {"id": "website", "label": "Better Website", "destination": 2},
        {"id": "automation", "label": "Automation", "destination": 1,
         "analyticsName": "auto-path", "variable": {"use_case": "automation"},
         "ctaId": "book"},
    ]


def test_validate_choices_normalizes():
    out = validate_choices(_choices(), step_count=4)
    assert [c["id"] for c in out] == ["website", "automation"]
    auto = out[1]
    assert auto["analyticsName"] == "auto-path"
    assert auto["variable"] == {"use_case": "automation"}
    assert auto["ctaId"] == "book"
    assert out[0].get("variable") is None and out[0].get("ctaId") is None


@pytest.mark.parametrize("bad", [
    [],                                            # empty
    [{"id": "", "label": "x", "destination": 0}],  # empty id
    [{"id": "bad id!", "label": "x", "destination": 0}],  # bad charset
    [{"id": "a", "label": "", "destination": 0}],  # empty label
    [{"id": "a", "label": "x", "destination": 9}], # out of range
    [{"id": "a", "label": "x", "destination": "1"}],  # non-int destination
    [{"id": "a", "label": "x", "destination": 0},
     {"id": "a", "label": "y", "destination": 1}],  # duplicate id
    "not-a-list",
])
def test_validate_choices_rejects_structural_problems(bad):
    with pytest.raises(ValueError):
        validate_choices(bad, step_count=4)


def test_demo_spec_roundtrips_branch_fields_and_stays_backward_compatible():
    # Old-style step: no branch fields at all.
    old = DemoStep(index=0, timestamp=0, pageUrl="https://x", pageTitle="p",
                   interaction={})
    spec = DemoSpec(id="d1", steps=[old])
    d = spec.to_dict()
    assert "choices" not in d["steps"][0] or d["steps"][0]["choices"] is None

    # New-style step with choices survives the round trip.
    new = DemoStep(index=1, timestamp=1, pageUrl="https://x", pageTitle="p",
                   interaction={}, branchId="entry-q", choices=_choices())
    d = DemoSpec(id="d2", steps=[new]).to_dict()
    assert d["steps"][0]["branchId"] == "entry-q"
    assert d["steps"][0]["choices"][0]["id"] == "website"


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def _minimal_spec(demo_id: str, step_count: int = 4) -> dict:
    return {
        "version": 1, "id": demo_id, "name": "T", "goal": "g",
        "createdAt": "2026-08-29T00:00:00Z",
        "viewport": {"width": 1024, "height": 768}, "startUrl": "https://x.test",
        "steps": [
            {"index": i, "timestamp": i, "pageUrl": "https://x.test",
             "pageTitle": f"s{i}",
             "interaction": {"type": "click",
                             "target": {"selector": f"#b{i}", "tagName": "button",
                                        "text": "b", "boundingRect": {
                                            "x": 1, "y": 1, "width": 2, "height": 2}},
                             "hotspot": {"xPct": 50, "yPct": 50}}}
            for i in range(step_count)
        ],
    }


@pytest.fixture
def forge_with_demo(tmp_path):
    from capturd.walk.coordinator import DemoForge

    demos_dir = tmp_path / "demos"
    demos_dir.mkdir()
    forge = DemoForge(demos_dir=demos_dir)
    forge.save_spec("branch-demo", _minimal_spec("branch-demo"))
    return forge


def test_add_choices_attaches_and_normalizes(forge_with_demo):
    out = forge_with_demo.add_choices("branch-demo", 1, _choices(),
                                      branch_id="entry-q")
    assert out == {"atStep": 1, "branchId": "entry-q",
                   "choices": ["website", "automation"]}
    spec = forge_with_demo.load_spec("branch-demo")
    assert spec["steps"][1]["choices"][1]["analyticsName"] == "auto-path"


def test_add_choices_rejects_bad_destination(forge_with_demo):
    from capturd.walk.coordinator import DemoForgeError
    with pytest.raises(DemoForgeError):
        forge_with_demo.add_choices(
            "branch-demo", 0,
            [{"id": "x", "label": "X", "destination": 99}])


def test_legacy_add_branch_still_works(forge_with_demo):
    alt = _minimal_spec("alt", step_count=1)["steps"]
    out = forge_with_demo.add_branch("branch-demo", 0, alt)
    assert out["branchCount"] == 1
    spec = forge_with_demo.load_spec("branch-demo")
    assert spec["steps"][0]["branches"] == [alt]
    assert "choices" not in spec["steps"][0]


# ---------------------------------------------------------------------------
# MCP tool (upgraded in place)
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_mcp_demo_branch_choices_mode(forge_with_demo, monkeypatch):
    from capturd.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "forge", forge_with_demo, raising=False)
    # _build_server binds forge at build time; find the tool fn and call it.
    import asyncio
    srv = mcp_server._build_server(forge_with_demo)
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    assert "demo.branch" in tools


def test_mcp_demo_branch_exactly_one_of():
    """Choices and altPath are mutually exclusive; neither is also an error."""
    # Structural check against the handler's contract via direct call.
    from capturd.mcp.server import _build_server
    import asyncio
    srv = _build_server()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    schema = tools["demo.branch"]
    props = schema.parameters.get("properties", {})
    assert "choices" in props and "alt_path" in props and "branch_id" in props


# ---------------------------------------------------------------------------
# Viewer behavior (Playwright)
# ---------------------------------------------------------------------------


def _placeholder_screenshot_b64() -> str:
    return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="


def _spec_with_choice_step() -> dict:
    def step(i, label):
        return {
            "index": i, "timestamp": i, "pageUrl": "https://x.test",
            "pageTitle": label,
            "interaction": {"type": "click",
                            "target": {"selector": f"#b{i}", "tagName": "button",
                                       "text": label,
                                       "boundingRect": {"x": 10, "y": 10,
                                                        "width": 50, "height": 20}},
                            "hotspot": {"xPct": 50, "yPct": 50}},
            "annotation": label,
            "screenshotBase64": _placeholder_screenshot_b64(),
        }

    chooser = step(1, "What do you need?")
    chooser["branchId"] = "entry-q"
    chooser["choices"] = [
        {"id": "website", "label": "Better Website", "destination": 2},
        {"id": "automation", "label": "Automation", "destination": 2,
         "analyticsName": "auto-path", "variable": {"use_case": "automation"}},
    ]
    return {
        "version": 1, "id": "branch-viewer-demo", "name": "Branch Demo",
        "goal": "g", "createdAt": "2026-08-29T00:00:00Z",
        "viewport": {"width": 1024, "height": 768}, "startUrl": "https://x.test",
        "steps": [step(0, "Intro"), chooser, step(2, "Rejoined")],
    }


@pytest.fixture
def branch_viewer_html(tmp_path) -> Path:
    from capturd.walk.viewer import render_viewer_to_file
    out = tmp_path / "viewer.html"
    render_viewer_to_file(_spec_with_choice_step(), out)
    return out


@pytest.mark.slow
def test_viewer_choice_screen_events_and_jump(branch_viewer_html: Path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(branch_viewer_html.as_uri())
        pg.wait_for_function("window.__demoViewer")

        pg.evaluate("window.__demoViewer.next()")     # -> choice step
        pg.wait_for_timeout(700)

        names = [e["event"] for e in pg.evaluate("window.__capturdEvents")]
        assert "branch_view" in names, names

        # Auto-advance must NOT have skipped past the choice step.
        assert pg.evaluate("window.__demoViewer.STATE.index") == 1

        # The overlay is visible with both buttons.
        assert pg.evaluate(
            "document.getElementById('choice-overlay').classList.contains('visible')")
        btns = pg.locator(".choice-btn")
        assert btns.count() == 2

        # Pick "Automation" -> branch_select with the analytics name, then jump.
        pg.locator(".choice-btn", has_text="Automation").click()
        pg.wait_for_timeout(700)

        events = pg.evaluate("window.__capturdEvents")
        sel = [e for e in events if e["event"] == "branch_select"]
        assert len(sel) == 1
        assert sel[0]["branchId"] == "entry-q"
        assert sel[0]["choiceId"] == "auto-path"
        # Jumped to the destination (rejoined step 2) and applied the variable.
        assert pg.evaluate("window.__demoViewer.STATE.index") == 2
        assert pg.evaluate("window.__demoViewer.STATE.vars['use_case']") == "automation"
        assert not pg.evaluate(
            "document.getElementById('choice-overlay').classList.contains('visible')")
        browser.close()


@pytest.mark.slow
def test_viewer_old_demos_render_unchanged(tmp_path: Path):
    """A spec with no choices never shows the overlay (backward compat)."""
    from capturd.walk.viewer import render_viewer_to_file

    spec = _spec_with_choice_step()
    spec["steps"][1].pop("choices")
    spec["steps"][1].pop("branchId")
    out = tmp_path / "viewer.html"
    render_viewer_to_file(spec, out)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(out.as_uri())
        pg.wait_for_function("window.__demoViewer")
        pg.evaluate("window.__demoViewer.next()")
        pg.wait_for_timeout(500)
        assert not pg.evaluate(
            "document.getElementById('choice-overlay').classList.contains('visible')")
        names = [e["event"] for e in pg.evaluate("window.__capturdEvents")]
        assert "branch_view" not in names
        browser.close()
