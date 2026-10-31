"""The frozen `UsageReceipt` boundary and the closed, one-way state path:
`cloud/src/mcp/engine/schemas/usage-receipt.schema.json` at `rhobear-builds`.

Money is a string never a float, unit counts are non-negative integers,
unknown fields are preserved, and only `reserved -> settled`, `reserved ->
released` and `settled -> reversed` move; `unresolved` is reconciliation's
alone. A timestamp fraction is pinned to the widths the minimum supported
runtime parses — exactly three or six digits — so acceptance is the same on
every interpreter.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

REQUIRED_FIELDS = ("operationId", "attemptId", "accountId", "product", "priceVersion",
                   "units", "customerDebitUsd", "state", "occurredAt")
STATES = ("reserved", "settled", "released", "unresolved", "reversed")
COVERAGE_KIND = "launch_mcp_unmetered"
# The frozen JS `^.{1,512}$`: its `.` excludes all four line terminators, while
# Python's excludes only `\n`, so they are named here.
_IDENTITY = re.compile("[^\n\r\u2028\u2029]{1,512}")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$")
_MONEY = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_MOVES = {("reserved", "settle"): "settled", ("reserved", "release"): "released",
          ("settled", "reverse"): "reversed"}


class ReceiptError(ValueError):
    """Not the frozen `UsageReceipt` shape, or not a move on the one-way path."""


def is_money_string(value: Any) -> bool:
    """A non-negative decimal string; `"0.00"` is a real charge, `-1` is not."""
    return isinstance(value, str) and _MONEY.fullmatch(value) is not None


def is_identity(value: Any) -> bool:
    """The one identity rule: a 1-512 character string, never empty.

    The window is the pinned `^.{1,512}$` (matched whole, no newline mode), so
    an identity containing any line terminator stays outside it. Do not widen this to
    `[\\s\\S]` or add DOTALL; the rule is frozen.
    """
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


def _instant(value: Any, field: str) -> datetime.datetime:
    """The schema's ISO-8601 timestamp with an offset, parsed as a real instant.

    Any fraction the frozen schema admits is accepted; it is normalised to six
    digits before parsing, so a timestamp is judged the same way on every
    interpreter (`requires-python >= 3.10`).
    """
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ReceiptError(f"{field} is not an ISO-8601 timestamp with an offset")
    try:
        # `fromisoformat` before 3.11 takes neither `Z` nor every fraction width,
        # so the fraction is normalised to six digits and `Z` is spelled out.
        # Past six digits it truncates: the validated value itself is unchanged.
        normal = re.sub(r"\.([0-9]+)",
                        lambda m: f".{m.group(1).ljust(6, chr(48))[:6]}", value, count=1)
        return datetime.datetime.fromisoformat(normal.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError(f"{field} is not a real instant: {value!r}") from exc


def validate_coverage(coverage: Any) -> dict[str, Any]:
    """The launch-coverage block exactly as the frozen schema carries it."""
    if not isinstance(coverage, dict):
        raise ReceiptError("coverage must be an object")
    for field in ("kind", "startedAt", "endsAt"):
        if field not in coverage:
            raise ReceiptError(f"coverage is missing required field {field!r}")
    if coverage["kind"] != COVERAGE_KIND:
        raise ReceiptError(f"coverage kind must be {COVERAGE_KIND!r}")
    started = _instant(coverage["startedAt"], "coverage startedAt")
    if _instant(coverage["endsAt"], "coverage endsAt") < started:
        raise ReceiptError("coverage endsAt precedes startedAt")
    seat = coverage.get("seat")
    if "seat" in coverage and (isinstance(seat, bool) or not isinstance(seat, int)):
        raise ReceiptError(f"coverage seat must be an integer, not {seat!r}")
    return coverage


def validate_receipt(record: Any) -> dict[str, Any]:
    """Return `record` when it is a valid `UsageReceipt`, else ReceiptError.

    One deliberate rule of this boundary is stricter than the frozen schema,
    which says only `"type": "integer"` for `units` values: here they must be
    non-negative integers, because a negative unit count is not a charge.
    """
    if not isinstance(record, dict):
        raise ReceiptError(f"a receipt is an object, not {type(record).__name__}")
    for field in REQUIRED_FIELDS:
        if field not in record:
            raise ReceiptError(f"receipt is missing required field {field!r}")
    for field in ("operationId", "attemptId", "accountId", "product", "priceVersion"):
        value = record[field]
        if not is_identity(value):
            raise ReceiptError(f"receipt {field} must be a 1-512 character string")
    units = record["units"]
    if not isinstance(units, dict):
        raise ReceiptError(f"units must be an object, not {type(units).__name__}")
    for name, value in units.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReceiptError(f"units[{name!r}] must be a non-negative integer, not {value!r}")
    debit = record["customerDebitUsd"]
    if debit is not None and not is_money_string(debit):
        raise ReceiptError(f"customerDebitUsd must be a money string or null, not {debit!r}")
    if record["state"] not in STATES:
        raise ReceiptError(f"receipt state {record['state']!r} is not one of {STATES}")
    _instant(record["occurredAt"], "occurredAt")
    if "coverage" in record:
        validate_coverage(record["coverage"])
    return record


def is_metered(record: Any) -> bool:
    """True when the receipt carries real money; False only under launch coverage."""
    return "coverage" not in validate_receipt(record)


def next_state(current: str | None, event: str) -> str:
    """The one legal move for `current` on `event`, else ReceiptError.

    Exactly three pairs move: `reserved -> settled`, `reserved -> released`
    and `settled -> reversed`. The first state is created by the caller, not
    by a `reserve` event; every pair involving `None` raises. `unresolved` is
    reconciliation's alone and is never produced here.
    """
    if event not in ("reserve", "settle", "release", "reverse"):
        raise ReceiptError(f"unknown receipt event {event!r}")
    if current is not None and current not in STATES:
        raise ReceiptError(f"unknown receipt state {current!r}")
    move = _MOVES.get((current, event))
    if move is None:
        raise ReceiptError(f"a receipt cannot move from {current!r} on {event!r}")
    return move
