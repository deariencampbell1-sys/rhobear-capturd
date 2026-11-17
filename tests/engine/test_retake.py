"""GAPS-NOW M4/M6 (JOURNEY E5/E6/E8, contract §4): direction-only retake.

A retake is a new take on the same shot with the exact `sourceRevision`
(same pixels), a validated direction, and the old take kept in history. The
selection moves to the new take only when a person approves it, and the
export uses the selected take's caption. Direction fields that nothing
renders yet are refused by name, never silently dropped.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from capturd.engine import take_store
from capturd.engine.take_store import DirectionError, RevisionConflict, TakeStore
from test_take_store import _forge


def _open(tmp_path):
    forge = _forge(tmp_path)
    store = TakeStore(forge)
    return forge, store, store.open_review("demo1")["takes"]


def test_a_retake_keeps_the_pixels_and_takes_the_new_direction(tmp_path):
    _forge_, store, takes = _open(tmp_path)
    new = store.retake("demo1", 0, 0, direction={"caption": "Figurine is best for printing."},
                       expected_revision=takes[0]["sourceRevision"])
    assert (new["shotIndex"], new["takeIndex"], new["state"]) == (0, 1, "ready")
    assert new["sourceRevision"] == takes[0]["sourceRevision"]
    assert new["sourceArtifactId"] == takes[0]["sourceArtifactId"]
    assert new["direction"]["caption"] == "Figurine is best for printing."
    review = store.review("demo1")
    assert [t["takeIndex"] for t in review["takes"] if t["shotIndex"] == 0] == [0, 1]
    assert review["selection"][0] == 0, "selection waits for a person's approval"


def test_a_retake_leaves_every_other_record_byte_identical(tmp_path):
    forge, store, takes = _open(tmp_path)
    path = forge.demo_dir("demo1") / take_store.FILENAME
    before = json.loads(path.read_text())["takes"]
    store.retake("demo1", 0, 0, direction={"caption": "x"},
                 expected_revision=takes[0]["sourceRevision"])
    after = json.loads(path.read_text())["takes"]
    assert after[:2] == before


def test_approving_the_retake_selects_it_and_the_export_uses_its_caption(tmp_path):
    forge, store, takes = _open(tmp_path)
    new = store.retake("demo1", 0, 0, direction={"caption": "New line."},
                       expected_revision=takes[0]["sourceRevision"])
    store.approve("demo1", 0, 1, by="Neo", expected_revision=new["sourceRevision"])
    store.approve("demo1", 1, 0, by="Neo", expected_revision=takes[1]["sourceRevision"])
    assert store.review("demo1")["selection"] == {0: 1, 1: 0}
    html = forge.export_demo("demo1", fmt="html").read_text()
    assert "New line." in html
    assert forge.load_spec("demo1")["steps"][0]["annotation"] == "Step 0.", \
        "the saved spec is never mutated by a take"


def test_a_stale_revision_retake_is_a_conflict(tmp_path):
    _f, store, _takes = _open(tmp_path)
    with pytest.raises(RevisionConflict):
        store.retake("demo1", 0, 0, direction={"caption": "x"}, expected_revision="a" * 40)
    assert len(store.review("demo1")["takes"]) == 2


@pytest.mark.parametrize("direction, field", [
    ({"caption": 5}, "caption"),
    ({"caption": "x" * 501}, "caption"),
    ({"camera": "dolly"}, "camera"),
    ({"voiceover": "Say this."}, "voiceover"),
    ({"cursor": "glide"}, "cursor"),
    ({}, "direction"),
    ("glide", "direction"),
])
def test_a_bad_direction_names_its_field_and_changes_nothing(tmp_path, direction, field):
    _f, store, takes = _open(tmp_path)
    with pytest.raises(DirectionError) as exc:
        store.retake("demo1", 0, 0, direction=direction,
                     expected_revision=takes[0]["sourceRevision"])
    assert exc.value.field == field
    assert len(store.review("demo1")["takes"]) == 2


def test_a_failed_take_cannot_be_retaken_it_needs_new_pixels(tmp_path):
    forge = _forge(tmp_path, pngs=(b"a", None))
    store = TakeStore(forge)
    takes = store.open_review("demo1")["takes"]
    with pytest.raises(take_store.TakeStoreError, match="record the shot again"):
        store.retake("demo1", 1, 0, direction={"caption": "x"},
                     expected_revision=takes[1]["sourceRevision"])


def test_the_retake_tool(tmp_path):
    from capturd.mcp.server import _build_server

    forge = _forge(tmp_path)
    server = _build_server(forge)

    def call(name, args):
        result = asyncio.run(server.call_tool(name, args))
        return result.structured_content.get("result", result.structured_content)

    takes = call("take.list", {"demo_id": "demo1"})["takes"]
    answer = call("take.retake", {"demo_id": "demo1", "shot_index": 0, "take_index": 0,
                                  "direction": {"caption": "Again.", "note": "slower"},
                                  "expected_revision": takes[0]["sourceRevision"]})
    assert answer["ok"] is True and answer["take"]["takeIndex"] == 1
    bad = call("take.retake", {"demo_id": "demo1", "shot_index": 0, "take_index": 0,
                               "direction": {"effect": "ring"},
                               "expected_revision": takes[0]["sourceRevision"]})
    assert bad["ok"] is False and bad["code"] == "invalid_direction" and bad["field"] == "effect"
