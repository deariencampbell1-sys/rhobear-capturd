"""Canonical serialisation of §3 take records — text in, text out.

The caller supplies and stores the text; nothing here touches a disk or a
path. `key(record)` is the `(jobId, shotIndex, takeIndex)` a caller keys its
own storage by — no second store lives here. Both directions refuse any
record that is not exactly the §3 shape (`takes.validate_take`), so an
unknown key cannot slip through either way.
"""

from __future__ import annotations

import json
from typing import Any

from .takes import TakeError, validate_take


def key(record: dict[str, Any]) -> tuple[str, int, int]:
    """Return the record's `(jobId, shotIndex, takeIndex)` storage key."""
    return (record["jobId"], record["shotIndex"], record["takeIndex"])


def dumps(record: dict[str, Any]) -> str:
    """Validate the record, then serialise it canonically: sorted keys."""
    validate_take(record)
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def loads(text: str) -> dict[str, Any]:
    """Parse and validate; an unknown or missing key is refused."""
    try:
        record = json.loads(text)
    except ValueError as exc:
        raise TakeError(f"take record text is not JSON: {exc}") from exc
    return validate_take(record)
