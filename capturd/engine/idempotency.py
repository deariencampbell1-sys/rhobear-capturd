"""One operation, one result: the local `operationId` ledger (GAPS-NOW M3).

`run(operation_id, payload, work)` does `work()` at most once per id. The same
id with the same payload returns the first result (across threads and across
a restart); the same id with a different payload is `IdempotencyMismatch`. A
`work()` that raises is not remembered, so the caller may retry the same id.
The payload is compared by the SHA-256 of its canonical JSON. The hosted
`(accountId, engine, idempotencyKey)` index is L8d's; this is the stdio
adapter's.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable


class IdempotencyMismatch(ValueError):
    """The operation id was already used with a different payload."""

    def __init__(self, operation_id: str):
        super().__init__(f"operation {operation_id!r} was already used with a different payload")
        self.operation_id = operation_id

    def payload(self) -> dict[str, Any]:
        return {"code": "idempotency_mismatch", "operationId": self.operation_id,
                "detail": str(self)}


def _digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class OperationLedger:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, entries: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    def run(self, operation_id: str, payload: Any, work: Callable[[], Any]) -> Any:
        digest = _digest(payload)
        with self._lock:
            key_lock = self._key_locks.setdefault(operation_id, threading.Lock())
        with key_lock:
            with self._lock:
                entry = self._load().get(operation_id)
            if entry is not None:
                if entry["payload"] != digest:
                    raise IdempotencyMismatch(operation_id)
                return entry["result"]
            # Round-trip so the first answer equals every replay of it.
            result = json.loads(json.dumps(work(), sort_keys=True, default=str))
            with self._lock:
                entries = self._load()
                entries[operation_id] = {"payload": digest, "result": result}
                self._save(entries)
            return result
