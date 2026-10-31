"""W3-06c-r1 — shot and take content: states, approval, export-blocked.

The §3 take record and the §2 state machine are a closed, pure content model
on top of L8d: five states, approval bound to a named human and an exact
`sourceRevision`, retake keeping the revision, recapture refusing it, and an
export-blocked rule that names every shot, take and need. Ruling D: the §4
`expectedRevision` pass-through (`conflict` + `details.reason:
'revision_conflict'`) is the L8d command layer's, unproven in the control
plane today — it is implemented nowhere here and deliberately not tested.

All five modules are pure: no disk, clock, randomness, environment, network,
logging or global state; a function returns a new dict and never mutates its
input. `test_modules_stay_pure` holds that line with `ast`.

Round 2 (W3-06c-r2 fix list): the §2 paths and refusals are written out as
literal calls so no test mirrors the production table; an ambiguous take set
is a caller error (`AmbiguousTakeSet`), the mutators validate what they
return, an `approved` take carries no revocation, `reject` demands a reason,
`approve` demands a real `at`, the registry shares nothing it returns,
stray `included` entries and non-canonical text (`NaN`, `Infinity`) are
refused, and `transition` refuses a non-string target.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from capturd.engine import approval, artifacts, persistence, shots, takes
from capturd.engine.approval import ApprovalError
from capturd.engine.artifacts import ArtifactError
from capturd.engine.shots import export_blocked, recapture, retake
from capturd.engine.takes import STATES, TakeError, transition, validate_take

REVISION = "a" * 40
OTHER_REVISION = "f" * 40
CREATED_AT = "2026-10-31T12:00:00Z"
APPROVED_AT = "2026-10-31T13:00:00Z"
APPROVED_AT_2 = "2026-10-31T14:00:00Z"
REVOKED_AT = "2026-10-31T15:00:00Z"
DIRECTION = {"caption": "the cursor lands", "voiceover": "then it clicks", "camera": "semantic"}

ENGINE_DIR = Path(__file__).resolve().parents[2] / "capturd" / "engine"
CONTENT_MODULES = ("takes.py", "approval.py", "shots.py", "artifacts.py", "persistence.py")

#: The explicit standard-library allowlist for the five content modules, plus
#: level-1 relative imports inside capturd.engine. Nothing else may appear.
_ALLOWED_IMPORTS = {"__future__", "typing", "json"}
_BANNED_IMPORTS = {"os", "pathlib", "time", "datetime", "random", "uuid", "logging", "socket"}

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
        "direction": dict(DIRECTION),
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


def codes_of(blocks):
    return [block["code"] for block in blocks]


# --------------------------------------------------------------------------- /
# 1. States and the §2 transition table


def test_states_are_the_closed_five():
    assert tuple(STATES) == ("draft", "ready", "approved", "failed", "unresolved")
    assert len(set(STATES)) == 5


def test_section_2_paths():
    """Every §2 path, written out as literal calls — no table is copied here.

    A copy of the production table proves nothing (r1: deleting
    `("draft", "ready")` from both stayed green), so each legal move is a
    literal call and the refusals live in `test_section_2_refusals`.
    """
    # draft → ready → approve → revoke → ready: the review loop.
    record = make_take(state="draft", shot=0, take=0)
    ready = transition(record, "ready")
    assert ready["state"] == "ready"
    approved = approval.approve(ready, by="Niobe", at=APPROVED_AT, source_revision=REVISION)
    assert approved["state"] == "approved"
    revoked = approval.revoke(approved, by="Morpheus", at=REVOKED_AT, reason="camera drifted")
    assert revoked["state"] == "ready"
    assert revoked["revokedReason"] == "camera drifted"
    assert record["state"] == "draft"  # the input is never mutated
    # draft → failed
    assert transition(make_take(state="draft", shot=1, take=0), "failed")["state"] == "failed"
    # draft → unresolved → ready: the reconcile path (§2).
    unresolved = transition(make_take(state="draft", shot=2, take=0), "unresolved")
    assert transition(unresolved, "ready")["state"] == "ready"
    # unresolved → failed
    assert transition(make_take(state="unresolved", shot=3, take=0), "failed")["state"] == "failed"
    # ready → failed
    assert transition(make_take(state="ready", shot=4, take=0), "failed")["state"] == "failed"
    # ready → unresolved
    assert transition(make_take(state="ready", shot=5, take=0), "unresolved")["state"] == "unresolved"


def test_section_2_refusals():
    """Every refused move, written out as a literal list — no shared table.

    Staying put is refused; entering or leaving `approved` is approval.py's
    alone; terminal `failed` moves nowhere; `draft` is never re-entered.
    """
    refused = [
        ("draft", "draft"), ("draft", "approved"),
        ("ready", "draft"), ("ready", "ready"), ("ready", "approved"),
        ("approved", "draft"), ("approved", "ready"), ("approved", "approved"),
        ("approved", "failed"), ("approved", "unresolved"),
        ("failed", "draft"), ("failed", "ready"), ("failed", "approved"),
        ("failed", "failed"), ("failed", "unresolved"),
        ("unresolved", "draft"), ("unresolved", "unresolved"), ("unresolved", "approved"),
    ]
    for frm, to in refused:
        with pytest.raises(TakeError):
            transition(make_take(state=frm, shot=0, take=0), to)


def test_transition_refuses_a_non_string_target():
    record = make_take(state="ready", shot=0, take=0)
    for bad_to in (["ready"], {"to": "ready"}, None, 3):  # unhashable included
        with pytest.raises(TakeError):
            transition(record, bad_to)
    assert record["state"] == "ready"  # nothing moved, nothing raised raw TypeError


# --------------------------------------------------------------------------- /
# 2. Approval binds a named human and the exact revision


def test_approve_binds_named_human_and_revision():
    record = make_take(state="ready", shot=2, take=1)
    original = copy.deepcopy(record)
    with pytest.raises(ApprovalError):
        approval.approve(record, by="   ", at=APPROVED_AT, source_revision=REVISION)
    with pytest.raises(ApprovalError):
        approval.approve(record, by="Niobe", at=APPROVED_AT, source_revision="b" * 40)
    for state in ("draft", "approved", "failed", "unresolved"):
        with pytest.raises(ApprovalError):
            approval.approve(make_take(state=state, shot=2, take=1), by="Niobe",
                             at=APPROVED_AT, source_revision=REVISION)
    approved = approval.approve(record, by="Niobe", at=APPROVED_AT, source_revision=REVISION)
    assert approved["state"] == "approved"
    assert approved["approvedBy"] == "Niobe"
    assert approved["approvedAt"] == APPROVED_AT
    assert record == original  # approve returned a new dict


def test_approve_refuses_a_blank_or_non_string_time():
    """Apollo HIGH (E-1031-060031-e22f): the `at` refusal gets its own test.

    The refusal itself is r1 code; this pins it so a blank or non-string
    `approvedAt` can never slip back in.
    """
    record = make_take(state="ready", shot=0, take=0)
    for bad_at in ("", "   ", "\t\n", 5, None):
        with pytest.raises(ApprovalError):
            approval.approve(record, by="Niobe", at=bad_at, source_revision=REVISION)
    assert record["state"] == "ready"  # nothing moved
    approved = approval.approve(record, by="Niobe", at=APPROVED_AT, source_revision=REVISION)
    assert approved["approvedAt"] == APPROVED_AT  # a valid time still approves


# --------------------------------------------------------------------------- /
# 3. Revocation records who, when and why; re-approval clears it


def test_revoke_records_reason_and_time():
    approved = approval.approve(make_take(state="ready", shot=0, take=0), by="Niobe",
                                at=APPROVED_AT, source_revision=REVISION)
    original = copy.deepcopy(approved)
    for blank in ({"by": ""}, {"at": "  "}, {"reason": "\t"}):
        kwargs = {"by": "Morpheus", "at": REVOKED_AT, "reason": "camera drifted"}
        kwargs.update(blank)
        with pytest.raises(ApprovalError):
            approval.revoke(approved, **kwargs)
    with pytest.raises(ApprovalError):
        approval.revoke(make_take(state="ready", shot=0, take=0), by="Morpheus",
                        at=REVOKED_AT, reason="camera drifted")
    revoked = approval.revoke(approved, by="Morpheus", at=REVOKED_AT, reason="camera drifted")
    assert revoked["state"] == "ready"
    assert revoked["revokedBy"] == "Morpheus"
    assert revoked["revokedAt"] == REVOKED_AT
    assert revoked["revokedReason"] == "camera drifted"
    assert revoked["approvedBy"] == "Niobe"  # kept as history (§5.4)
    assert revoked["approvedAt"] == APPROVED_AT
    assert approved == original
    # Edge case 3: a revoked take is ready, so if it is included the export blocks.
    blocks = export_blocked([0], [revoked], {0: 0}, {(0, 0)})
    assert codes_of(blocks) == ["included_not_approved"]
    again = approval.approve(revoked, by="Niobe", at=APPROVED_AT_2, source_revision=REVISION)
    assert again["state"] == "approved"
    assert again["revokedBy"] is None
    assert again["revokedAt"] is None
    assert again["revokedReason"] is None
    assert again["approvedBy"] == "Niobe"
    assert again["approvedAt"] == APPROVED_AT_2
    assert validate_take(again) == again  # edge case 3: re-approval validates
    assert export_blocked([0], [again], {0: 0}, {(0, 0)}) == []


def test_reject_requires_a_real_reason():
    """A blank or non-string `reason` is refused: an empty `rejectedReason`
    is not null, so it would block the export forever (§6)."""
    record = make_take(state="ready", shot=0, take=0)
    for bad_reason in ("", "   ", "\t", 7, None):
        with pytest.raises(ApprovalError):
            approval.reject(record, reason=bad_reason)
    assert record["state"] == "ready" and record["rejectedReason"] is None
    # Edge case 2: on a take that is not ready the state error still comes first.
    for bad_reason in ("", "   ", None):
        with pytest.raises(ApprovalError) as exc:
            approval.reject(make_take(state="draft", shot=0, take=0), reason=bad_reason)
        assert "ready take" in str(exc.value)  # the state refusal, not the reason
    rejected = approval.reject(record, reason="caption wrong")
    assert rejected["rejectedReason"] == "caption wrong"
    assert validate_take(rejected) == rejected


def test_mutators_validate_what_they_return():
    """`approve`, `revoke` and `reject` return through `validate_take`, as
    `_new_take` does: a record with an extra key never comes back clean."""
    extra_ready = {**make_take(state="ready", shot=0, take=0), "surprise": 1}
    extra_approved = {**make_take(state="approved", shot=0, take=0), "surprise": 1}
    with pytest.raises(TakeError):
        approval.approve(extra_ready, by="Niobe", at=APPROVED_AT, source_revision=REVISION)
    with pytest.raises(TakeError):
        approval.revoke(extra_approved, by="Morpheus", at=REVOKED_AT, reason="camera drifted")
    with pytest.raises(TakeError):
        approval.reject(extra_ready, reason="caption wrong")


# --------------------------------------------------------------------------- /
# 4. Retake keeps the exact revision; recapture refuses it; neither carries approval


def test_retake_keeps_exact_revision():
    previous = make_take(state="ready", shot=2, take=1)
    new = retake(previous, take_index=2, job_id="job_next", attempt_id="att_next",
                 direction={"caption": "again", "voiceover": "v2", "camera": "semantic"},
                 created_at=CREATED_AT)
    assert new["state"] == "draft"
    assert new["shotIndex"] == 2
    assert new["takeIndex"] == 2
    assert new["sourceRevision"] == previous["sourceRevision"] == REVISION
    assert new["sourceArtifactId"] == previous["sourceArtifactId"]
    assert new["jobId"] == "job_next"
    assert new["attemptId"] == "att_next"
    assert new["direction"] == {"caption": "again", "voiceover": "v2", "camera": "semantic"}
    assert new["createdAt"] == CREATED_AT
    for field in ("approvedBy", "approvedAt", "rejectedReason",
                  "revokedBy", "revokedAt", "revokedReason"):
        assert new[field] is None
    assert validate_take(new) == new
    assert previous["state"] == "ready" and previous["takeIndex"] == 1  # untouched


def test_recapture_refuses_same_revision():
    previous = make_take(state="ready", shot=0, take=0)
    with pytest.raises(TakeError):
        recapture(previous, take_index=1, job_id="job_c", attempt_id="att_c",
                  source_revision=REVISION, source_artifact_id="art_new",
                  created_at=CREATED_AT)
    new = recapture(previous, take_index=1, job_id="job_c", attempt_id="att_c",
                    source_revision=OTHER_REVISION, source_artifact_id="art_new",
                    created_at=CREATED_AT)
    assert new["state"] == "draft"
    assert new["sourceRevision"] == OTHER_REVISION
    assert new["sourceRevision"] != previous["sourceRevision"]
    assert new["sourceArtifactId"] == "art_new"
    assert new["direction"] == previous["direction"]  # the intent carries over
    for field in ("approvedBy", "approvedAt", "rejectedReason",
                  "revokedBy", "revokedAt", "revokedReason"):
        assert new[field] is None
    # A mixed-case revision is not a 40-lowercase-hex revision: refused.
    with pytest.raises(TakeError):
        recapture(previous, take_index=1, job_id="job_c", attempt_id="att_c",
                  source_revision="F" * 40, source_artifact_id="art_new",
                  created_at=CREATED_AT)


# --------------------------------------------------------------------------- /
# 5. The export-blocked rule (§6), one test per code plus the clear case


def test_export_blocked_no_shots():
    blocks = export_blocked([], [], {}, set())
    assert codes_of(blocks) == ["no_shots"]
    assert blocks[0]["shotIndex"] is None
    assert blocks[0]["takeIndex"] is None
    perfect = [make_take(state="approved", shot=0, take=0)]
    assert codes_of(export_blocked([], perfect, {}, set())) == ["no_shots"]


def test_export_blocked_no_selected_take():
    first = make_take(state="ready", shot=0, take=0)
    second = make_take(state="ready", shot=1, take=0)
    blocks = export_blocked([0, 1], [first, second], {0: 0}, set())
    assert [(b["code"], b["shotIndex"], b["takeIndex"]) for b in blocks] == \
        [("no_selected_take", 1, None)]


def test_export_blocked_selected_take_missing():
    present = make_take(state="ready", shot=0, take=1)
    # Edge case 1: a selection naming a take not in the set is a block, not a KeyError.
    blocks = export_blocked([0], [present], {0: 0}, set())
    assert codes_of(blocks) == ["selected_take_missing"]
    assert blocks[0]["shotIndex"] == 0
    assert blocks[0]["takeIndex"] == 0


def test_export_blocked_selected_not_exportable():
    draft = make_take(state="draft", shot=0, take=0)
    codes = codes_of(export_blocked([0], [draft], {0: 0}, set()))
    assert "selected_not_exportable" in codes and "take_in_draft" in codes
    failed = make_take(state="failed", shot=0, take=0)
    codes = codes_of(export_blocked([0], [failed], {0: 0}, set()))
    assert "selected_not_exportable" in codes and "take_failed" in codes
    unresolved = make_take(state="unresolved", shot=0, take=0)
    codes = codes_of(export_blocked([0], [unresolved], {0: 0}, set()))
    assert "selected_not_exportable" in codes and "take_unresolved" in codes


def test_export_blocked_included_not_approved():
    # Edge case 2 (second half): an included ready take blocks.
    ready = make_take(state="ready", shot=0, take=0)
    blocks = export_blocked([0], [ready], {0: 0}, {(0, 0)})
    assert codes_of(blocks) == ["included_not_approved"]
    assert blocks[0]["shotIndex"] == 0
    assert blocks[0]["takeIndex"] == 0


def test_export_blocked_take_in_draft():
    selected = make_take(state="approved", shot=0, take=1)
    draft = make_take(state="draft", shot=0, take=0)
    blocks = export_blocked([0], [selected, draft], {0: 1}, set())
    assert codes_of(blocks) == ["take_in_draft"]
    assert blocks[0]["shotIndex"] == 0
    assert blocks[0]["takeIndex"] == 0


def test_export_blocked_take_failed():
    failed = make_take(state="failed", shot=0, take=0)
    codes = codes_of(export_blocked([0], [failed], {0: 0}, set()))
    assert set(codes) == {"selected_not_exportable", "take_failed"}
    assert len(codes) == 2  # every matching clause reports, not only the first


def test_export_blocked_take_unresolved():
    unresolved = make_take(state="unresolved", shot=0, take=0)
    codes = codes_of(export_blocked([0], [unresolved], {0: 0}, set()))
    assert set(codes) == {"selected_not_exportable", "take_unresolved"}


def test_export_blocked_clear():
    # Every shot selected, every selected take approved, every included take approved.
    first = make_take(state="approved", shot=0, take=0)
    second = make_take(state="approved", shot=1, take=0)
    assert export_blocked([0, 1], [first, second], {0: 0, 1: 0}, {(0, 0), (1, 0)}) == []
    # Edge case 2 (first half): a selected approved take need not be included.
    assert export_blocked([0, 1], [first, second], {0: 0, 1: 0}, set()) == []


# --------------------------------------------------------------------------- /
# 6. A failed take anywhere in the job tree blocks, selected or not


def test_failed_unselected_take_still_blocks():
    approved = make_take(state="approved", shot=0, take=0)
    failed = make_take(state="failed", shot=0, take=1)
    blocks = export_blocked([0], [approved, failed], {0: 0}, set())
    assert codes_of(blocks) == ["take_failed"]
    assert blocks[0]["shotIndex"] == 0
    assert blocks[0]["takeIndex"] == 1


# --------------------------------------------------------------------------- /
# 7. A rejected ready take: selected or included it blocks; otherwise not


def test_rejected_take_blocks_export():
    rejected = approval.reject(make_take(state="ready", shot=0, take=0), reason="caption wrong")
    other = make_take(state="approved", shot=0, take=1)
    take_set = [rejected, other]
    codes = codes_of(export_blocked([0], take_set, {0: 0}, set()))
    assert set(codes) == {"selected_not_exportable"}  # no new code
    codes = codes_of(export_blocked([0], take_set, {0: 1}, {(0, 0)}))
    assert set(codes) == {"included_not_approved"}  # no new code
    # Edge case 5: neither selected nor included — it does not block.
    assert export_blocked([0], take_set, {0: 1}, set()) == []
    fixed = approval.approve(rejected, by="Niobe", at=APPROVED_AT, source_revision=REVISION)
    assert export_blocked([0], [fixed, other], {0: 1}, {(0, 0)}) == []


# --------------------------------------------------------------------------- /
# 8. An ambiguous take set is a caller error, never a silent pick


def test_ambiguous_take_set_refused():
    """Two records sharing a `(shotIndex, takeIndex)` of two ints refuse.

    The pair does not identify a take (`jobId` + `attemptId` do), so picking
    one silently would let the other vanish from every verdict. A blocked
    state never raises; an ambiguous set does, naming the pair.
    """
    first = make_take(state="ready", shot=2, take=1)
    second = make_take(state="approved", shot=2, take=1, jobId="job_2", attemptId="att_b")
    assert first["jobId"] != second["jobId"]
    for take_set in ([first, second], [second, first]):  # either list order
        with pytest.raises(takes.AmbiguousTakeSet) as exc:
            export_blocked([2], take_set, {2: 1}, set())
        assert "(shotIndex=2, takeIndex=1)" in str(exc.value)  # it names the pair
        assert isinstance(exc.value, TakeError)  # an AmbiguousTakeSet is a TakeError
    assert issubclass(takes.AmbiguousTakeSet, TakeError)
    # Two identical records are just as ambiguous.
    with pytest.raises(takes.AmbiguousTakeSet):
        export_blocked([2], [first, copy.deepcopy(first)], {2: 1}, set())
    # Edge case 1: the set itself is wrong — it refuses even when the pair is
    # neither selected nor included.
    with pytest.raises(takes.AmbiguousTakeSet):
        export_blocked([9], [first, second], {}, set())


# --------------------------------------------------------------------------- /
# 9. A stray `included` entry blocks; it never raises


def test_export_blocked_stray_included_entries_block_and_never_raise():
    """An `included` entry that is not a two-item pair becomes an
    `included_not_approved` block with null indexes and a plain need —
    never IndexError, never TypeError."""
    approved = make_take(state="approved", shot=0, take=0)
    for stray in ([1], [], None, 3, [1, 2, 3]):
        blocks = export_blocked([0], [approved], {0: 0}, [stray])
        assert codes_of(blocks) == ["included_not_approved"], stray
        assert blocks[0]["shotIndex"] is None
        assert blocks[0]["takeIndex"] is None
        assert blocks[0]["needed"] == "each included entry to be a (shotIndex, takeIndex) pair"
    # A real pair still clears, and a real missing pair still blocks by name.
    assert export_blocked([0], [approved], {0: 0}, [(0, 0)]) == []
    blocks = export_blocked([0], [approved], {0: 0}, [(0, 1)])
    assert [(b["code"], b["shotIndex"], b["takeIndex"]) for b in blocks] == \
        [("included_not_approved", 0, 1)]


# --------------------------------------------------------------------------- /
# 10. Persistence: canonical text both ways, keyed by jobId/shotIndex/takeIndex


def test_persistence_round_trip_and_key():
    record = make_take(state="approved", shot=3, take=2)
    text = persistence.dumps(record)
    assert json.loads(text) == record
    assert text == json.dumps(record, sort_keys=True, separators=(",", ":"))
    assert persistence.loads(text) == record
    assert persistence.key(record) == ("job_1", 3, 2)
    extra = {**record, "surprise": 1}
    with pytest.raises(TakeError):
        persistence.dumps(extra)
    with pytest.raises(TakeError):
        persistence.loads(json.dumps(extra))
    missing = {k: v for k, v in record.items() if k != "attemptId"}
    with pytest.raises(TakeError):
        persistence.dumps(missing)
    with pytest.raises(TakeError):
        persistence.loads("not json")
    with pytest.raises(TakeError):
        persistence.loads("[1, 2]")
    # Edge case 4: a valid record round-trips byte for byte.
    approved = make_take(state="approved", shot=3, take=2)
    assert persistence.loads(persistence.dumps(approved)) == approved
    assert persistence.dumps(persistence.loads(persistence.dumps(approved))) == \
        persistence.dumps(approved)


def test_persistence_refuses_unserialisable_records():
    """`key` validates first; `dumps` is strict; `loads` refuses NaN/Infinity."""
    base = make_take(state="ready", shot=0, take=0)
    with pytest.raises(TakeError):  # a bad record is TakeError, never KeyError
        persistence.key({k: v for k, v in base.items() if k != "jobId"})
    with pytest.raises(TakeError):
        persistence.key({**base, "surprise": 1})
    with pytest.raises(TakeError):
        persistence.key(["not", "a", "dict"])
    with pytest.raises(TakeError):  # a set has no canonical JSON text
        persistence.dumps({**base, "direction": {"camera": {"semantic"}}})
    with pytest.raises(TakeError):  # NaN is refused on the way out (allow_nan=False)
        persistence.dumps({**base, "durationMs": float("nan")})
    with pytest.raises(TakeError):  # and NaN / Infinity are refused on the way in
        persistence.loads(persistence.dumps(base).replace(
            '"durationMs":4820', '"durationMs":NaN'))
    with pytest.raises(TakeError):
        persistence.loads(persistence.dumps(base).replace(
            '"durationMs":4820', '"durationMs":Infinity'))


# --------------------------------------------------------------------------- /
# 11. The artifact registry: one id, one revision, forever


def test_artifact_registry_conflict():
    registry = artifacts.register({}, artifact_id="art_1", revision="rev-1", kind="image/png")
    assert registry == {"art_1": {"revision": "rev-1", "kind": "image/png"}}
    again = artifacts.register(registry, artifact_id="art_1", revision="rev-1", kind="image/png")
    assert again == registry
    with pytest.raises(ArtifactError):
        artifacts.register(registry, artifact_id="art_1", revision="rev-2", kind="image/png")
    assert registry == {"art_1": {"revision": "rev-1", "kind": "image/png"}}
    grown = artifacts.register(registry, artifact_id="art_2", revision="rev-2", kind="video/mp4")
    assert set(grown) == {"art_1", "art_2"}


def test_registry_result_shares_nothing_and_refuses_corrupt_entries():
    """A returned registry shares no per-artifact dict with the input, on
    either path; a known id mapped to `None` or a non-mapping is refused."""
    registry = artifacts.register({}, artifact_id="art_1", revision="rev-1", kind="image/png")
    # The new-id path: changing the result must not touch the input.
    grown = artifacts.register(registry, artifact_id="art_2", revision="rev-2", kind="video/mp4")
    grown["art_2"]["kind"] = "text/plain"
    grown["art_1"]["kind"] = "text/plain"
    assert registry["art_1"] == {"revision": "rev-1", "kind": "image/png"}
    # The same-revision path copies too.
    again = artifacts.register(grown, artifact_id="art_2", revision="rev-2", kind="video/mp4")
    again["art_1"]["kind"] = "application/pdf"
    again["art_2"]["revision"] = "rev-9"
    assert grown["art_1"] == {"revision": "rev-1", "kind": "text/plain"}
    assert grown["art_2"] == {"revision": "rev-2", "kind": "text/plain"}
    # A corrupt known entry is an ArtifactError, never an AttributeError and
    # never a silent re-registration.
    for corrupt in (None, "rev-1", ["rev-1"], 7):
        with pytest.raises(ArtifactError):
            artifacts.register({"art_x": corrupt}, artifact_id="art_x",
                               revision="rev-1", kind="image/png")


# --------------------------------------------------------------------------- /
# 12. Validate refusals (§3 shape), including the revision edge cases


def test_validate_refuses_bad_records():
    base = make_take(state="ready")
    assert validate_take(base) == base
    bad_records = [
        {**base, "kind": "capturd.shot"},
        {**base, "shotIndex": -1},
        {**base, "takeIndex": -3},
        {**base, "shotIndex": True},  # a bool is not an index
        {**base, "state": "waiting"},
        {**base, "state": "awaiting_approval"},
        {**base, "sourceRevision": "A" * 40},  # mixed case refused
        {**base, "sourceRevision": "a" * 39},
        {**base, "sourceRevision": "a" * 41},
        {**base, "sourceRevision": "g" * 40},
        {k: v for k, v in base.items() if k != "attemptId"},
        {**base, "surprise": 1},
        {**base, "state": "approved"},  # approved without who and when
        {**base, "state": "approved", "approvedBy": "Niobe", "approvedAt": None},
        {**base, "state": "approved", "approvedBy": "", "approvedAt": APPROVED_AT},
    ]
    for record in bad_records:
        with pytest.raises(TakeError):
            validate_take(record)
    with pytest.raises(TakeError):
        validate_take(["not", "a", "dict"])


def test_an_approved_take_carries_no_revocation():
    """A1: `approved` with any `revoked*` field set is a TakeError — approval
    clears revocations, so both at once is a contradiction. The wider rule
    "revoked implies ready" is deliberately NOT written: a revoked `ready`
    take may legally move to `failed` or `unresolved` by transition, keeping
    its history (§2)."""
    approved = approval.approve(make_take(state="ready", shot=0, take=0), by="Niobe",
                                at=APPROVED_AT, source_revision=REVISION)
    for field in ("revokedBy", "revokedAt", "revokedReason"):
        with pytest.raises(TakeError):
            validate_take({**approved, field: "history"})
    # The legal move the wider rule would break: revoke, then transition on.
    revoked = approval.revoke(approved, by="Morpheus", at=REVOKED_AT, reason="camera drifted")
    moved = transition(revoked, "failed")
    assert moved["state"] == "failed"
    assert moved["revokedBy"] == "Morpheus"  # the history is kept
    assert validate_take(moved) == moved


# --------------------------------------------------------------------------- /
# 13. The five modules stay pure: allowlisted imports, no I/O, no clocks


def test_modules_stay_pure():
    for name in CONTENT_MODULES:
        source = (ENGINE_DIR / name).read_text(encoding="utf-8")
        assert "waiting" not in source, f"{name}: mentions a waiting state"
        assert "awaiting_approval" not in source, f"{name}: mentions a waiting state"
        tree = ast.parse(source, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in _BANNED_IMPORTS, f"{name}: banned import {alias.name}"
                    assert alias.name in _ALLOWED_IMPORTS, \
                        f"{name}: import outside allowlist: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.level == 1, \
                        f"{name}: relative import escapes capturd.engine (level {node.level})"
                else:
                    root = (node.module or "").split(".")[0]
                    assert root not in _BANNED_IMPORTS, \
                        f"{name}: banned import from {node.module}"
                    assert node.module in _ALLOWED_IMPORTS, \
                        f"{name}: import outside allowlist: {node.module}"
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "open":
                    raise AssertionError(f"{name}: an open() call")
                if isinstance(func, ast.Attribute) and func.attr in ("open", "now"):
                    raise AssertionError(f"{name}: a .{func.attr}() call")
