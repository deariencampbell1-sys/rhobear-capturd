"""W3-06g-r1 — content copies at depth, typed refusals, an enforced hint.

Red-first proof for F110–F115: a nested write through a `transition` record
must not reach the source (F110); a `set` inside a registry entry must not be
aliased and keeps its concrete type (F111); the approval mutators must refuse
a non-dict record with `ApprovalError`, never `AttributeError` (F112);
`validate_take` must enforce the `jobId: str` half of `persistence.key`'s hint
(F113); `validate_take` must return the very object it was handed without
rewriting it (F114); and a state name counts as mentioned only when it is a
string constant, not when a comment happens to use the word (F115).

The record surface — state names, field names, return types — is asserted here
so a later refactor notices if it moves.
"""

from __future__ import annotations

import copy

import pytest

from capturd.engine import approval, artifacts, persistence, takes
from capturd.engine.approval import ApprovalError
from capturd.engine.takes import TakeError, transition, validate_take

REVISION = "a" * 40
APPROVED_AT = "2026-10-31T13:00:00Z"
REVOKED_AT = "2026-10-31T15:00:00Z"
CREATED_AT = "2026-10-31T12:00:00Z"


def make_take(state="ready", shot=0, take=0, **overrides):
    """A §3-shaped take record; `approved` carries who and when."""
    record = {
        "shotIndex": shot,
        "takeIndex": take,
        "state": state,
        "jobId": "job_1",
        "attemptId": f"att_{shot}_{take}",
        "sourceRevision": REVISION,
        "sourceArtifactId": "art_source",
        "kind": "capturd.take",
        "direction": {"caption": "the cursor lands", "voiceover": "then it clicks"},
        "durationMs": 4820,
        "createdAt": CREATED_AT,
        "approvedBy": None,
        "approvedAt": None,
        "rejectedReason": None,
        "revokedBy": None,
        "revokedAt": None,
        "revokedReason": None,
    }
    if state == "approved":
        record["approvedBy"] = "Niobe"
        record["approvedAt"] = APPROVED_AT
    record.update(overrides)
    return record


# --------------------------------------------------------------------------- /
# F110 — a nested write through a transition never reaches the source


def test_a_nested_write_through_a_transition_does_not_reach_the_source():
    source = make_take(state="draft")
    source["direction"] = {
        "caption": "the cursor lands",
        "camera": {"semantic": {"label": "push in"}, "lens": "35mm"},
    }
    snapshot = copy.deepcopy(source)
    moved = transition(source, "ready")
    assert moved["direction"] == source["direction"]
    # one level down: direction.camera is a fresh mapping
    moved["direction"]["camera"]["lens"] = "85mm"
    # two levels down: direction.camera.semantic is a fresh mapping too
    moved["direction"]["camera"]["semantic"]["label"] = "pull out"
    assert source == snapshot
    assert source["direction"]["camera"]["lens"] == "35mm"
    assert source["direction"]["camera"]["semantic"]["label"] == "push in"


def test_an_empty_direction_is_copied_not_shared():
    """Edge case 1: an empty `direction` is still a fresh mapping."""
    source = make_take(state="draft")
    source["direction"] = {}
    moved = transition(source, "ready")
    assert moved["direction"] == {}
    assert moved["direction"] is not source["direction"]


def test_a_non_mapping_direction_is_passed_through_unchanged():
    """Edge case 1: a direction that is not a mapping is neither refused nor
    converted — the value keeps its type, and the copy still detaches it."""
    source = make_take(state="draft")
    source["direction"] = ["caption", "camera"]
    moved = transition(source, "ready")
    assert moved["direction"] == ["caption", "camera"]
    assert type(moved["direction"]) is list
    source["direction"].append("voiceover")
    assert moved["direction"] == ["caption", "camera"]


# --------------------------------------------------------------------------- /
# F111 — a set inside a registry entry is copied, not aliased


def test_a_set_inside_a_registry_entry_is_not_aliased():
    tags = {"poster", "final"}
    registry = {"art_1": {"revision": "rev-1", "kind": "image/png", "tags": tags}}
    grown = artifacts.register(registry, artifact_id="art_2", revision="rev-2",
                               kind="video/mp4")
    assert grown["art_1"]["tags"] is not tags  # identity, not equality
    assert grown["art_1"]["tags"] == tags
    grown["art_1"]["tags"].add("mutated")
    assert "mutated" not in tags
    assert "mutated" not in registry["art_1"]["tags"]


def test_a_set_survives_the_copy_with_its_concrete_type():
    """Edge case 3: `frozenset` and a `set` subclass keep their own type."""
    class TagSet(set):
        pass

    frozen = frozenset({"a", "b"})
    subclassed = TagSet({"c"})
    registry = {"art_1": {"revision": "rev-1", "kind": "image/png",
                          "frozen": frozen, "subclassed": subclassed}}
    copied = artifacts.register(registry, artifact_id="art_2", revision="rev-2",
                                kind="video/mp4")
    assert type(copied["art_1"]["frozen"]) is frozenset
    assert type(copied["art_1"]["subclassed"]) is TagSet
    assert copied["art_1"]["frozen"] == frozen
    assert copied["art_1"]["subclassed"] == subclassed
    assert copied["art_1"]["frozen"] is not frozen
    assert copied["art_1"]["subclassed"] is not subclassed


def test_a_set_reached_through_a_list_inside_a_mapping_is_copied():
    """Edge case 2: the recursion reaches a set inside a list inside a mapping."""
    shared = {"a", "b"}
    registry = {"art_1": {"revision": "rev-1", "kind": "image/png",
                          "renditions": [{"name": "poster", "tags": shared}]}}
    grown = artifacts.register(registry, artifact_id="art_2", revision="rev-2",
                               kind="video/mp4")
    assert grown["art_1"]["renditions"][0]["tags"] is not shared
    grown["art_1"]["renditions"][0]["tags"].add("c")
    assert shared == {"a", "b"}


def test_unknown_registry_keys_are_preserved_through_the_copy():
    """Edge case 4: a copy path never drops a key it did not expect."""
    entry = {"revision": "rev-1", "kind": "image/png", "extra": {"nested": 1}}
    registry = {"art_1": entry}
    grown = artifacts.register(registry, artifact_id="art_2", revision="rev-2",
                               kind="video/mp4")
    assert grown["art_1"]["extra"] == {"nested": 1}
    assert grown["art_1"]["extra"] is not entry["extra"]


# --------------------------------------------------------------------------- /
# F112 — the mutators refuse a non-dict with the module's own error


@pytest.mark.parametrize(
    "mutator, kwargs",
    [
        pytest.param(approval.approve,
                     {"by": "Niobe", "at": APPROVED_AT, "source_revision": REVISION},
                     id="approve"),
        pytest.param(approval.revoke,
                     {"by": "Morpheus", "at": REVOKED_AT, "reason": "camera drifted"},
                     id="revoke"),
        pytest.param(approval.reject, {"reason": "caption wrong"}, id="reject"),
    ],
)
def test_the_mutators_refuse_a_non_dict_record_with_their_own_error(mutator, kwargs):
    for broken in (["x"], None, 7, 3.5, "approved", ("ready",)):
        with pytest.raises(ApprovalError) as exc:
            mutator(broken, **kwargs)
        assert "dict" in str(exc.value), broken


# --------------------------------------------------------------------------- /
# F113 — the `jobId: str` half of `persistence.key`'s hint is enforced


def test_a_non_string_job_id_is_refused_or_the_hint_is_gone():
    """`key` promises `tuple[str, int, int]`; `validate_take` enforces the
    string. The empty string still passes — the hint says `str`, not non-blank."""
    record = make_take()
    for bad in (5, None, 3.5, b"job_1", ["job_1"], True):
        with pytest.raises(TakeError):
            validate_take({**record, "jobId": bad})
        with pytest.raises(TakeError):
            persistence.key({**record, "jobId": bad})
    key = persistence.key(record)
    assert key == ("job_1", 0, 0)
    assert type(key[0]) is str


# --------------------------------------------------------------------------- /
# F114 — validation returns the very object, unchanged; and the surface holds


def test_validate_take_returns_the_record_itself_and_does_not_rewrite_it():
    record = make_take()
    snapshot = copy.deepcopy(record)
    assert validate_take(record) is record  # never a mutated copy
    assert record == snapshot  # never an in-place rewrite
    # Edge case 5: twice on the same object introduces no copy and no alias.
    assert validate_take(record) is record
    assert record == snapshot


def test_the_module_surface_is_still_the_documented_one():
    """Edge case 6: state names, field names and the key shape do not move."""
    draft = make_take(state="draft")
    moved = transition(draft, "ready")
    assert tuple(takes.STATES) == ("draft", "ready", "approved", "failed", "unresolved")
    assert set(moved) == set(draft) == set(takes.RECORD_KEYS)
    assert moved["state"] == "ready"
    assert persistence.key(draft) == ("job_1", 0, 0)
    assert isinstance(persistence.key(draft), tuple) and len(persistence.key(draft)) == 3
    returned = approval.approve(moved, by="Niobe", at=APPROVED_AT,
                                source_revision=REVISION)
    assert set(returned) == set(takes.RECORD_KEYS)
    assert returned["state"] == "approved"
