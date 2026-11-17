"""Edited content reopens the review without erasing take history."""
from capturd.engine.take_store import TakeStore
from test_take_store import _forge


def test_edit_reopens_approved_takes(tmp_path):
    forge = _forge(tmp_path)
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    store.approve("demo1", 1, 0, by="Neo", expected_revision=takes[1]["sourceRevision"])
    forge.reorder_steps("demo1", [1, 0])
    review = store.review("demo1")
    assert review["takes"][1]["state"] == "ready"
    assert review["history"][-1]["from"] == "approved"
    assert len(review["takes"]) == 2


def test_unapproved_review_is_retained_when_edited(tmp_path):
    forge = _forge(tmp_path)
    store = TakeStore(forge)
    before = store.open_review("demo1")
    forge.trim_steps("demo1", 0, 0)
    after = store.review("demo1")
    assert after is not None
    assert after["contentRevision"] != before["contentRevision"]
    assert len(after["takes"]) == 2
    assert after["selection"] == {0: 0}
