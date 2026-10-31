"""Canonical serialisation of §3 take records — text in, text out.

The caller supplies and stores the text; nothing here touches a disk or a
path. `key(record)` is the `(jobId, shotIndex, takeIndex)` a caller keys its
own storage by — no second store lives here. Both directions validate the
record first (`takes.validate_take`: the closed §3 key set plus the checked
fields), so an unknown key cannot slip through either way, and the text
itself is strict: `NaN` and `Infinity` have no canonical take-record form.
"""

from __future__ import annotations

import json
from typing import Any

from .takes import TakeError, validate_take


def _refused_constant(name: str) -> None:
    raise ValueError(f"{name} is not a number a take record may carry")


def key(record: dict[str, Any]) -> tuple[str, int, int]:
    """Validate first, then return the record's storage key.

    A record that is not the §3 shape is TakeError, never a KeyError.
    """
    validate_take(record)
    return (record["jobId"], record["shotIndex"], record["takeIndex"])


def dumps(record: dict[str, Any]) -> str:
    """Validate the record, then serialise it canonically: sorted keys.

    Strict: `allow_nan=False` — a `NaN` or `Infinity` has no canonical
    take-record text — and anything JSON cannot serialise (a set, for one)
    is TakeError, not a half-canonical string.
    """
    validate_take(record)
    try:
        return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TakeError(f"take record has no canonical JSON text: {exc}") from exc


def loads(text: str) -> dict[str, Any]:
    """Parse and validate; an unknown or missing key, or a `NaN`/`Infinity`
    in the text, is refused."""
    try:
        record = json.loads(text, parse_constant=_refused_constant)
    except ValueError as exc:
        raise TakeError(f"take record text is not JSON: {exc}") from exc
    return validate_take(record)
