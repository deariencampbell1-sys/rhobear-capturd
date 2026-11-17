"""GAPS-NOW U1/U2/M2/W7: the take model reaches people and agents.

The local adapter keeps one take file per demo (the demo id is the job id
locally), written atomically through `persistence.dumps`. A review opens by
seeding one take per step; from then on export is gated by `export_blocked`
over the selected takes (contract §6), and approval follows §5 with
`expectedRevision` conflicts per §4.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from capturd.engine import take_store
from capturd.walk.coordinator import DemoExportBlocked, DemoForge

PNG_A = b"png-bytes-a"
PNG_B = b"png-bytes-b"


def _step(index: int, png: bytes | None) -> dict:
    return {
        "index": index,
        "timestamp": index * 1000,
        "pageUrl": "https://example.com",
        "pageTitle": "t",
        "interaction": {
            "type": "click",
            "target": {"selector": "body", "tagName": "", "text": "x",
                       "boundingRect": {"x": 0, "y": 0, "width": 16, "height": 16}},
            "hotspot": {"xPct": 50, "yPct": 50},
        },
        "annotation": f"Step {index}.",
        "screenshotBase64": base64.b64encode(png).decode("ascii") if png else None,
        "screenshotPath": None,
        **({} if png else {"screenshotError": "selector not found"}),
    }


def _forge(tmp_path, pngs=(PNG_A, PNG_B)) -> DemoForge:
    forge = DemoForge(demos_dir=tmp_path / "demos")
    forge.save_spec("demo1", {
        "version": 1, "id": "demo1", "name": "demo1", "goal": "g",
        "createdAt": "2026-11-17T00:00:00Z",
        "viewport": {"width": 16, "height": 16},
        "startUrl": "https://example.com",
        "aiAnnotations": {"style": "snappy", "summary": "", "animationTimeline": []},
        "steps": [_step(i, png) for i, png in enumerate(pngs)],
    })
    return forge


def _store(forge) -> take_store.TakeStore:
    return take_store.TakeStore(forge)


def test_opening_review_seeds_one_take_per_step(tmp_path):
    store = _store(_forge(tmp_path))
    review = store.open_review("demo1")
    takes = review["takes"]
    assert [(t["shotIndex"], t["takeIndex"]) for t in takes] == [(0, 0), (1, 0)]
    assert all(t["state"] == "ready" and t["jobId"] == "demo1" for t in takes)
    assert takes[0]["sourceRevision"] == hashlib.sha1(PNG_A).hexdigest()
    assert takes[1]["sourceRevision"] == hashlib.sha1(PNG_B).hexdigest()
    assert review["selection"] == {0: 0, 1: 0}


def test_a_step_without_pixels_is_a_failed_take_never_ready(tmp_path):
    store = _store(_forge(tmp_path, pngs=(PNG_A, None)))
    takes = store.open_review("demo1")["takes"]
    assert takes[1]["state"] == "failed"
    assert takes[1]["rejectedReason"] is None


def test_opening_review_twice_keeps_the_first_take_set(tmp_path):
    store = _store(_forge(tmp_path))
    first = store.open_review("demo1")
    store.approve("demo1", 0, 0, by="Neo", expected_revision=first["takes"][0]["sourceRevision"])
    again = store.open_review("demo1")
    assert again["takes"][0]["state"] == "approved"


def test_the_take_set_survives_a_restart(tmp_path):
    forge = _forge(tmp_path)
    store = _store(forge)
    rev = store.open_review("demo1")["takes"][0]["sourceRevision"]
    store.approve("demo1", 0, 0, by="Neo", expected_revision=rev)
    reborn = take_store.TakeStore(DemoForge(demos_dir=tmp_path / "demos"))
    assert reborn.review("demo1")["takes"][0]["approvedBy"] == "Neo"


def test_the_file_holds_canonical_take_records(tmp_path):
    forge = _forge(tmp_path)
    _store(forge).open_review("demo1")
    doc = json.loads((forge.demo_dir("demo1") / take_store.FILENAME).read_text())
    from capturd.engine import persistence
    for text in doc["takes"]:
        assert persistence.dumps(persistence.loads(text)) == text


def test_approve_needs_a_named_human(tmp_path):
    store = _store(_forge(tmp_path))
    rev = store.open_review("demo1")["takes"][0]["sourceRevision"]
    with pytest.raises(take_store.TakeStoreError):
        store.approve("demo1", 0, 0, by="  ", expected_revision=rev)


def test_a_stale_expected_revision_is_a_conflict_and_changes_nothing(tmp_path):
    store = _store(_forge(tmp_path))
    actual = store.open_review("demo1")["takes"][0]["sourceRevision"]
    with pytest.raises(take_store.RevisionConflict) as exc:
        store.approve("demo1", 0, 0, by="Neo", expected_revision="0" * 40)
    assert exc.value.payload() == {
        "code": "conflict",
        "details": {"reason": "revision_conflict", "expected": "0" * 40, "actual": actual},
    }
    assert store.review("demo1")["takes"][0]["state"] == "ready"


def test_approving_one_take_leaves_every_other_record_byte_identical(tmp_path):
    forge = _forge(tmp_path)
    store = _store(forge)
    review = store.open_review("demo1")
    before = json.loads((forge.demo_dir("demo1") / take_store.FILENAME).read_text())["takes"][1]
    store.approve("demo1", 0, 0, by="Neo", expected_revision=review["takes"][0]["sourceRevision"])
    after = json.loads((forge.demo_dir("demo1") / take_store.FILENAME).read_text())["takes"][1]
    assert after == before


def test_reject_and_revoke_record_why(tmp_path):
    store = _store(_forge(tmp_path))
    takes = store.open_review("demo1")["takes"]
    rejected = store.reject("demo1", 1, 0, reason="pointer misses the button")
    assert rejected["rejectedReason"] == "pointer misses the button"
    store.approve("demo1", 0, 0, by="Neo", expected_revision=takes[0]["sourceRevision"])
    revoked = store.revoke("demo1", 0, 0, by="Neo", reason="wrong page")
    assert revoked["state"] == "ready" and revoked["revokedReason"] == "wrong page"


def test_an_unknown_take_is_named(tmp_path):
    store = _store(_forge(tmp_path))
    store.open_review("demo1")
    with pytest.raises(take_store.TakeStoreError, match="shot 7"):
        store.reject("demo1", 7, 0, reason="x")


# --- the export gate (contract §6, GAPS-NOW U2/W7) ---------------------------

def test_a_demo_without_a_review_exports_as_before(tmp_path):
    forge = _forge(tmp_path)
    assert forge.export_demo("demo1", fmt="html").is_file()


def test_export_is_blocked_until_every_selected_take_is_approved(tmp_path):
    forge = _forge(tmp_path)
    store = _store(forge)
    takes = store.open_review("demo1")["takes"]
    store.approve("demo1", 0, 0, by="Neo", expected_revision=takes[0]["sourceRevision"])
    with pytest.raises(DemoExportBlocked) as exc:
        forge.export_demo("demo1", fmt="html")
    blocks = exc.value.blocks
    assert {"code": "included_not_approved", "shotIndex": 1, "takeIndex": 0,
            "needed": "the included take approved"} in blocks
    assert not (forge.demo_dir("demo1") / "export.html").exists()
    store.approve("demo1", 1, 0, by="Neo", expected_revision=takes[1]["sourceRevision"])
    assert forge.export_demo("demo1", fmt="html").is_file()


def test_a_failed_take_blocks_export_by_name(tmp_path):
    forge = _forge(tmp_path, pngs=(PNG_A, None))
    _store(forge).open_review("demo1")
    with pytest.raises(DemoExportBlocked) as exc:
        forge.export_demo("demo1", fmt="html")
    assert any(b["code"] == "take_failed" and b["shotIndex"] == 1 for b in exc.value.blocks)


# --- the @capturd tools -------------------------------------------------------

def _call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    content = getattr(result, "structured_content", None)
    if content is not None:
        return content.get("result", content)
    return json.loads(result.content[0].text)


def test_the_take_tools_review_approve_and_gate_export(tmp_path):
    from capturd.mcp.server import _build_server

    forge = _forge(tmp_path)
    server = _build_server(forge)
    review = _call(server, "take.list", {"demo_id": "demo1"})
    assert review["ok"] is True and len(review["takes"]) == 2
    blocked = _call(server, "demo.export", {"demo_id": "demo1", "format": "html"})
    assert blocked["ok"] is False and blocked["error"] == "export_blocked"
    assert {b["shotIndex"] for b in blocked["blocks"]} == {0, 1}
    for take in review["takes"]:
        answer = _call(server, "take.approve", {
            "demo_id": "demo1", "shot_index": take["shotIndex"], "take_index": 0,
            "approved_by": "Neo", "expected_revision": take["sourceRevision"]})
        assert answer["ok"] is True and answer["take"]["state"] == "approved"
    conflict = _call(server, "take.approve", {
        "demo_id": "demo1", "shot_index": 0, "take_index": 0,
        "approved_by": "Neo", "expected_revision": "f" * 40})
    assert conflict["ok"] is False and conflict["code"] == "conflict"
    assert conflict["details"]["reason"] == "revision_conflict"
    exported = _call(server, "demo.export", {"demo_id": "demo1", "format": "html"})
    assert exported.get("ok", True) is not False
