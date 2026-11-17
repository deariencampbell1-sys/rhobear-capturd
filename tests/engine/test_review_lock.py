"""GAPS-NOW W10 (JOURNEY E15, contract §5.2): an approval is never changed under it.

With a review open, any change to the demo's spec (an edit, a camera
keyframe, reorder/trim, optimize, re-enrichment) would change what an
approved take exports. So:
* with any approved take, the change is refused and names the approved
  shots (revoke them first, or use take.retake / take.recapture);
* with no approval yet, the change goes through and the review is closed,
  to be seeded again from the new spec by the next take.list.
"""
from __future__ import annotations

import pytest

from capturd.engine.take_store import TakeStore
from capturd.walk.coordinator import DemoReviewLocked
from test_take_store import _forge


def test_a_change_under_an_approval_is_refused(tmp_path):
    forge = _forge(tmp_path)
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    store.approve("demo1", 1, 0, by="Neo", expected_revision=takes[1]["sourceRevision"])
    before = forge.load_spec("demo1")
    with pytest.raises(DemoReviewLocked, match="shot 1"):
        forge.reorder_steps("demo1", [1, 0])
    with pytest.raises(DemoReviewLocked):
        forge.append_animation_keyframe("demo1", 0, "hold", duration=500)
    with pytest.raises(DemoReviewLocked):
        forge.enrich_demo("demo1")
    assert forge.load_spec("demo1") == before
    assert store.review("demo1")["takes"][1]["state"] == "approved"


def test_a_change_before_any_approval_closes_the_review(tmp_path):
    forge = _forge(tmp_path)
    store = TakeStore(forge)
    store.open_review("demo1")
    forge.trim_steps("demo1", 0, 0)
    assert store.review("demo1") is None
    assert len(store.open_review("demo1")["takes"]) == 1


def test_after_revoking_the_change_goes_through(tmp_path):
    forge = _forge(tmp_path)
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    store.approve("demo1", 0, 0, by="Neo", expected_revision=takes[0]["sourceRevision"])
    store.revoke("demo1", 0, 0, by="Neo", reason="change the camera")
    forge.append_animation_keyframe("demo1", 0, "hold", duration=500)
    assert store.review("demo1") is None


def test_the_mcp_edit_names_the_lock(tmp_path):
    import asyncio

    from capturd.mcp.server import _build_server

    forge = _forge(tmp_path)
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    store.approve("demo1", 0, 0, by="Neo", expected_revision=takes[0]["sourceRevision"])
    server = _build_server(forge)
    with pytest.raises(Exception, match="approved"):
        asyncio.run(server.call_tool("demo.hold", {"demo_id": "demo1", "step_index": 0, "ms": 400}))
