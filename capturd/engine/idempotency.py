"""Durable local operation claims; uncertain side effects are never retried."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


class IdempotencyMismatch(ValueError):
    def __init__(self, operation_id: str):
        super().__init__(f"operation {operation_id!r} was already used with a different payload")
        self.operation_id = operation_id

    def payload(self) -> dict[str, Any]:
        return {"code": "idempotency_mismatch", "operationId": self.operation_id,
                "detail": str(self)}


class OperationUnknown(RuntimeError):
    """Work may have happened; a caller must reconcile rather than retry."""

    def __init__(self, operation_id: str):
        super().__init__(f"operation {operation_id!r} outcome is unknown; reconciliation required")
        self.operation_id = operation_id

    def payload(self) -> dict[str, Any]:
        return {"code": "outcome_unknown", "operationId": self.operation_id,
                "detail": str(self)}


def _digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class OperationLedger:
    def __init__(self, path: Path):
        self._path = Path(path)

    @contextmanager
    def _guard(self, suffix: str):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(f".{self._path.name}.{suffix}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("operation lock must be a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, entries: dict[str, Any]) -> None:
        tmp = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("x", encoding="utf-8") as stream:
                json.dump(entries, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            tmp.unlink(missing_ok=True)

    def _receipt_path(self, key: str) -> Path:
        return self._path.with_name(f".{self._path.name}.{key}.result")

    def _write_receipt(self, key: str, digest: str, result: Any) -> None:
        path = self._receipt_path(key)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'payload': digest, 'result': result}, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _read_receipt(self, key: str, digest: str) -> Any:
        path = self._receipt_path(key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None
                data = json.load(stream)
            if not isinstance(data, dict) or data.get('payload') != digest or 'result' not in data:
                return None
            return data
        except (FileNotFoundError, OSError, ValueError, UnicodeError):
            return None

    def run(self, operation_id: str, payload: Any, work: Callable[[], Any]) -> Any:
        digest = _digest(payload)
        key = hashlib.sha256(operation_id.encode('utf-8')).hexdigest()
        # Held across work: another live process waits for the durable result.
        # An orphan claim after the lock is released is an uncertain outcome.
        with self._guard(key):
            with self._guard('ledger'):
                entries = self._load()
                entry = entries.get(operation_id)
                if entry is not None:
                    if entry['payload'] != digest:
                        raise IdempotencyMismatch(operation_id)
                    if 'result' in entry:
                        return entry['result']
                    receipt = self._read_receipt(key, digest)
                    if receipt is None:
                        raise OperationUnknown(operation_id)
                    entries[operation_id] = {'payload': digest, 'state': 'complete', 'result': receipt['result']}
                    self._save(entries)
                    self._receipt_path(key).unlink(missing_ok=True)
                    return receipt['result']
                entries[operation_id] = {'payload': digest, 'state': 'claimed'}
                self._save(entries)
            result = json.loads(json.dumps(work(), sort_keys=True, default=str))
            # The durable receipt bridges a crash while the ledger's complete
            # transition is being committed. Before a result exists, the claim
            # remains unknown and is never speculatively repeated.
            self._write_receipt(key, digest, result)
            with self._guard('ledger'):
                entries = self._load()
                entries[operation_id] = {'payload': digest, 'state': 'complete', 'result': result}
                self._save(entries)
            self._receipt_path(key).unlink(missing_ok=True)
            return result
