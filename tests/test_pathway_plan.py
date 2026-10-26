"""W0-01d-r1 — pathway_plan: URL resolution and target planning.

The seam layer: a parsed pathway map plus a base URL becomes capture targets.
Everything here reuses the frozen modules (pathway_map, planner) and never
re-implements parsing, planning, scrubbing or selectors.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from capturd.shots.capture import CaptureTarget, RestedCaptureError
from capturd.shots.pathway_map import PathwayMapError, load_pathway_map
from capturd.shots.pathway_plan import (
    MAX_TARGETS,
    plan_targets_from_map,
    resolve_pathway_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "capture-foundations"
WIRING = Path(__file__).parent / "fixtures" / "capture-wiring"
TWO_CONTROL_HTML = (WIRING / "two-control-page.html").read_text(encoding="utf-8")


def _load(name: str) -> dict:
    path = (WIRING / name) if (WIRING / name).is_file() else (FIXTURES / name)
    if name.endswith(".json"):
        return load_pathway_map(map_path=path)
    return load_pathway_map(manifest_path=path)


# ---------------------------------------------------------------------------
# 1. resolve_pathway_url — ported route resolution
# ---------------------------------------------------------------------------


def test_resolve_route_path_becomes_absolute():
    assert resolve_pathway_url("/settings", "http://base.test") == "http://base.test/settings"
    assert resolve_pathway_url("about", "http://base.test") == "http://base.test/about"


def test_absolute_route_is_not_rebased():
    # Edge 1: an already-absolute route is kept as-is, never doubled
    # against base_url.
    absolute = "https://example.com/deep/page?x=1"
    assert resolve_pathway_url(absolute, "http://127.0.0.1:8123") == absolute


def test_unresolvable_route_raises_with_the_pathway_named():
    # Work item 1: never a silent "" — a route that cannot be resolved
    # raises PathwayMapError naming the pathway.
    for bad in ("", "   ", "C:\\Users\\dev\\route", "mailto:someone@example.com"):
        with pytest.raises(PathwayMapError) as excinfo:
            resolve_pathway_url(bad, "http://base.test", pathway_id="p-broken-route")
        message = str(excinfo.value)
        assert "p-broken-route" in message
        # no host path may leak through the refusal
        assert "C:\\Users" not in message
        assert "Users\\dev" not in message


def test_resolve_names_no_pathway_when_unnamed():
    with pytest.raises(PathwayMapError):
        resolve_pathway_url("", "http://base.test")


# ---------------------------------------------------------------------------
# 2. plan_targets_from_map — one CaptureTarget per capturable pathway
# ---------------------------------------------------------------------------


def test_plan_targets_carry_pathway_fields():
    pathway_map = _load("named-control-manifest.md")

    def fake_page(url: str) -> str:
        return (
            "<html><body>"
            '<button id="open-settings" data-target="#settings-panel">Settings</button>'
            "</body></html>"
        )

    import capturd.shots.pathway_plan as pathway_plan

    original = pathway_plan._load_page_html
    pathway_plan._load_page_html = fake_page
    try:
        urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    finally:
        pathway_plan._load_page_html = original

    assert len(targets) == 2
    assert all(isinstance(target, CaptureTarget) for target in targets)
    first, second = targets
    assert first.pathway_id == "open-settings"
    assert first.pathway_status == "WIRED"
    assert first.pathway_trigger.strip("`").startswith("click")
    assert first.url == "http://base.test/settings"
    # The map names the control `#open-settings`; the selector comes from
    # planner.py's candidate, not from the map string.
    assert first.trigger_selector == "#open-settings"
    assert second.pathway_id == "open-missing"
    assert second.pathway_status == "UNWIRED"
    assert second.url == "http://base.test/"
    # The map names `#missing-btn` but the page has no such control.
    assert second.trigger_selector == ""
    assert urls == ["http://base.test/settings", "http://base.test/"]


def test_named_selector_comes_from_planner():
    # "Do not re-implement planning, scrubbing or selectors — call it":
    # when the map names a control the page cannot answer with the map's own
    # string, the target's selector is the PLANNER's generated selector for
    # the matching control — never a hand-rolled one.
    import capturd.shots.planner as planner

    pathway_map = _load("named-control-manifest.md")
    page_without_ids = "<html><body><button>Settings</button></body></html>"

    def fake_page(url: str) -> str:
        return page_without_ids if url.endswith("/settings") else "<html><body></body></html>"

    import capturd.shots.pathway_plan as pathway_plan

    original = pathway_plan._load_page_html
    pathway_plan._load_page_html = fake_page
    try:
        _urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    finally:
        pathway_plan._load_page_html = original

    plan = planner.plan_candidates(planner.parse_html(page_without_ids))
    assert len(plan.candidates) == 1
    generated = plan.candidates[0].selector
    # the map named `#open-settings`; the planner's selector is the one used
    assert targets[0].trigger_selector == generated
    assert targets[0].trigger_selector != "#open-settings"


def test_verifier_call_binds_to_control_target():
    # The verifier flavour names the control by the call it makes
    # (downstream "/api/state"); the fixture page has the control wired to
    # that call, and the planner's selector for it is what the target carries.
    import capturd.shots.pathway_plan as pathway_plan

    pathway_map = _load("sample-verifier-map.json")

    original = pathway_plan._load_page_html
    pathway_plan._load_page_html = lambda url: TWO_CONTROL_HTML
    try:
        urls, targets = plan_targets_from_map(pathway_map, "http://127.0.0.1:8123")
    finally:
        pathway_plan._load_page_html = original

    assert len(targets) == 2
    selectors = {target.pathway_id: target.trigger_selector for target in targets}
    assert selectors["route-not-found-_api_state"] == "#load-state"
    assert selectors["route-not-found-_api_control"] == "#send-control"
    assert urls == ["http://127.0.0.1:8123/?view=board"]


def test_no_host_path_in_plan_or_error():
    # Edge 4: a pasted map with a Windows path in its text parses, and the
    # plan or the error carries no host path.
    pathway_map = _load("windows-path-manifest.md")
    pasted = pathway_map["raw"]
    assert "C:\\Users\\dev" in pasted  # the path is in the map text

    import capturd.shots.pathway_plan as pathway_plan

    original = pathway_plan._load_page_html
    pathway_plan._load_page_html = lambda url: "<html><body></body></html>"
    try:
        urls, targets = plan_targets_from_map(pathway_map, "http://base.test")
    finally:
        pathway_plan._load_page_html = original

    plan_text = json.dumps(
        {"urls": urls, "targets": [dataclasses.asdict(target) for target in targets]},
        sort_keys=True,
    )
    for host_fragment in ("C:\\Users", "Users\\dev", "project\\src", "project/src"):
        assert host_fragment not in plan_text

    with pytest.raises(PathwayMapError) as excinfo:
        resolve_pathway_url("C:\\Users\\dev\\broken", "http://base.test", pathway_id="p1")
    assert "C:\\Users" not in str(excinfo.value)


def test_max_targets_refusal_matches_the_service_limit():
    # Work item 2 / edge 3: the service's MAX_TARGETS refusal shape —
    # "too many capture targets; maximum is N" — nothing partially planned.
    assert MAX_TARGETS == 5000
    pathway_map = _load("named-control-manifest.md")

    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(pathway_map, "http://base.test", max_targets=1)
    assert str(excinfo.value) == "too many capture targets; maximum is 1"


def test_plan_targets_require_a_base_url():
    pathway_map = _load("single-pathway-manifest.md")
    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map(pathway_map, "")
    assert "base_url" in str(excinfo.value)


def test_empty_map_has_no_targets():
    pathway_map = _load("single-pathway-manifest.md")
    with pytest.raises(RestedCaptureError) as excinfo:
        plan_targets_from_map({**pathway_map, "pathways": []}, "http://base.test")
    assert "no pathways" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Ported helpers
# ---------------------------------------------------------------------------


def test_view_inferred_from_location():
    from capturd.shots.pathway_plan import _infer_view_from_location

    assert _infer_view_from_location("hub/ui_kits/app/BoardView.jsx:188") == "board"
    assert _infer_view_from_location("C:\\Users\\dev\\src\\RestedCaptureView.jsx:1") == "capture"
    assert _infer_view_from_location("somewhere/unknown.txt") is None


def test_route_path_extracted():
    from capturd.shots.pathway_plan import _extract_route_path

    assert _extract_route_path("`GET /settings`") == "/settings"
    assert _extract_route_path('fetch(url + "/api/state")') == "/api/state"
    assert _extract_route_path("Home painted") is None