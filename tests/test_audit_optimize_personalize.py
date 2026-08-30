"""Phases 7-9 — demo.audit, demo.optimize, personalization, end-card CTA."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capturd.walk.audit import audit_spec
from capturd.walk.optimize import optimize_spec
from capturd.walk.personalize import (
    has_variables,
    personalize_spec,
    render_template,
    sanitize_vars,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec(steps: int = 4, *, annotations: bool = True, timeline: bool = True,
          cta: dict | None = None) -> dict:
    out = {
        "version": 1, "id": "audit-demo", "name": "Audit Demo", "goal": "g",
        "createdAt": "2026-08-29T00:00:00Z",
        "viewport": {"width": 1440, "height": 900}, "startUrl": "https://x.test",
        "steps": [
            {"index": i, "timestamp": i, "pageUrl": "https://x.test",
             "pageTitle": f"s{i}",
             "interaction": {"type": "click",
                             "target": {"selector": f"#b{i}", "tagName": "button",
                                        "text": f"Button {i}",
                                        "boundingRect": {"x": 700, "y": 300,
                                                         "width": 200, "height": 50}},
                             "hotspot": {"xPct": 50, "yPct": 50}},
             "annotation": (f"Step {i} narration." if annotations else None),
             "screenshotBase64": "AAAA"}
            for i in range(steps)
        ],
    }
    if timeline:
        out["aiAnnotations"] = {"animationTimeline": [
            {"stepIndex": i, "action": "zoomTo", "duration": 800}
            for i in range(steps)
        ]}
    if cta:
        out["cta"] = cta
    return out


# ---------------------------------------------------------------------------
# Phase 7 — audit
# ---------------------------------------------------------------------------


def test_audit_clean_spec_scores_high():
    a = audit_spec(_spec(cta={"id": "book", "label": "Book a Build Call",
                              "url": "https://rhobear.ai/call"}))
    assert a["overall"] >= 80
    assert a["behavioral_data"] is False
    # The no-analytics note must exist, but nothing behavioral may be claimed.
    eng = [f for f in a["findings"] if f["category"] == "engagement"]
    assert eng and "unavailable" in eng[0]["message"]


def test_audit_flags_silent_steps_and_duplicate_narration():
    spec = _spec(steps=3)
    spec["steps"][0]["annotation"] = "Button 0"          # duplicates hotspot copy
    spec["steps"][1]["annotation"] = None                # silent step
    a = audit_spec(spec)
    msgs = [f["message"] for f in a["findings"]]
    assert any("duplicates the visible hotspot" in m for m in msgs)
    assert any("no narration" in m for m in msgs)
    assert a["scores"]["narration"] < 100


def test_audit_flags_long_holds_static_steps_missing_cta():
    spec = _spec(steps=3)
    spec["aiAnnotations"]["animationTimeline"].append(
        {"stepIndex": 0, "action": "hold", "duration": 5000})
    spec["steps"][1]["interaction"] = {}                  # no hotspot
    a = audit_spec(spec)
    msgs = [f["message"] for f in a["findings"]]
    assert any("hold" in m for m in msgs)
    assert any("No final CTA" in m for m in msgs)
    assert a["scores"]["cta_structure"] < 100


def test_audit_flags_mobile_crop_and_dead_end_branch():
    spec = _spec(steps=4, cta={"id": "c", "label": "Go", "url": "https://x"})
    spec["steps"][2]["interaction"]["target"]["boundingRect"] = {
        "x": 5, "y": 300, "width": 30, "height": 50}      # far left edge
    spec["steps"][1]["choices"] = [
        {"id": "loop", "label": "Loop", "destination": 1}]
    a = audit_spec(spec)
    msgs = [f["message"] for f in a["findings"]]
    assert any("mobile-safe crop" in m for m in msgs)
    assert any("dead-end loop" in m for m in msgs)
    assert a["scores"]["branch_design"] < 100


def test_audit_uses_real_analytics_when_given_never_fabricates():
    spec = _spec(steps=3, cta={"id": "c", "label": "Go", "url": "https://x"})
    analytics = {
        "starts": 10, "completion_rate": 0.2,
        "steps": [{"step_id": "step-1", "reach": 8, "exits": 4,
                   "dropoff_pct": 50.0, "replays": 0}],
    }
    a = audit_spec(spec, analytics=analytics)
    assert a["behavioral_data"] is True
    eng = [f for f in a["findings"] if f["category"] == "engagement"]
    assert any("exit at step-1" in f["message"] for f in eng)
    assert any("Completion rate" in f["message"] for f in eng)

    # Same spec WITHOUT analytics: no behavioral claims appear.
    a2 = audit_spec(spec)
    assert all("exit at" not in f["message"] for f in a2["findings"])


def test_audit_scores_are_bounded_and_findings_ordered():
    spec = _spec(steps=2)
    spec["steps"][0]["interaction"] = {}
    spec["steps"][1]["interaction"] = {}
    spec["steps"][0]["annotation"] = None
    spec["steps"][1]["annotation"] = None
    a = audit_spec(spec)
    assert all(0 <= v <= 100 for v in a["scores"].values())
    assert 0 <= a["overall"] <= 100
    sevs = [f["severity"] for f in a["findings"]]
    assert sevs == sorted(sevs, key=lambda s: {"high": 0, "warn": 1, "info": 2}[s])


# ---------------------------------------------------------------------------
# Phase 8 — safe optimization
# ---------------------------------------------------------------------------


def test_optimize_plan_only_by_default():
    spec = _spec(steps=2)
    spec["steps"][0]["annotation"] = ("This is a very long annotation that rambles. "
                                      "It keeps going far past the budget. " * 4)
    original = spec["steps"][0]["annotation"]
    out = optimize_spec(spec)                     # apply=False
    assert out["applied"] is False and out["spec"] is None
    assert spec["steps"][0]["annotation"] == original, "plan-only must not mutate"
    actions = {p["action"] for p in out["plan"]}
    assert "copy_shorten" in actions
    short = next(p for p in out["plan"] if p["action"] == "copy_shorten")
    assert len(short["after"]) < len(short["before"])
    assert short["after"].endswith(("…", ".", "!", "?"))


def test_optimize_apply_is_reviewable_and_safe():
    spec = _spec(steps=2)
    spec["steps"][0]["annotation"] = "Trailing   spaces   and!!! punctuation.  "
    spec["aiAnnotations"]["animationTimeline"].append(
        {"stepIndex": 0, "action": "hold", "duration": 8000})
    out = optimize_spec(spec, apply=True)
    assert out["applied"] is True
    actions = [p["action"] for p in out["plan"]]
    assert "caption_cleanup" in actions and "pause_trim" in actions
    assert spec["steps"][0]["annotation"] == "Trailing spaces and! punctuation."
    hold = spec["aiAnnotations"]["animationTimeline"][-1]
    assert hold["duration"] == 3500


def test_optimize_never_deletes_steps_or_choices():
    spec = _spec(steps=4)
    spec["steps"][1]["choices"] = [{"id": "loop", "label": "L", "destination": 1}]
    n_steps = len(spec["steps"])
    out = optimize_spec(spec, apply=True)
    assert len(spec["steps"]) == n_steps, "optimize must never delete steps"
    assert spec["steps"][1]["choices"], "optimize must never delete branches"
    approval = [p for p in out["plan"] if p["action"] == "approval_required"]
    assert approval and all(p["safe"] is False for p in approval)


# ---------------------------------------------------------------------------
# Phase 9 — personalization
# ---------------------------------------------------------------------------


def test_render_template_substitution_and_fallbacks():
    t = 'See how {{business_name}} handles {{use_case | default:"incoming leads"}}'
    assert render_template(t, {"business_name": "America"}) == \
        "See how America handles incoming leads"
    assert render_template(t, {"business_name": "Acme", "use_case": "billing"}) == \
        "See how Acme handles billing"
    assert render_template("{{missing}}", {}) == ""
    assert render_template("{{missing | default:\"fallback\"}}", {}) == "fallback"
    assert render_template("plain text", {}) == "plain text"


def test_sanitize_vars_bounds_and_key_grammar():
    out = sanitize_vars({
        "good": "x" * 500,                 # bounded
        "bad key!": "dropped",             # grammar violation
        "n": 42,                           # stringified
    })
    assert len(out["good"]) == 200
    assert "bad key!" not in out
    assert out["n"] == "42"


def test_personalize_spec_applies_allowed_fields_only_and_copies():
    spec = _spec(steps=2, cta={"id": "book", "label": "Talk to {{name}}",
                               "url": "https://x"})
    spec["name"] = "How {{business_name}} grows"
    spec["steps"][0]["annotation"] = "Welcome, {{first_name | default:\"friend\"}}"
    spec["steps"][1]["choices"] = [
        {"id": "w", "label": "{{choice_word}} Website", "destination": 1}]
    spec["steps"][0]["interaction"]["target"]["text"] = "{{not_touched}}"

    out = personalize_spec(spec, {"business_name": "Acme", "choice_word": "Better"})
    assert out["name"] == "How Acme grows"
    assert out["steps"][0]["annotation"] == "Welcome, friend"
    assert out["steps"][1]["choices"][0]["label"] == "Better Website"
    assert out["cta"]["label"] == "Talk to "  # missing var, no default → empty
    assert "{{not_touched}}" in out["steps"][0]["interaction"]["target"]["text"], \
        "hotspot copy (recorded product text) must never be templated"
    # Input spec untouched (published versions are immutable).
    assert spec["name"] == "How {{business_name}} grows"


def test_personalize_cta_empty_when_var_missing():
    out = personalize_spec({"cta": {"id": "b", "label": "Hi {{name}}"}}, {})
    assert out["cta"]["label"] == "Hi "


def test_has_variables():
    assert has_variables("{{x}}") and not has_variables("plain")


# ---------------------------------------------------------------------------
# Viewer end-card CTA (Phase 6 surface for the contract)
# ---------------------------------------------------------------------------


def _placeholder_screenshot_b64() -> str:
    return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="


@pytest.mark.slow
def test_viewer_cta_card_emits_view_and_click(tmp_path: Path):
    from capturd.walk.viewer import render_viewer_to_file
    from playwright.sync_api import sync_playwright

    spec = {
        "version": 1, "id": "cta-demo", "name": "CTA Demo", "goal": "g",
        "createdAt": "2026-08-29T00:00:00Z",
        "viewport": {"width": 1024, "height": 768}, "startUrl": "https://x.test",
        "cta": {"id": "book", "analyticsName": "book-call",
                "label": "Book a Build Call", "url": "https://rhobear.ai/call"},
        "steps": [
            {"index": i, "timestamp": i, "pageUrl": "https://x.test",
             "pageTitle": f"s{i}",
             "interaction": {"type": "click",
                             "target": {"selector": f"#b{i}", "tagName": "button",
                                        "text": "b", "boundingRect": {
                                            "x": 10, "y": 10, "width": 50, "height": 20}},
                             "hotspot": {"xPct": 50, "yPct": 50}},
             "annotation": f"Step {i}",
             "screenshotBase64": _placeholder_screenshot_b64()}
            for i in range(2)
        ],
    }
    out = tmp_path / "viewer.html"
    render_viewer_to_file(spec, out)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(out.as_uri())
        pg.wait_for_function("window.__demoViewer")
        pg.evaluate("window.__demoViewer.goToStep(1, 1, true)")   # last step
        pg.wait_for_timeout(900)

        assert pg.evaluate(
            "document.getElementById('cta-card').classList.contains('visible')")
        names = [e["event"] for e in pg.evaluate("window.__capturdEvents")]
        assert "cta_view" in names
        view = next(e for e in pg.evaluate("window.__capturdEvents")
                    if e["event"] == "cta_view")
        assert view["ctaId"] == "book-call"

        pg.evaluate("document.getElementById('cta-btn').click()")
        pg.wait_for_timeout(100)
        click = next(e for e in pg.evaluate("window.__capturdEvents")
                     if e["event"] == "cta_click")
        assert click["ctaId"] == "book-call"
        # Navigate away: card hides.
        pg.evaluate("window.__demoViewer.goToStep(0, -1, true)")
        pg.wait_for_timeout(900)
        assert not pg.evaluate(
            "document.getElementById('cta-card').classList.contains('visible')")
        browser.close()
