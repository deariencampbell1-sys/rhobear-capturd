"""Take records and the §2 state machine (W3-06c-r1, contract §2/§3).

Pure content: dict in, new dict out; the caller supplies identity and time.

Ruling D (E-1031-043454-b6e7): the §4 revision-conflict pass-through —
`conflict` with `details.reason: 'revision_conflict'` — is owned by the L8d
command layer and implemented nowhere in the control plane today. It is not
modelled here and has no `expectedRevision` test in this unit.
"""

from __future__ import annotations

from typing import Any

#: The closed set of §2 states — nothing else, ever: progress flags belong to
#: the L8d job, never to a take state.
STATES = ("draft", "ready", "approved", "failed", "unresolved")

#: The §3 take record shape — closed: no key missing, no key extra.
RECORD_KEYS = frozenset({
    "shotIndex", "takeIndex", "state", "jobId", "attemptId", "sourceRevision",
    "sourceArtifactId", "kind", "direction", "durationMs", "createdAt",
    "approvedBy", "approvedAt", "rejectedReason", "revokedBy", "revokedAt",
    "revokedReason",
})

KIND = "capturd.take"

#: §2 moves. `approved` never appears: entering or leaving it is approval.py's
#: alone. `failed` is terminal — a new attempt is a new take. Unresolved moves
#: back to `ready` or `failed` are the §2 reconcile path.
TRANSITIONS = {
    ("draft", "ready"), ("draft", "failed"), ("draft", "unresolved"),
    ("ready", "failed"), ("ready", "unresolved"),
    ("unresolved", "ready"), ("unresolved", "failed"),
}


class TakeError(ValueError):
    """A record is not the §3 shape, or the move is not in the §2 table."""


def _is_index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_revision(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 40
            and all(c in "0123456789abcdef" for c in value))


def validate_take(record: Any) -> dict[str, Any]:
    """Return `record` if it is exactly the §3 shape, else raise TakeError.

    Closed key set, `kind`, non-negative int indexes, a state in STATES,
    `sourceRevision` as 40 lowercase hex; `approved` also carries a named
    `approvedBy` and a real `approvedAt` (§5.3). Never mutates.
    """
    if not isinstance(record, dict):
        raise TakeError(f"a take record is a dict, not {type(record).__name__}")
    keys = set(record)
    missing = RECORD_KEYS - keys
    unknown = keys - RECORD_KEYS
    if missing:
        raise TakeError(f"take record is missing §3 keys: {sorted(missing)}")
    if unknown:
        raise TakeError(f"take record carries unknown keys: {sorted(unknown)}")
    if record["kind"] != KIND:
        raise TakeError(f"take record kind must be {KIND!r}, not {record['kind']!r}")
    for field in ("shotIndex", "takeIndex"):
        if not _is_index(record[field]):
            raise TakeError(
                f"take record {field} must be a non-negative int, not {record[field]!r}")
    if record["state"] not in STATES:
        raise TakeError(
            f"take record state {record['state']!r} is not one of {STATES} "
            "(and never a job progress flag)")
    if not _is_revision(record["sourceRevision"]):
        raise TakeError("take record sourceRevision must be 40 lowercase hex")
    if record["state"] == "approved":
        for field in ("approvedBy", "approvedAt"):
            value = record[field]
            if not isinstance(value, str) or not value.strip():
                raise TakeError(f"an approved take carries a named {field}")
    return record


def transition(record: dict[str, Any], to: str) -> dict[str, Any]:
    """Return a new record with the state moved to `to`, per §2 only.

    Refuses everything outside TRANSITIONS: staying put, entering or leaving
    `approved` (approval.py's alone), anything out of terminal `failed`.
    """
    validate_take(record)
    if to not in STATES:
        raise TakeError(f"cannot move a take to the unknown state {to!r}")
    if (record["state"], to) not in TRANSITIONS:
        raise TakeError(f"a take cannot move from {record['state']!r} to {to!r}")
    return {**record, "state": to}
