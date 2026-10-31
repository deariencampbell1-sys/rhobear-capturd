"""Shot/take content commands and the export-blocked rule (§4, §6).

`retake`/`recapture` mint new §3 take records (the L8d command layer — job,
receipt, `expectedRevision` — is elsewhere; ruling D: the `conflict` +
`revision_conflict` pass-through is the command layer's, unproven today).
`export_blocked` takes the selection and the complete take set as arguments
(rulings B and C): no job store is read, no state is kept.
"""

from __future__ import annotations

from .takes import AmbiguousTakeSet, TakeError, validate_take

# §6: the code and what-is-needed per blocking take state.
_BLOCKS = {
    "draft": ("take_in_draft", "the draft take to reach ready"),
    "failed": ("take_failed", "the failed take replaced by a new attempt"),
    "unresolved": ("take_unresolved", "the unresolved take reconciled"),
}


def retake(previous, *, take_index, job_id, attempt_id, direction, created_at):
    """Mint a new `draft` take on the same shot with the exact `sourceRevision`.

    Direction only: the pixels are the previous take's, so the revision and
    the source artifact carry over unchanged (§4). Identity and time come
    from the caller — the new child job; every approval field starts null.
    A retake is a new take: `take_index` equal to the previous record's is
    refused, so the pair `(shotIndex, takeIndex)` is never minted twice.
    """
    validate_take(previous)
    if take_index == previous["takeIndex"]:
        raise TakeError(
            f"retake is a new take; takeIndex {take_index!r} is the previous "
            "take's")
    return _new_take(
        previous, take_index=take_index, job_id=job_id, attempt_id=attempt_id,
        direction=direction, created_at=created_at,
        source_revision=previous["sourceRevision"],
        source_artifact_id=previous["sourceArtifactId"])


def recapture(previous, *, take_index, job_id, attempt_id, source_revision,
              source_artifact_id, created_at):
    """Mint a new `draft` take from a newly recorded source.

    Refused when `source_revision` equals the previous take's: recapture is
    re-recording the pixels, and the same revision would be a retake (§4).
    The old revision stays immutable; the previous direction carries over.
    A recapture is a new take: `take_index` equal to the previous record's
    is refused, so the pair `(shotIndex, takeIndex)` is never minted twice.
    """
    validate_take(previous)
    if take_index == previous["takeIndex"]:
        raise TakeError(
            f"recapture is a new take; takeIndex {take_index!r} is the "
            "previous take's")
    if source_revision == previous["sourceRevision"]:
        raise TakeError("recapture must record new pixels; use retake to keep "
                        + repr(previous["sourceRevision"]))
    return _new_take(
        previous, take_index=take_index, job_id=job_id, attempt_id=attempt_id,
        direction=previous["direction"], created_at=created_at,
        source_revision=source_revision, source_artifact_id=source_artifact_id)


def _new_take(previous, *, take_index, job_id, attempt_id, direction,
              created_at, source_revision, source_artifact_id):
    """The §3 record for a new take; every approval field starts null."""
    record = {
        "shotIndex": previous["shotIndex"],
        "takeIndex": take_index,
        "state": "draft",
        "jobId": job_id,
        "attemptId": attempt_id,
        "sourceRevision": source_revision,
        "sourceArtifactId": source_artifact_id,
        "kind": "capturd.take",
        "direction": dict(direction) if isinstance(direction, dict) else direction,
        "durationMs": None,  # measured when the take completes
        "createdAt": created_at,
        "approvedBy": None,
        "approvedAt": None,
        "rejectedReason": None,
        "revokedBy": None,
        "revokedAt": None,
        "revokedReason": None,
    }
    return validate_take(record)


def _is_index(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _ambiguous_pair(take_list):
    """The first duplicated `(shotIndex, takeIndex)` of two ints, or None."""
    seen = set()
    for record in take_list:
        shot, take = record.get("shotIndex"), record.get("takeIndex")
        if _is_index(shot) and _is_index(take):
            pair = (shot, take)
            if pair in seen:
                return pair
            seen.add(pair)
    return None


def _included_pair(entry):
    """The `(shotIndex, takeIndex)` of a two-item int-sequence entry, else None."""
    if isinstance(entry, (tuple, list)) and len(entry) == 2 \
            and _is_index(entry[0]) and _is_index(entry[1]):
        return (entry[0], entry[1])
    return None


def _take_order(record):
    shot, take = record.get("shotIndex"), record.get("takeIndex")
    return (0, shot, take) if _is_index(shot) and _is_index(take) else (1, 0, 0)


def _pair_order(pair):
    """The sort key for a real included pair: always two non-negative ints
    (only `_included_pair`'s two-int result is ever appended)."""
    return pair


def _block(code, shot_index, take_index, needed):
    return {"code": code, "shotIndex": shot_index, "takeIndex": take_index, "needed": needed}


def export_blocked(shot_indexes, takes, selection, included):
    """Return every reason the export is blocked (§6); empty means clear.

    `shot_indexes` are the demo's shots; `takes` the complete take set for
    the job tree (ruling C); `selection` maps shot index to take index
    (ruling B); `included` the `(shotIndex, takeIndex)` pairs in the package.
    Every matching clause reports, not only the first; each block names the
    shot, the take and what is needed. A blocked state never raises.

    An invalid or ambiguous *set* is a caller error and does: every record
    is validated by `validate_take` before the list is sorted or indexed —
    so a non-dict entry is `TakeError`, never `AttributeError`, and a null
    `approvedBy` on an `approved` take is refused, never trusted to clear
    the gate — then two valid records sharing a `(shotIndex, takeIndex)` of
    two ints raise `AmbiguousTakeSet`; a malformed record is reported as
    malformed, not as an ambiguity. It is never silently picked from. An
    `included` entry that is not a two-item pair becomes an
    `included_not_approved` block with null indexes.
    """
    shot_list = list(shot_indexes)
    validated = [validate_take(record) for record in takes]
    take_list = sorted(validated, key=_take_order)
    ambiguous = _ambiguous_pair(take_list)
    if ambiguous is not None:
        raise AmbiguousTakeSet(
            f"two take records share (shotIndex={ambiguous[0]}, "
            f"takeIndex={ambiguous[1]}); a take set holds one record per pair")
    chosen = dict(selection)
    included_pairs, stray_included = [], []
    for entry in included:
        pair = _included_pair(entry)
        if pair is None:
            stray_included.append(entry)
        else:
            included_pairs.append(pair)
    included_pairs = sorted(set(included_pairs), key=_pair_order)
    by_pair = {(t.get("shotIndex"), t.get("takeIndex")): t for t in take_list}
    blocks = []
    if not shot_list:
        blocks.append(_block("no_shots", None, None, "at least one shot"))
    for shot in shot_list:
        take_index = chosen.get(shot)
        if take_index is None:
            blocks.append(_block("no_selected_take", shot, None, "a selected take for the shot"))
            continue
        record = by_pair.get((shot, take_index))
        if record is None:
            blocks.append(_block("selected_take_missing", shot, take_index,
                                 "the selected take to exist in the take set"))
            continue
        needs = []
        if record.get("state") not in ("ready", "approved"):
            needs.append("selected take to be ready or approved")
        if record.get("rejectedReason") is not None:
            needs.append("the rejectedReason on the selected take cleared")
        if needs:
            blocks.append(_block("selected_not_exportable", shot, take_index, "; ".join(needs)))
    for _entry in stray_included:
        blocks.append(_block("included_not_approved", None, None,
                             "each included entry to be a (shotIndex, takeIndex) pair"))
    for pair in included_pairs:
        record = by_pair.get(pair)
        if record is None:
            blocks.append(_block("included_not_approved", pair[0], pair[1],
                                 "the included take to exist and be approved"))
            continue
        needs = []
        if record.get("state") != "approved":
            needs.append("the included take approved")
        if record.get("rejectedReason") is not None:
            needs.append("the rejectedReason on the included take cleared")
        if needs:
            blocks.append(_block("included_not_approved", pair[0], pair[1], "; ".join(needs)))
    for record in take_list:
        code, needed = _BLOCKS.get(record.get("state"), (None, None))
        if code:
            blocks.append(_block(code, record.get("shotIndex"), record.get("takeIndex"), needed))
    return blocks
