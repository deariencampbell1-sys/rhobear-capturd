"""Approval policy (contract §5): named humans only, bound to one revision.

Pure record in, new record out. `by`, `at` and `reason` arrive as arguments
and are never resolved here — identity is 06d's job. A rejection is never a
failure: one `failed` take anywhere blocks the whole job, so `reject` leaves
the state `ready` (Niobe, E-1031-043454-b6e7).
"""

from __future__ import annotations

from typing import Any

from .takes import validate_take


class ApprovalError(ValueError):
    """The approval, revocation or rejection is not allowed on this record."""


def _blank(*values: Any) -> bool:
    return any(not isinstance(value, str) or not value.strip() for value in values)


def _checked(record: Any) -> dict[str, Any]:
    """Validate the record before a mutator reads it.

    A record that is not a dict is the module's own `ApprovalError`, never an
    `AttributeError` from `record.get`. A malformed dict is refused by
    `validate_take` (`TakeError`) — the same refusal the returned records
    already go through, so a mutator never escapes as a builtin.
    """
    if not isinstance(record, dict):
        raise ApprovalError(
            f"a take record is a dict, not {type(record).__name__}")
    return validate_take(record)


def approve(record: dict[str, Any], *, by: str, at: str, source_revision: str) -> dict[str, Any]:
    """Approve a `ready` take: bind the named human, the time, the revision.

    Refused unless the state is `ready`, `by` and `at` are non-blank, and
    `source_revision` equals the record's (§5.2, L8d §14.3). Clears the
    `revoked*` fields and `rejectedReason`. The returned record passes
    `validate_take`. Never mutates.
    """
    record = _checked(record)
    if record.get("state") != "ready":
        raise ApprovalError(
            f"only a ready take can be approved, not {record.get('state')!r}")
    if _blank(by):
        raise ApprovalError("an approval names its human; by is blank")
    if _blank(at):
        raise ApprovalError("an approval carries its time; at is blank")
    if source_revision != record.get("sourceRevision"):
        raise ApprovalError(
            f"approval binds {record.get('sourceRevision')!r}, not {source_revision!r}")
    return validate_take({
        **record,
        "state": "approved",
        "approvedBy": by,
        "approvedAt": at,
        "rejectedReason": None,
        "revokedBy": None,
        "revokedAt": None,
        "revokedReason": None,
    })


def revoke(record: dict[str, Any], *, by: str, at: str, reason: str) -> dict[str, Any]:
    """Revoke an `approved` take: back to `ready`, the revocation recorded.

    All of `by`, `at` and `reason` must be non-blank. `approvedBy` and
    `approvedAt` stay as history (§5.4); a derived export is L9's concern.
    The returned record passes `validate_take`.
    """
    record = _checked(record)
    if record.get("state") != "approved":
        raise ApprovalError(
            f"only an approved take can be revoked, not {record.get('state')!r}")
    if _blank(by, at, reason):
        raise ApprovalError("a revocation records who, when and why; all three are required")
    return validate_take({
        **record,
        "state": "ready",
        "revokedBy": by,
        "revokedAt": at,
        "revokedReason": reason,
    })


def reject(record: dict[str, Any], *, reason: str) -> dict[str, Any]:
    """Reject a `ready` take: record why, leave the state `ready`.

    `reason` must be non-blank: an empty `rejectedReason` is not null, so it
    would block the export forever (§6). The state does not move — a
    rejected take is not good enough yet, while a `failed` take anywhere
    would block the whole job. The returned record passes `validate_take`.
    Never mutates.
    """
    record = _checked(record)
    if record.get("state") != "ready":
        raise ApprovalError(
            f"only a ready take can be rejected, not {record.get('state')!r}")
    if _blank(reason):
        raise ApprovalError("a rejection records why; reason is blank")
    return validate_take({**record, "rejectedReason": reason})
