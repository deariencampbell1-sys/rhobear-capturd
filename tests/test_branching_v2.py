"""Branching V2 (neutral extraction) — viewer-facing choice points.

Covers the W2-05a slice only: neutral choice primitives, no personalization,
no CTA/tracked-send tokens, no analytics-name requirements.

- schema.validate_choices: normalization to {id, label, destination} plus
  structural rejection (empty / duplicate ids, bad labels/types, out-of-range
  destinations) and the self-dead-end destination rule
- DemoStep round-trips branchId/choices; old specs without them stay valid
- DemoForge.add_choices (+ legacy add_branch untouched)
- viewer render: a spec with no choices renders unchanged, malformed choices
  fail safely with the offending field named
- source scan: no personalization / tracked-send / CRM / AWS / hosted
  analytics layer was introduced in the owned product files
- Playwright (slow): overlay waits for selection, branch_view on entry,
  branch_select on pick, jump to the destination; old specs show no overlay
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from capturd.walk.schema import DemoSpec, DemoStep, validate_choices

FIXTURES = Path(__file__).parent / "fixtures" / "neutral-v2"

#: Product files this task owns — the scan target for excluded layers.
OWNED_PRODUCT_SOURCES = (
    "capturd/walk/schema.py",
    "capturd/walk/coordinator.py",
    "capturd/walk/viewer.py",
    "capturd/walk/templates/viewer.html",
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _choices(step_count: int = 4) -> list[dict]:
    return [
        {"id": "website", "label": "Better Website", "destination": 2},
        {"id": "automation", "label": "Automation", "destination": 1},
    ]


def test_validate_choices_normalizes_to_neutral_shape():
    out = validate_choices(_choices(), step_count=4, step_index=0)
    assert [c["id"] for c in out] == ["website", "automation"]
    assert out[0] == {"id": "website", "label": "Better Website", "destination": 2}
    # Exactly the three neutral keys — nothing personalization-, CTA- or
    # analytics-shaped survives normalization.
    for choice in out:
        assert set(choice) == {"id", "label", "destination"}


def test_validate_choices_drops_excluded_keys_from_input():
    """V2-style extras (variable / ctaId / analyticsName) are not carried."""
    out = validate_choices(
        [
            {"id": "auto", "label": "Auto", "destination": 1,
             "analyticsName": "auto-path", "variable": {"use_case": "x"},
             "ctaId": "book"},
        ],
        step_count=4,
        step_index=0,
    )
    assert out == [{"id": "auto", "label": "Auto", "destination": 1}]


@pytest.mark.parametrize("bad", [
    [],                                             # empty
    "not-a-list",                                   # not a list
    [{"id": "", "label": "x", "destination": 0}],   # empty id
    [{"id": "bad id!", "label": "x", "destination": 0}],  # bad charset
    [{"id": "a", "label": "", "destination": 0}],   # empty label
    [{"id": "a", "label": "x", "destination": 9}],  # out of range
    [{"id": "a", "label": "x", "destination": "1"}],  # non-int destination
    [{"id": "a", "label": "x", "destination": True}],  # bool is not an index
    [{"id": "a", "label": "x", "destination": 0},
     {"id": "b", "label": "y", "destination": 0},
     {"id": "b", "label": "z", "destination": 1}],  # duplicate id
    [dict(c) for c in _choices()] + [
        {"id": f"c{i}", "label": f"C{i}", "destination": 1} for i in range(11)
    ],                                               # 13 choices > max 12
])
def test_validate_choices_rejects_structural_problems(bad):
    with pytest.raises(ValueError):
        validate_choices(bad, step_count=4, step_index=0)


def test_self_dead_end_destination_rejected_with_field_named():
    """A choice that targets its own step with no other exit is refused,
    and the error names the offending field."""
    with pytest.raises(ValueError) as exc:
        validate_choices(
            [{"id": "stuck", "label": "Stay", "destination": 1}],
            step_count=3,
            step_index=1,
        )
    msg = str(exc.value)
    assert "destination" in msg, msg
    assert "stuck" in msg, msg


def test_all_self_targets_is_a_dead_end():
    with pytest.raises(ValueError) as exc:
        validate_choices(
            [{"id": "one", "label": "One", "destination": 2},
             {"id": "two", "label": "Two", "destination": 2}],
            step_count=3,
            step_index=2,
        )
    assert "destination" in str(exc.value)


def test_self_loop_allowed_when_another_exit_exists():
    out = validate_choices(
        [{"id": "stay", "label": "Stay", "destination": 1},
         {"id": "go", "label": "Go on", "destination": 2}],
        step_count=3,
        step_index=1,
    )
    assert [c["destination"] for c in out] == [1, 2]


def test_demo_step_roundtrips_branch_fields_and_stays_backward_compatible():
    # Old-style step: no branch fields at all.
    old = DemoStep(index=0, timestamp=0, pageUrl="https://x", pageTitle="p",
                   interaction={})
    d = DemoSpec(id="d1", steps=[old]).to_dict()
    assert d["steps"][0]["choices"] is None
    assert d["steps"][0]["branchId"] is None

    # New-style step with choices survives the round trip.
    new = DemoStep(index=1, timestamp=1, pageUrl="https://x", pageTitle="p",
                   interaction={}, branchId="entry-q", choices=_choices())
    d = DemoSpec(id="d2", steps=[new]).to_dict()
    assert d["steps"][0]["branchId"] == "entry-q"
    assert d["steps"][0]["choices"][0]["id"] == "website"


# ---------------------------------------------------------------------------
# Viewer render (non-browser)
# ---------------------------------------------------------------------------


def _embedded_spec(html: str) -> dict:
    m = re.search(
        r'<script id="demo-data" type="application/json">\n(.*?)\n  </script>',
        html,
        re.S,
    )
    assert m, "rendered viewer is missing the embedded demo-data block"
    return json.loads(m.group(1))


def test_plain_spec_renders_unchanged_without_choices():
    from capturd.walk.viewer import render_viewer

    html = render_viewer(_fixture("spec_plain.json"))
    embedded = _embedded_spec(html)
    for step in embedded["steps"]:
        assert "choices" not in step
        assert "branchId" not in step
    # The choice overlay ships hidden by default — a spec without choices
    # renders exactly as before.
    assert re.search(r"#choice-overlay\s*\{\s*display:\s*none;", html), \
        "choice overlay must be hidden by default"


def test_malformed_choices_fail_render_safely():
    from capturd.walk.viewer import ViewerRenderError, render_viewer

    with pytest.raises(ViewerRenderError) as exc:
        render_viewer(_fixture("spec_bad_choices.json"))
    assert "destination" in str(exc.value)

    dead = _fixture("spec_choices.json")
    dead["steps"][1]["choices"] = [{"id": "loop", "label": "Loop", "destination": 1}]
    with pytest.raises(ViewerRenderError) as exc:
        render_viewer(dead)
    assert "destination" in str(exc.value)


def test_viewer_refusal_raises_viewer_render_error():
    """Finding 4 — every refusal on the render path is a ViewerRenderError,
    never a bare ValueError (not even as a base class)."""
    from capturd.walk.viewer import ViewerRenderError, render_viewer

    with pytest.raises(ViewerRenderError) as exc:
        render_viewer(_fixture("spec_bad_choices.json"))
    assert not isinstance(exc.value, ValueError)
    assert "destination" in str(exc.value)


# ---------------------------------------------------------------------------
# Render / export refusals and personalization stripping (W2-05a-r2)
# ---------------------------------------------------------------------------


PERSONALIZATION_KEYS = ("analyticsName", "variable", "ctaId")


def _assert_no_personalization(embedded: dict) -> None:
    """The embedded #demo-data payload carries no excluded key anywhere."""
    blob = json.dumps(embedded)
    for key in PERSONALIZATION_KEYS:
        assert key not in blob, f"#demo-data leaked {key}: {blob[:400]}"
    for step in embedded["steps"]:
        for ch in step.get("choices") or []:
            assert set(ch) == {"id", "label", "destination"}


def _strip_personalization(spec: dict) -> dict:
    clean = json.loads(json.dumps(spec))
    for step in clean["steps"]:
        for ch in step.get("choices") or []:
            for key in PERSONALIZATION_KEYS:
                ch.pop(key, None)
    return clean


def _fresh(tmp_path: Path, name: str, spec: dict):
    from capturd.walk.coordinator import DemoForge

    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec(name, spec)
    return forge


def test_export_refuses_malformed_choices_with_demo_export_error(tmp_path: Path):
    """Finding 1/4 — export_demo refuses a spec with malformed choices with
    a DemoExportError carrying the viewer's message, and writes no file
    (edge case 5: a refusal never leaves a half-written export behind)."""
    from capturd.walk.exporter import DemoExportError

    forge = _fresh(tmp_path, "bad-choices", _fixture("spec_bad_choices.json"))
    with pytest.raises(DemoExportError) as exc:
        forge.export_demo("bad-choices", fmt="html")
    assert "destination" in str(exc.value)
    assert not (forge.demo_dir("bad-choices") / "export.html").exists()


def test_render_and_export_strip_personalization_keys(tmp_path: Path):
    """Finding 3 — the normalization that strips analyticsName/variable/ctaId
    runs on render AND export, so a hand-authored spec cannot carry them into
    #demo-data. With the keys present the output is byte-identical to the
    output without them (edge case 1: keys stripped, everything else
    untouched)."""
    from capturd.walk.viewer import render_viewer

    dirty = _fixture("spec_personalization.json")
    clean = _strip_personalization(dirty)

    html_dirty = render_viewer(json.loads(json.dumps(dirty)))
    html_clean = render_viewer(json.loads(json.dumps(clean)))
    assert html_dirty == html_clean, "extra keys changed the render bytes"
    _assert_no_personalization(_embedded_spec(html_dirty))

    forge = _fresh(tmp_path, "persona", clean)
    out_clean = forge.export_demo("persona", fmt="html").read_bytes()
    forge.save_spec("persona", dirty)
    out_dirty = forge.export_demo("persona", fmt="html").read_bytes()
    assert out_dirty == out_clean, "extra keys changed the export bytes"
    _assert_no_personalization(
        _embedded_spec(out_dirty.decode("utf-8")))


def test_empty_choices_refused_on_both_paths(tmp_path: Path):
    """Finding 5 — ``choices: []`` is refused by schema.validate_choices and
    by render and export alike, always with schema's exact message."""
    from capturd.walk.exporter import DemoExportError
    from capturd.walk.viewer import ViewerRenderError, render_viewer

    message = "choices: must be a non-empty list"
    with pytest.raises(ValueError) as exc:
        validate_choices([], step_count=3, step_index=1)
    assert str(exc.value) == message

    spec = _fixture("spec_choices.json")
    spec["steps"][1]["choices"] = []

    with pytest.raises(ViewerRenderError) as rex:
        render_viewer(json.loads(json.dumps(spec)))
    assert str(rex.value) == message

    forge = _fresh(tmp_path, "empty-choices", spec)
    with pytest.raises(DemoExportError) as eex:
        forge.export_demo("empty-choices", fmt="html")
    assert str(eex.value) == message
    assert not (forge.demo_dir("empty-choices") / "export.html").exists()


# ---------------------------------------------------------------------------
# Demo-data embed escape (W2-05a-r3)
# ---------------------------------------------------------------------------


BREAKOUT = "</script><script>window.__pwned=1</script>"


def _escape_spec() -> dict:
    """A spec whose choice label and step annotation carry the script-
    breakout string — both are free text and both land in #demo-data."""
    spec = json.loads(json.dumps(_fixture("spec_choices.json")))
    step = spec["steps"][1]
    step["annotation"] = "Note " + BREAKOUT
    step["choices"][0]["label"] = "Run " + BREAKOUT
    return spec


def _pipeline_normalized(spec: dict) -> dict:
    """The spec as render/export ship it: target metadata + choice
    normalization applied (both run in place on the shipped copy)."""
    from capturd.walk.viewer import propagate_target_metadata, validate_spec_choices

    out = json.loads(json.dumps(spec))
    propagate_target_metadata(out)
    validate_spec_choices(out)
    return out


def test_render_and_export_escape_the_demo_data_embed(tmp_path: Path):
    """A choice label or a step annotation holding
    ``</script><script>window.__pwned=1</script>`` must not break out of the
    ``#demo-data`` block at either sink — render_viewer AND export_demo.
    Each output carries the same ``</script>`` count as the same spec
    without the string, and never a live injected tag."""
    from capturd.walk.viewer import render_viewer

    spec = _escape_spec()
    clean = json.loads(json.dumps(spec))
    clean["steps"][1]["annotation"] = "Note safe"
    clean["steps"][1]["choices"][0]["label"] = "Run safe"

    html = render_viewer(json.loads(json.dumps(spec)))
    html_clean = render_viewer(clean)
    assert html.count("</script>") == html_clean.count("</script>"), (
        "a free-text field broke out of the demo-data script block")
    assert "<script>window.__pwned" not in html

    forge = _fresh(tmp_path, "escape", spec)
    forge.save_spec("escape-clean", clean)
    out = forge.export_demo("escape", fmt="html").read_text(encoding="utf-8")
    out_clean = forge.export_demo("escape-clean", fmt="html").read_text(
        encoding="utf-8")
    assert out.count("</script>") == out_clean.count("</script>"), (
        "a free-text field broke out of the demo-data script block on export")
    assert "<script>window.__pwned" not in out


def test_embedded_spec_round_trips_intact():
    """The escape changes bytes, not data: the ``#demo-data`` text, parsed
    by ``json.loads``, equals the shipped spec with the breakout string,
    ``<!--`` and ``<script`` intact (edge case 1). A spec with no ``<``
    anywhere parses to exactly the spec too (edge case 2)."""
    from capturd.walk.viewer import render_viewer

    spec = _escape_spec()
    spec["steps"][0]["annotation"] = "<!-- not a comment <script> trap"
    embedded = _embedded_spec(render_viewer(json.loads(json.dumps(spec))))
    assert embedded == _pipeline_normalized(spec)
    assert BREAKOUT in embedded["steps"][1]["choices"][0]["label"]

    plain = _fixture("spec_plain.json")
    assert "<" not in json.dumps(plain)
    assert _embedded_spec(render_viewer(json.loads(json.dumps(plain)))) \
        == _pipeline_normalized(plain)


@pytest.mark.slow
def test_label_with_markup_shows_as_text(tmp_path: Path):
    """In the booted viewer the breakout label renders as literal text, the
    annotation names the choice point literally, and the injected script
    never executes (window.__pwned stays unset)."""
    from capturd.walk.viewer import render_viewer_to_file

    out = tmp_path / "viewer_escape.html"
    render_viewer_to_file(_escape_spec(), out)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(out.as_uri())
        pg.wait_for_function("window.__demoViewer && window.__capturdEvents")
        pg.evaluate("window.__demoViewer.next()")
        pg.wait_for_function("window.__demoViewer.STATE.index === 1",
                             timeout=15000)
        pg.wait_for_function(
            "document.querySelectorAll('.choice-btn').length === 2",
            timeout=5000)

        assert pg.evaluate("window.__pwned") is None, \
            "the breakout string executed as script in the viewer"
        labels = [pg.locator(".choice-btn").nth(i).text_content()
                  for i in range(2)]
        assert "Run " + BREAKOUT in labels, labels
        assert pg.evaluate(
            "document.getElementById('choice-title').textContent"
        ) == "Note " + BREAKOUT
        browser.close()


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def _minimal_spec(demo_id: str, step_count: int = 4) -> dict:
    return {
        "version": 1, "id": demo_id, "name": "T", "goal": "g",
        "createdAt": "2026-10-30T00:00:00Z",
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
    stored = spec["steps"][1]["choices"]
    assert stored[0] == {"id": "website", "label": "Better Website",
                         "destination": 2}
    assert set(stored[1]) == {"id", "label", "destination"}
    assert spec["steps"][1]["branchId"] == "entry-q"
    # Steps without choices stay untouched.
    assert spec["steps"][0].get("choices") is None


def test_add_choices_rejects_bad_destination(forge_with_demo):
    from capturd.walk.coordinator import DemoForgeError

    with pytest.raises(DemoForgeError) as exc:
        forge_with_demo.add_choices(
            "branch-demo", 0,
            [{"id": "x", "label": "X", "destination": 99}])
    assert "destination" in str(exc.value)


def test_add_choices_rejects_self_dead_end(forge_with_demo):
    from capturd.walk.coordinator import DemoForgeError

    with pytest.raises(DemoForgeError) as exc:
        forge_with_demo.add_choices(
            "branch-demo", 2,
            [{"id": "loop", "label": "Loop", "destination": 2}])
    assert "destination" in str(exc.value)


def test_add_choices_rejects_bad_branch_id(forge_with_demo):
    from capturd.walk.coordinator import DemoForgeError

    with pytest.raises(DemoForgeError) as exc:
        forge_with_demo.add_choices("branch-demo", 0, _choices(),
                                    branch_id="bad branch id!")
    assert "branchId" in str(exc.value)


def test_add_choices_rejects_out_of_range_step(forge_with_demo):
    from capturd.walk.coordinator import DemoForgeError

    with pytest.raises(DemoForgeError):
        forge_with_demo.add_choices("branch-demo", 9, _choices())


def test_legacy_add_branch_still_works(forge_with_demo):
    alt = _minimal_spec("alt", step_count=1)["steps"]
    out = forge_with_demo.add_branch("branch-demo", 0, alt)
    assert out["branchCount"] == 1
    spec = forge_with_demo.load_spec("branch-demo")
    assert spec["steps"][0]["branches"] == [alt]
    assert spec["steps"][0].get("choices") is None


# ---------------------------------------------------------------------------
# Source scan — the excluded layers stay out of the owned product files
# ---------------------------------------------------------------------------

BANNED_TOKENS = (
    "personalize",
    "analyticsName",
    "ctaId",
    "cta_view",
    "cta_click",
    "STATE.vars",
    "ch.variable",
    "sendBeacon",
    "beacon",
    "XMLHttpRequest",
    "frontman",
    "herald",
    "polly",
    "transcribe",
    "boto3",
    "vertex",
    "gemini",
    "rho_voice",
    "attribution",
)


def test_no_excluded_layers_introduced_in_owned_sources():
    repo = Path(__file__).resolve().parents[1]
    for rel in OWNED_PRODUCT_SOURCES:
        text = (repo / rel).read_text(encoding="utf-8").lower()
        for token in BANNED_TOKENS:
            assert token.lower() not in text, f"{rel} contains banned token {token!r}"
        if rel.endswith(".html"):
            # The only network call in the viewer is the pre-existing
            # relative-path ?demo= loader; the event emitter adds none.
            assert text.count("fetch(") == 1, "viewer must not gain network calls"
            assert "websocket(" not in text
        else:
            assert "import requests" not in text
            assert "import httpx" not in text


# ---------------------------------------------------------------------------
# Viewer behavior (Playwright)
# ---------------------------------------------------------------------------


@pytest.fixture
def choice_viewer_html(tmp_path) -> Path:
    from capturd.walk.viewer import render_viewer_to_file

    out = tmp_path / "viewer.html"
    render_viewer_to_file(_fixture("spec_choices.json"), out)
    return out


@pytest.mark.slow
def test_viewer_choice_screen_waits_selects_and_jumps(choice_viewer_html: Path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(choice_viewer_html.as_uri())
        pg.wait_for_function("window.__demoViewer && window.__capturdEvents")

        # Auto-play into the choice step; it must wait instead of advancing.
        pg.evaluate("window.__demoViewer.play()")
        pg.wait_for_function("window.__demoViewer.STATE.index === 1",
                             timeout=15000)
        pg.wait_for_timeout(700)

        names = [e["event"] for e in pg.evaluate("window.__capturdEvents")]
        assert "demo_start" in names, names
        assert "branch_view" in names, names
        assert pg.evaluate(
            "document.getElementById('choice-overlay').classList.contains('visible')")
        assert pg.locator(".choice-btn").count() == 2

        # The choice step holds: dwell + transition long past, still on step 1.
        pg.wait_for_timeout(2600)
        assert pg.evaluate("window.__demoViewer.STATE.index") == 1

        pg.locator(".choice-btn", has_text="Automation").click()
        pg.wait_for_timeout(700)

        events = pg.evaluate("window.__capturdEvents")
        sel = [e for e in events if e["event"] == "branch_select"]
        assert len(sel) == 1, [e["event"] for e in events]
        assert sel[0]["branchId"] == "entry-q"
        assert sel[0]["choiceId"] == "automation"
        assert sel[0]["stepId"] == "step-1"
        # Jumped to the destination and the overlay is gone.
        assert pg.evaluate("window.__demoViewer.STATE.index") == 2
        assert not pg.evaluate(
            "document.getElementById('choice-overlay').classList.contains('visible')")
        browser.close()


@pytest.mark.slow
def test_demo_param_out_of_range_destination_never_strands_viewer(tmp_path: Path):
    """Finding 6 — the ``?demo=`` runtime path skips render-time validation.
    A choice whose destination cannot exist must never be offered: clicking
    it re-enters the choice step forever and the viewer can never finish.
    Served over loopback-intercepted http (no real network) — the harness
    empties #demo-data so loadDemoSpec falls back to ``?demo=``."""
    from capturd.walk.viewer import default_template_path

    tpl = default_template_path().read_text(encoding="utf-8")
    page_html = re.sub(
        r'<script id="demo-data"[^>]*>.*?</script>',
        lambda _m: '<script id="demo-data" type="application/json">\n  </script>',
        tpl, count=1, flags=re.S,
    )
    spec_json = (FIXTURES / "spec_bad_choices.json").read_text(encoding="utf-8")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.route("http://**", lambda route: route.abort())
        pg.route("https://**", lambda route: route.abort())
        pg.route("ws://**", lambda route: route.abort())

        def _serve(route):
            url = route.request.url.split("?")[0]
            if url.endswith("/spec_bad_choices.json"):
                route.fulfill(body=spec_json, content_type="application/json")
            elif url.endswith("/viewer.html"):
                route.fulfill(body=page_html, content_type="text/html")
            else:
                route.fulfill(status=404, body="")

        pg.route("http://demo.local/**", _serve)  # registered last → wins
        pg.goto("http://demo.local/viewer.html?demo=spec_bad_choices.json")
        pg.wait_for_function("window.__demoViewer && window.__capturdEvents")
        pg.evaluate("window.__demoViewer.next()")
        pg.wait_for_function("window.__demoViewer.STATE.index === 1",
                             timeout=15000)
        pg.wait_for_timeout(500)

        # If the dead pick (destination 99 on a 2-step spec) is offered,
        # clicking it must not start the strand cycle.
        if pg.locator(".choice-btn").count():
            pg.locator(".choice-btn").first.click()
            pg.wait_for_timeout(700)
        assert pg.locator(".choice-btn").count() == 0, (
            "the ?demo= runtime path offered a choice with an unreachable "
            "destination — the viewer is stranded on the choice step")

        # Whatever happens, the run must still reach its natural end.
        pg.evaluate("window.__demoViewer.play()")
        pg.wait_for_function(
            "window.__capturdEvents.some(e => e.event === 'demo_complete')",
            timeout=20000)
        browser.close()


@pytest.mark.slow
def test_self_loop_choice_is_dropped_at_runtime(tmp_path: Path):
    """Finding 2 — the ``?demo=`` runtime path skips render-time validation.
    A choice whose destination is its own step is refused by schema, render
    and export, yet the runtime kept offering it: picking it re-enters the
    choice step forever and demo_complete never fires. Every choice
    targeting its own step must be dropped, the step acting as a terminal
    one (edge case 3)."""
    from capturd.walk.viewer import default_template_path

    spec = json.loads(json.dumps(_fixture("spec_plain.json")))
    last = len(spec["steps"]) - 1
    spec["steps"][last]["choices"] = [
        {"id": "loop_a", "label": "Loop A", "destination": last},
        {"id": "loop_b", "label": "Loop B", "destination": last},
    ]
    spec_json = json.dumps(spec)

    tpl = default_template_path().read_text(encoding="utf-8")
    page_html = re.sub(
        r'<script id="demo-data"[^>]*>.*?</script>',
        lambda _m: '<script id="demo-data" type="application/json">\n  </script>',
        tpl, count=1, flags=re.S,
    )

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.route("http://**", lambda route: route.abort())
        pg.route("https://**", lambda route: route.abort())
        pg.route("ws://**", lambda route: route.abort())

        def _serve(route):
            url = route.request.url.split("?")[0]
            if url.endswith("/spec_selfloop.json"):
                route.fulfill(body=spec_json, content_type="application/json")
            elif url.endswith("/viewer.html"):
                route.fulfill(body=page_html, content_type="text/html")
            else:
                route.fulfill(status=404, body="")

        pg.route("http://demo.local/**", _serve)  # registered last → wins
        pg.goto("http://demo.local/viewer.html?demo=spec_selfloop.json")
        pg.wait_for_function("window.__demoViewer && window.__capturdEvents")
        pg.evaluate("window.__demoViewer.next()")
        pg.wait_for_function("window.__demoViewer.STATE.index === 1",
                             timeout=15000)
        pg.evaluate("window.__demoViewer.next()")
        pg.wait_for_function("window.__demoViewer.STATE.index === 2",
                             timeout=15000)
        pg.wait_for_timeout(500)

        assert pg.locator(".choice-btn").count() == 0, (
            "the ?demo= runtime path offered a choice that re-enters its own "
            "step — the viewer is stranded on the choice step")

        # The run still reaches its natural end.
        pg.evaluate("window.__demoViewer.play()")
        pg.wait_for_function(
            "window.__capturdEvents.some(e => e.event === 'demo_complete')",
            timeout=25000)
        browser.close()


@pytest.mark.slow
def test_choice_pick_during_transition_is_not_recorded(choice_viewer_html: Path):
    """Finding 7 — the showChoices click handler has a re-entrancy window:
    a pick landing while goToStep is mid-transition emits branch_select and
    destroys the overlay, but goToStep refuses the navigation. An ignored
    pick must never be recorded."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(choice_viewer_html.as_uri())
        pg.wait_for_function("window.__demoViewer && window.__capturdEvents")
        pg.evaluate("window.__demoViewer.next()")
        pg.wait_for_function("window.__demoViewer.STATE.index === 1",
                             timeout=15000)
        pg.wait_for_function("document.querySelectorAll('.choice-btn').length === 2")

        phase = pg.evaluate(
            """() => {
              const V = window.__demoViewer;
              V.next();                       // phase → 'transitioning'
              const during = V.STATE.phase;
              document.querySelector('.choice-btn').click();
              return during;
            }""")
        assert phase == "transitioning", f"window closed unexpectedly: {phase}"

        pg.wait_for_function("window.__demoViewer.STATE.index === 2",
                             timeout=15000)
        pg.wait_for_timeout(400)
        sels = [e for e in pg.evaluate("window.__capturdEvents")
                if e["event"] == "branch_select"]
        assert sels == [], f"a mid-transition pick was recorded: {sels}"
        browser.close()


@pytest.mark.slow
def test_viewer_old_demo_without_choices_renders_unchanged(tmp_path: Path):
    """A spec with no choices never shows the overlay and never emits
    branch events — playback behaves exactly as before."""
    from capturd.walk.viewer import render_viewer_to_file

    out = tmp_path / "viewer.html"
    render_viewer_to_file(_fixture("spec_plain.json"), out)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(out.as_uri())
        pg.wait_for_function("window.__demoViewer")
        pg.evaluate("window.__demoViewer.next()")
        pg.wait_for_timeout(700)
        assert not pg.evaluate(
            "document.getElementById('choice-overlay').classList.contains('visible')")
        assert pg.locator(".choice-btn").count() == 0
        names = [e["event"] for e in pg.evaluate("window.__capturdEvents")]
        assert "branch_view" not in names, names
        assert "branch_select" not in names, names
        browser.close()
