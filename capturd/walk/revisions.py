"""Immutable engine-local revisions for Captur'd walkthrough specs (W2-05b).

Only the immutable-version concept, extracted neutral: content-hashed
revision ids, atomic write/read/list, immutable approved/published pointers,
copy-old-revision-to-new-draft rollback. Plain local files under a
caller-chosen root; the store reaches no other process and no wire.

Rules: revision bytes are written once and never change behind an id
(``rv_<seq:06d>_<sha256 of the canonical bytes>``), so every read re-hashes
what it returns; ``save_revision`` mints a fresh id per call, so two writers
freezing the same draft at once get two distinct revisions under one lock
per root; ``approve``/``publish`` move pointers only — writing into a frozen
slot forks a new revision; ``rollback_to`` copies old bytes into a fresh
draft, and a missing id raises ``RevisionNotFound`` naming the id before
anything is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "RevisionCorrupt",
    "RevisionError",
    "RevisionNotFound",
    "RevisionRecord",
    "RevisionStore",
    "canonical_bytes",
    "content_hash",
    "hash_spec",
]

_REV_ID_RE = re.compile(r"^rv_(\d{6})_([0-9a-f]{64})$")
_DRAFT_NAME = "draft.json"
_POINTERS_NAME = "pointers.json"
_REVISIONS_DIR = "revisions"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RevisionError(RuntimeError):
    """Base refusal for the immutable-revision store."""


class RevisionNotFound(RevisionError):
    """A revision id does not exist — the message always names the id."""

    def __init__(self, revision_id: str) -> None:
        self.revision_id = str(revision_id)
        super().__init__(f"revision not found: {self.revision_id}")


class RevisionCorrupt(RevisionError):
    """Stored bytes do not match the hash their id promises."""


@dataclass(frozen=True)
class RevisionRecord:
    """One immutable revision: identity, hash and on-disk facts."""

    id: str
    sha256: str
    seq: int
    created_at: float
    size: int


def canonical_bytes(spec: Mapping[str, Any]) -> bytes:
    """Deterministic UTF-8 JSON — the same spec always produces the same bytes."""
    try:
        text = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RevisionError(f"spec is not JSON-serializable: {exc}") from exc
    return text.encode("utf-8")


def content_hash(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def hash_spec(spec: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of a spec's canonical bytes."""
    return content_hash(canonical_bytes(spec))


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(root: Path) -> threading.RLock:
    """One lock per resolved root, shared by every store instance on it."""
    key = os.path.abspath(str(root))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _atomic_write(path: Path, data: bytes) -> None:
    """Publish bytes so any reader sees the old file or the whole new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # pragma: no cover - best-effort cleanup only
            pass


def _parse_object(data: bytes, label: str) -> dict[str, Any]:
    """Decode a JSON object or refuse, naming the file that is wrong (S6)."""
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RevisionCorrupt(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RevisionCorrupt(f"{label} must contain a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class RevisionStore:
    """Immutable revisions, one mutable draft and two pointers for one root.

    Layout: ``root/draft.json`` (mutable working copy), ``root/pointers.json``
    (approved/published), ``root/revisions/rv_<seq>_<sha>.json`` (frozen).
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.revisions_dir = self.root / _REVISIONS_DIR
        self._lock = _lock_for(self.root)

    # -- paths + draft ------------------------------------------------

    def _revision_path(self, revision_id: str) -> Path:
        """Strict id → path; anything that is not an id is simply not found."""
        if not isinstance(revision_id, str) or not _REV_ID_RE.match(revision_id):
            raise RevisionNotFound(str(revision_id))
        return self.revisions_dir / f"{revision_id}.json"

    def _existing_record(self, revision_id: str) -> RevisionRecord:
        path = self._revision_path(revision_id)
        if not path.is_file():
            raise RevisionNotFound(revision_id)
        match = _REV_ID_RE.match(revision_id)
        stat = path.stat()
        return RevisionRecord(
            id=revision_id, seq=int(match.group(1)), sha256=match.group(2),
            created_at=stat.st_mtime, size=stat.st_size,
        )

    def load_draft(self) -> dict[str, Any] | None:
        """The current draft spec, or ``None`` when no draft exists yet."""
        path = self.root / _DRAFT_NAME
        if not path.is_file():
            return None
        envelope = _parse_object(path.read_bytes(), _DRAFT_NAME)
        spec = envelope.get("spec")
        if not isinstance(spec, dict):
            raise RevisionCorrupt(f"{_DRAFT_NAME} has no spec object")
        return spec

    def _write_draft(
        self, spec: dict[str, Any], data: bytes, forked_from: str | None
    ) -> dict[str, Any]:
        """Atomically publish the mutable draft. Revisions are never touched."""
        envelope = {
            "forkedFrom": forked_from, "sha256": content_hash(data),
            "size": len(data), "spec": spec, "updatedAt": time.time(),
        }
        with self._lock:
            _atomic_write(self.root / _DRAFT_NAME, canonical_bytes(envelope))
        return {key: envelope[key] for key in ("forkedFrom", "sha256", "size", "updatedAt")}

    def save_draft(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically replace the mutable draft with ``spec``."""
        if not isinstance(spec, dict):
            raise RevisionError("draft spec must be a JSON object")
        data = canonical_bytes(spec)
        return self._write_draft(spec, data, None)

    def save_revision(
        self,
        spec: Mapping[str, Any] | None = None,
        *,
        revision_id: str | None = None,
    ) -> RevisionRecord:
        """Freeze ``spec`` (or the current draft) into a NEW immutable revision.

        ``revision_id`` names a slot the caller believes it is updating; the
        store never rewrites frozen bytes. An approved or published target
        always forks a fresh revision, any other target forks too unless the
        content is byte-identical (then its record returns unchanged), and an
        unknown target raises ``RevisionNotFound``.
        """
        if spec is None:
            spec = self.load_draft()
            if spec is None:
                raise RevisionError("no draft to freeze; pass a spec")
        if not isinstance(spec, dict):
            raise RevisionError("revision spec must be a JSON object")
        data = canonical_bytes(spec)
        digest = content_hash(data)
        with self._lock:
            if revision_id is not None:
                existing = self._existing_record(revision_id)  # names unknown ids
                pointers = self._load_pointers()
                frozen_slot = revision_id in (pointers["approved"], pointers["published"])
                if not frozen_slot and content_hash(self.read_bytes(revision_id)) == digest:
                    return existing  # byte-identical, unapproved → no new revision
                # approved/published slot (or changed content) → a NEW revision
            return self._mint(digest, data)

    def _mint(self, digest: str, data: bytes) -> RevisionRecord:
        seq = self._next_seq()
        revision_id = f"rv_{seq:06d}_{digest}"
        path = self.revisions_dir / f"{revision_id}.json"
        if path.is_file():
            # Same seq ⇒ same id ⇒ same bytes; verify rather than assume.
            if content_hash(path.read_bytes()) != digest:
                raise RevisionCorrupt(
                    f"revision {revision_id}: existing bytes do not match their id"
                )
        else:
            _atomic_write(path, data)
        stat = path.stat()
        return RevisionRecord(revision_id, digest, seq, stat.st_mtime, stat.st_size)

    def _next_seq(self) -> int:
        seq = 0
        if self.revisions_dir.is_dir():
            for entry in self.revisions_dir.iterdir():
                match = _REV_ID_RE.match(entry.stem)
                if match:
                    seq = max(seq, int(match.group(1)))
        return seq + 1

    def read_bytes(self, revision_id: str) -> bytes:
        """Frozen bytes, hash-verified against the id that promises them."""
        path = self._revision_path(revision_id)
        if not path.is_file():
            raise RevisionNotFound(revision_id)
        data = path.read_bytes()
        expected = _REV_ID_RE.match(revision_id).group(2)
        if content_hash(data) != expected:
            raise RevisionCorrupt(f"revision {revision_id}: bytes do not match the id hash")
        return data

    def read(self, revision_id: str) -> dict[str, Any]:
        """The frozen spec for ``revision_id`` (hash-verified on every read)."""
        return _parse_object(self.read_bytes(revision_id), f"revision {revision_id}")

    def list_revisions(self) -> list[RevisionRecord]:
        """Every revision, newest first. Stray files are ignored."""
        if not self.revisions_dir.is_dir():
            return []
        records: list[RevisionRecord] = []
        for entry in self.revisions_dir.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            match = _REV_ID_RE.match(entry.stem)
            if not match:
                continue
            stat = entry.stat()
            records.append(RevisionRecord(
                entry.stem, match.group(2), int(match.group(1)),
                stat.st_mtime, stat.st_size,
            ))
        records.sort(key=lambda record: record.seq, reverse=True)
        return records

    def _load_pointers(self) -> dict[str, Any]:
        path = self.root / _POINTERS_NAME
        if not path.is_file():
            return {"approved": None, "published": None}
        parsed = _parse_object(path.read_bytes(), _POINTERS_NAME)
        return {"approved": parsed.get("approved"), "published": parsed.get("published")}

    def _set_pointer(self, kind: str, revision_id: str) -> str:
        with self._lock:
            self.read_bytes(revision_id)  # re-hash: never point at corrupt bytes
            pointers = self._load_pointers()
            pointers[kind] = revision_id
            _atomic_write(self.root / _POINTERS_NAME, canonical_bytes(pointers))
        return revision_id

    def approve(self, revision_id: str) -> str:
        """Point ``approved`` at a frozen revision. Bytes never change."""
        return self._set_pointer("approved", revision_id)

    def publish(self, revision_id: str) -> str:
        """Point ``published`` at a frozen revision. Bytes never change."""
        return self._set_pointer("published", revision_id)

    def approved_revision_id(self) -> str | None:
        return self._load_pointers()["approved"]

    def published_revision_id(self) -> str | None:
        return self._load_pointers()["published"]

    def rollback_to(self, revision_id: str) -> dict[str, Any]:
        """Copy an old revision's frozen bytes into a fresh mutable draft.

        History is preserved — no revision is created, changed or removed —
        and a missing or corrupt id refuses before anything is written.
        """
        data = self.read_bytes(revision_id)  # raises before any write
        return self._write_draft(_parse_object(data, f"revision {revision_id}"),
                                 data, revision_id)
