"""W2-05c — deterministic structural audit + safe optimization (neutral extraction).

Every behaviour in the ownership list gets a test here, edge cases first:

* ``capturd/walk/audit.py`` — deterministic structural findings that cite
  concrete spec evidence (dwell ms, hold ms, geometry, choice ids); a page
  with no choices audits as a valid empty result, not an error; NO
  behavioral/engagement result is ever fabricated when no observed data
  exists — the honest absence note says so instead.
* ``capturd/walk/optimize.py`` — dry-run by default (plan only, the input
  spec is never mutated, not even with ``apply=True`` which returns an
  optimized copy); caption whitespace cleanup, sentence-boundary copy
  shortening and bounded HOLD trims with before/after plan; a trim that
  would drop a hold below its floor is clamped and reported; steps,
  branches, media and claims are never touched — unsafe items surface as
  ``approval_required``.
* MCP adapters — ``demo.branch`` choices mode (neutral three-key choices,
  exactly-one-of guard), ``demo.audit``, ``demo.optimize`` (apply=True
  creates a NEW draft through 05b's ``revisions.py``; the live demo.json and
  any approved/published revision stay byte-identical; applying against an
  approved revision is REFUSED and a new draft is offered), and the neutral
  ``demo.revision`` list/get/save/approve/restore surface.
* source scan — no personalization, tracked-send, CRM, attribution-token,
  AWS or hosted-analytics import was introduced in the owned product files.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from capturd.walk.audit import audit_spec
from capturd.walk.optimize import optimize_spec
from capturd.walk.revisions import RevisionStore, hash_spec

FIXTURES = Path(__file__).parent / "fixtures" / "neutral-v2" / "audit"
PRODUCT = Path(__file__).resolve().parents[1]

#: Product files this task owns — the scan target for excluded layers.
OWNED_PRODUCT_SOURCES = (
    "capturd/walk/audit.py",
    "capturd/walk/optimize.py",
    "capturd/mcp/server.py",
)

#: Only these imports are allowed in the two new engine modules — anything
#: else (cloud SDKs, HTTP clients, analytics) fails this scan.
ALLOWED_ENGINE_IMPORTS = {"__future__", "copy", "re", "typing"}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _run(coro):
    return asyncio.run(coro)


def _call(server, tool: str, **args) -> dict:
    """Call an MCP tool in-process; returns its structured result."""

    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args)

    return _run(go()).structured_content  # type: ignore[return-value]


def _call_error(server, tool: str, **args) -> str:
    """Call an MCP tool expecting a refusal; returns the error text."""

    async def go():
        async with Client(server) as client:
            try:
                await client.call_tool(tool, args)
            except ToolError as exc:
                return str(exc)
        raise AssertionError(f"expected {tool} to be refused")

    return _run(go())  # type: ignore[return-value]


@pytest.fixture
def forge(tmp_path: Path):
    from capturd.walk.coordinator import DemoForge

    return DemoForge(demos_dir=tmp_path / "demos")


@pytest.fixture
def server(forge):
    from capturd.mcp.server import _build_server

    return _build_server(forge=forge)


@pytest.fixture
def store_for(forge):
    """RevisionStore rooted where the MCP adapters put it for a demo."""

    def _make(demo_id: str) -> RevisionStore:
        return RevisionStore(forge.demo_dir(demo_id) / "revisions")

    return _make


# ---------------------------------------------------------------------------
# Audit — deterministic structure, evidence-backed, never fabricated
# ---------------------------------------------------------------------------


def test_audit_clean_spec_scores_high():
    a = audit_spec(_fixture("spec_audit_clean.json"))
    assert a["overall"] >= 80
    assert a["behavioral_data"] is False
    assert not any(f["severity"] == "high" for f in a["findings"])


def test_audit_no_choices_is_valid_not_an_error():
    """Edge case 1: a page — and a whole spec — with no choices audits fine."""
    a = audit_spec(_fixture("spec_audit_clean.json"))
    assert a["scores"]["branch_design"] == 100  # nothing to flag
    empty = audit_spec({"version": 1, "id": "x", "steps": []})
    assert 0 <= empty["overall"] <= 100
    assert empty["findings"], "the honest absence note must still appear"


def test_audit_flags_silent_steps_duplicate_narration_missing_hotspot():
    a = audit_spec(_fixture("spec_audit_silent.json"))
    msgs = [f["message"] for f in a["findings"]]
    assert any("duplicates the visible hotspot" in m for m in msgs)
    assert any("no narration" in m for m in msgs)
    assert any("no hotspot" in m for m in msgs)
    assert a["scores"]["narration"] < 100
    assert a["scores"]["interaction_clarity"] < 100


def test_audit_findings_cite_spec_evidence():
    """Every flagged number traces back to the spec — dwell and hold ms included."""
    spec = _fixture("spec_audit_silent.json")
    a = audit_spec(spec)
    holds = [f for f in a["findings"]
             if f["category"] == "pacing" and "hold" in f["message"]
             and "keyframe" in f["message"]]
    fixture_holds = [kf["duration"] for kf in spec["aiAnnotations"]["animationTimeline"]
                     if kf["action"] == "hold"]
    assert holds and all(
        isinstance(f.get("hold_ms"), int) and f["hold_ms"] in fixture_holds
        for f in holds)
    long = [f for f in a["findings"] if f["category"] == "pacing"
            and isinstance(f.get("dwell_ms"), int)]
    assert long and all(f["dwell_ms"] > 6000 for f in long)


def test_audit_flags_dead_end_choice_loop():
    a = audit_spec(_fixture("spec_audit_loop.json"))
    hit = [f for f in a["findings"] if f["category"] == "branch_design"]
    assert hit and hit[0]["severity"] == "high" and hit[0]["choice_id"] == "loop"
    assert a["scores"]["branch_design"] < 100


def test_audit_flags_mobile_crop_with_geometry_evidence():
    spec = _fixture("spec_audit_clean.json")
    spec["steps"][1]["interaction"]["target"]["boundingRect"] = {
        "x": 5, "y": 300, "width": 30, "height": 50}
    a = audit_spec(spec)
    crop = [f for f in a["findings"] if f["category"] == "mobile_experience"]
    assert crop and isinstance(crop[0]["x_fraction"], float)
    assert crop[0]["x_fraction"] < 0.10


def test_audit_never_fabricates_engagement_results():
    """No observed data ⇒ exactly one honest note; no invented drop-off numbers."""
    a = audit_spec(_fixture("spec_audit_silent.json"))
    eng = [f for f in a["findings"] if f["category"] == "engagement"]
    assert len(eng) == 1 and eng[0]["severity"] == "info"
    assert "unavailable" in eng[0]["message"]
    for banned in ("exit", "dropoff", "reach", "completion", "%"):
        assert banned not in eng[0]["message"].lower()
    for key in ("dropoff_pct", "exits", "reach", "completion_rate", "starts"):
        assert key not in eng[0]
    assert all(f["category"] != "engagement" or f is eng[0] for f in a["findings"])


def test_audit_scores_bounded_and_findings_ordered():
    a = audit_spec(_fixture("spec_audit_silent.json"))
    assert all(0 <= v <= 100 for v in a["scores"].values())
    assert 0 <= a["overall"] <= 100
    sevs = [f["severity"] for f in a["findings"]]
    assert sevs == sorted(sevs, key=lambda s: {"high": 0, "warn": 1, "info": 2}[s])


# ---------------------------------------------------------------------------
# Optimize — dry-run by default, safe plan, reversible apply
# ---------------------------------------------------------------------------


def test_optimize_plan_only_by_default_never_mutates():
    spec = _fixture("spec_optimize_messy.json")
    original = copy.deepcopy(spec)
    out = optimize_spec(spec)  # apply defaults to False — the dry run
    assert out["applied"] is False and out["spec"] is None
    assert spec == original, "dry-run must not mutate the input spec"
    actions = {p["action"] for p in out["plan"]}
    assert {"caption_cleanup", "copy_shorten", "pause_trim"} <= actions
    for entry in out["plan"]:
        assert "before" in entry and "after" in entry
    short = next(p for p in out["plan"] if p["action"] == "copy_shorten")
    assert len(short["after"]) < len(short["before"])
    assert short["after"].endswith(("…", ".", "!", "?")), "never cut mid-word"


def test_optimize_apply_returns_copy_and_keeps_input_intact():
    spec = _fixture("spec_optimize_messy.json")
    original = copy.deepcopy(spec)
    out = optimize_spec(spec, apply=True)
    assert out["applied"] is True and isinstance(out["spec"], dict)
    assert spec == original, "apply must never mutate the input spec"
    applied = out["spec"]
    assert applied["steps"][0]["annotation"] == "Trailing spaces and! punctuation."
    cleanup = next(p for p in out["plan"] if p["action"] == "caption_cleanup")
    assert cleanup["before"] == original["steps"][0]["annotation"]
    assert cleanup["after"] == applied["steps"][0]["annotation"]
    hold = applied["aiAnnotations"]["animationTimeline"][-1]
    assert hold["duration"] == 3500
    # every applied entry is safe; unsafe items are never applied
    assert all(p["safe"] for p in out["plan"] if p["action"] != "approval_required")


def test_optimize_hold_trim_clamped_at_floor():
    """Edge case 3: a trim that would drop a hold below its floor is clamped
    and reported in the plan."""
    spec = _fixture("spec_optimize_messy.json")
    out = optimize_spec(spec, hold_budget_ms=500)
    trim = next(p for p in out["plan"] if p["action"] == "pause_trim")
    assert trim["before"] == 8000
    assert trim["after"] == 1000, "clamped to the hold floor, not the raw budget"
    assert trim["clamped"] is True
    applied = optimize_spec(spec, apply=True, hold_budget_ms=500)["spec"]
    hold = applied["aiAnnotations"]["animationTimeline"][-1]
    assert hold["duration"] == 1000
    # the default budget never binds the floor
    default = optimize_spec(spec)
    assert next(p for p in default["plan"] if p["action"] == "pause_trim")["clamped"] is False


def test_optimize_never_deletes_steps_branches_or_media():
    spec = _fixture("spec_optimize_messy.json")
    spec["steps"][1]["choices"] = [{"id": "loop", "label": "L", "destination": 1}]
    spec["steps"][1]["screenshotPath"] = "assets/shot.png"
    n_steps = len(spec["steps"])
    out = optimize_spec(spec, apply=True)
    assert len(out["spec"]["steps"]) == n_steps, "optimize must never delete steps"
    assert out["spec"]["steps"][1]["choices"] == spec["steps"][1]["choices"]
    assert out["spec"]["steps"][1]["screenshotPath"] == "assets/shot.png"
    approval = [p for p in out["plan"] if p["action"] == "approval_required"]
    assert approval and all(p["safe"] is False for p in approval)
    assert any(p.get("choice_id") == "loop" for p in approval)


def test_optimize_flag_only_for_non_string_annotation():
    spec = _fixture("spec_optimize_messy.json")
    spec["steps"][0]["annotation"] = 42
    out = optimize_spec(spec, apply=True)
    flag = next(p for p in out["plan"] if p["action"] == "flag_only")
    assert flag["safe"] is False
    assert out["spec"]["steps"][0]["annotation"] == 42, "flagged, never rewritten"


# ---------------------------------------------------------------------------
# MCP adapters — neutral choices / audit / optimize / revision only
# ---------------------------------------------------------------------------


def test_mcp_adapter_surface_is_neutral_only(server):
    names = {t.name for t in _run(server.list_tools())}
    assert {"demo.branch", "demo.audit", "demo.optimize", "demo.revision"} <= names
    for banned in ("demo.personalize", "demo.cta", "demo.analytics",
                   "voice.list", "voice.preview", "voice.synthesize",
                   "demo.publish", "demo.track"):
        assert banned not in names, f"{banned} must not be introduced"


def test_mcp_branch_choices_mode_strips_excluded_keys(server, forge):
    forge.save_spec("demo-x", _fixture("spec_audit_clean.json"))
    res = _call(server, "demo.branch", demo_id="demo-x", at_step=1,
                choices=[{"id": "site", "label": "Site", "destination": 2,
                          "analyticsName": "x", "variable": {"a": "b"},
                          "ctaId": "c"}])
    assert res["ok"] is True and res["mode"] == "viewer-choices"
    assert res["choices"] == ["site"]
    stored = forge.load_spec("demo-x")["steps"][1]["choices"]
    assert stored == [{"id": "site", "label": "Site", "destination": 2}], \
        "exactly the three neutral keys survive"


def test_mcp_branch_enforces_exactly_one_of(server, forge):
    forge.save_spec("demo-x", _fixture("spec_audit_clean.json"))
    both = _call_error(server, "demo.branch", demo_id="demo-x", at_step=0,
                       alt_path=[{"index": 9}], choices=[
                           {"id": "a", "label": "A", "destination": 1}])
    assert "exactly one" in both
    neither = _call_error(server, "demo.branch", demo_id="demo-x", at_step=0)
    assert "exactly one" in neither


def test_mcp_audit_and_missing_demo(server, forge):
    forge.save_spec("demo-a", _fixture("spec_audit_silent.json"))
    res = _call(server, "demo.audit", demo_id="demo-a")
    assert res["ok"] is True and 0 <= res["overall"] <= 100
    assert any(f["category"] == "narration" for f in res["findings"])
    assert res["behavioral_data"] is False
    err = _call_error(server, "demo.audit", demo_id="no-such-demo")
    assert "demo not found" in err


def test_mcp_optimize_dry_run_default_touches_nothing(server, forge):
    forge.save_spec("demo-o", _fixture("spec_optimize_messy.json"))
    before = (forge.demo_dir("demo-o") / "demo.json").read_bytes()
    res = _call(server, "demo.optimize", demo_id="demo-o")
    assert res["ok"] is True and res["applied"] is False
    assert any(p["action"] == "pause_trim" for p in res["plan"])
    assert (forge.demo_dir("demo-o") / "demo.json").read_bytes() == before
    assert not (forge.demo_dir("demo-o") / "revisions").exists(), \
        "dry-run must not create a revision store"


def test_mcp_optimize_apply_creates_new_draft_via_revisions(server, forge, store_for):
    """Applying creates a NEW draft through 05b's revisions.py; the live
    demo.json is untouched and the draft awaits review."""
    spec = _fixture("spec_optimize_messy.json")
    forge.save_spec("demo-o", spec)
    demo_bytes = (forge.demo_dir("demo-o") / "demo.json").read_bytes()
    res = _call(server, "demo.optimize", demo_id="demo-o", apply=True)
    assert res["applied"] is True and res["draft"]["sha256"]
    store = store_for("demo-o")
    draft = store.load_draft()
    assert draft is not None and draft != spec
    assert draft["steps"][0]["annotation"] == "Trailing spaces and! punctuation."
    assert draft["aiAnnotations"]["animationTimeline"][-1]["duration"] == 3500
    assert hash_spec(draft) == res["draft"]["sha256"]
    assert (forge.demo_dir("demo-o") / "demo.json").read_bytes() == demo_bytes
    assert store.approved_revision_id() is None, "nothing is approved by applying"


def test_mcp_optimize_apply_refused_on_approved_revision(server, forge, store_for):
    """Edge case 2: apply against an approved revision is refused; a new draft
    is offered instead — and the approved bytes stay byte-identical."""
    spec = _fixture("spec_optimize_messy.json")
    forge.save_spec("demo-r", spec)
    store = store_for("demo-r")
    rec = store.save_revision(spec)
    store.approve(rec.id)
    approved_path = store.revisions_dir / f"{rec.id}.json"
    approved_bytes = approved_path.read_bytes()
    err = _call_error(server, "demo.optimize", demo_id="demo-r",
                      apply=True, revision_id=rec.id)
    assert "approved" in err and "draft" in err, "the refusal must offer a draft"
    assert approved_path.read_bytes() == approved_bytes, \
        "approved revision must stay byte-identical"
    # planning against the approved revision stays allowed and read-only
    res = _call(server, "demo.optimize", demo_id="demo-r", revision_id=rec.id)
    assert res["applied"] is False and res["plan"]
    assert approved_path.read_bytes() == approved_bytes


def test_mcp_optimize_apply_on_unapproved_revision_forks_draft(server, forge, store_for):
    spec = _fixture("spec_optimize_messy.json")
    forge.save_spec("demo-f", spec)
    store = store_for("demo-f")
    rec = store.save_revision(spec)
    rev_bytes = (store.revisions_dir / f"{rec.id}.json").read_bytes()
    res = _call(server, "demo.optimize", demo_id="demo-f",
                apply=True, revision_id=rec.id)
    assert res["applied"] is True and res["draft"]["sha256"]
    draft = store.load_draft()
    assert draft["steps"][0]["annotation"] == "Trailing spaces and! punctuation."
    assert (store.revisions_dir / f"{rec.id}.json").read_bytes() == rev_bytes, \
        "the unapproved revision's frozen bytes stay identical too"


def test_mcp_revision_lifecycle(server, forge, store_for):
    spec = _fixture("spec_audit_clean.json")
    forge.save_spec("demo-v", spec)
    # save with no draft on the store is refused, naming the problem
    err = _call_error(server, "demo.revision", demo_id="demo-v", action="save")
    assert "no draft" in err
    # optimize apply creates a draft even on an already-clean spec
    res = _call(server, "demo.optimize", demo_id="demo-v", apply=True)
    assert res["applied"] is True
    saved = _call(server, "demo.revision", demo_id="demo-v", action="save")
    rid = saved["revision"]["id"]
    assert rid.startswith("rv_") and saved["revision"]["sha256"]
    listing = _call(server, "demo.revision", demo_id="demo-v", action="list")
    assert [r["id"] for r in listing["revisions"]] == [rid]
    assert listing["approved"] is None and listing["published"] is None
    got = _call(server, "demo.revision", demo_id="demo-v",
                action="get", revision_id=rid)
    assert got["spec"] == spec
    approved = _call(server, "demo.revision", demo_id="demo-v",
                     action="approve", revision_id=rid)
    assert approved["approved"] == rid
    restored = _call(server, "demo.revision", demo_id="demo-v",
                     action="restore", revision_id=rid)
    assert restored["restoredFrom"] == rid
    store = store_for("demo-v")
    assert store.load_draft() == spec
    # unknown revision ids are named, never guessed
    missing = _call_error(server, "demo.revision", demo_id="demo-v",
                          action="get", revision_id="rv_999999_" + "0" * 64)
    assert "not found" in missing and "rv_999999_" in missing
    # publish is not a surface here — the action list refuses it
    refused = _call_error(server, "demo.revision", demo_id="demo-v", action="publish")
    assert "action must be" in refused


# ---------------------------------------------------------------------------
# Source scan — no excluded layer was introduced
# ---------------------------------------------------------------------------

_BANNED_ENGINE_MARKERS = (
    "personalize", "ctaId", "analyticsName", "boto3", "polly", "transcribe",
    "frontman", "herald", "utm_", "attribution", "tracked-send", "{{",
)


def test_engine_modules_import_nothing_excluded():
    """audit.py / optimize.py stay stdlib-only deterministic code."""
    for rel in ("capturd/walk/audit.py", "capturd/walk/optimize.py"):
        path = PRODUCT / rel
        text = path.read_text(encoding="utf-8")
        for marker in _BANNED_ENGINE_MARKERS:
            assert marker not in text, f"{rel} mentions excluded marker {marker!r}"
        tree = ast.parse(text)
        seen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                seen.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])
        extra = seen - ALLOWED_ENGINE_IMPORTS
        assert not extra, f"{rel} imports outside the engine allow-list: {sorted(extra)}"


def test_mcp_server_carries_no_excluded_surface():
    """The adapter file must not name the excluded layers (it did not before
    this task and must not after it)."""
    text = (PRODUCT / "capturd/mcp/server.py").read_text(encoding="utf-8")
    for marker in ("personalize", "ctaId", "analyticsName", "boto3", "polly",
                   "transcribe", "frontman", "herald", "utm_", "attribution",
                   "tracked-send", "{{"):
        assert marker not in text, f"server.py mentions excluded marker {marker!r}"
