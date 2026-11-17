"""The local take store: one take file per demo, the §3 records on disk.

The stdio `@capturd` adapter's durable review state (GAPS-NOW U1/M2). The
demo id is the job id locally; the hosted job store is L8d's and this is not
it. A review opens by seeding one take per recorded step: the take's
`sourceRevision` is the SHA-1 of the step's pixels (40 lowercase hex, §3), a
step without pixels is a `failed` take (never `ready`), and each shot selects
take 0. Every write goes through `persistence.dumps` and an atomic replace,
so a crash leaves the previous file whole.

State moves are the engine's own: `approval.approve/reject/revoke`; the
export gate is `shots.export_blocked` over the selected takes (§6).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import threading
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from . import approval, persistence
from .shots import export_blocked
from .takes import KIND, TakeError

FILENAME = "takes.json"


class TakeStoreError(ValueError):
    """The review or the take named does not exist, or the move is refused."""


class RevisionConflict(TakeStoreError):
    """§4: `expectedRevision` is not the take's revision. No state changed."""

    def __init__(self, expected: str, actual: str):
        super().__init__(f"revision conflict: expected {expected!r}, take is at {actual!r}")
        self.expected, self.actual = expected, actual

    def payload(self) -> dict[str, Any]:
        return {"code": "conflict",
                "details": {"reason": "revision_conflict",
                            "expected": self.expected, "actual": self.actual}}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _content_revision(data: dict[str, Any]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha1(encoded.encode('utf-8')).hexdigest()


def _seed_take(demo_id: str, shot: int, step: dict[str, Any], created_at: str) -> dict[str, Any]:
    pixels = None
    if step.get("screenshotBase64") and not step.get("screenshotError"):
        try:
            pixels = base64.b64decode(step["screenshotBase64"], validate=True)
        except (binascii.Error, ValueError):
            pixels = None
    digest = hashlib.sha1(pixels if pixels else b"").hexdigest()
    return {
        "shotIndex": shot,
        "takeIndex": 0,
        "state": "ready" if pixels else "failed",
        "jobId": demo_id,
        "attemptId": f"att_{uuid.uuid4().hex}",
        "sourceRevision": digest,
        "sourceArtifactId": f"local_{digest[:16]}" if pixels else None,
        "kind": KIND,
        "direction": {"caption": step.get("annotation"), "voiceover": step.get("voiceover"),
                      "camera": "semantic"},
        "durationMs": None,
        "createdAt": created_at,
        "approvedBy": None,
        "approvedAt": None,
        "rejectedReason": None,
        "revokedBy": None,
        "revokedAt": None,
        "revokedReason": None,
    }


class TakeStore:
    """Review state for the demos one `DemoForge` owns."""

    def __init__(self, forge: Any):
        self._forge = forge
        self._lock = threading.Lock()

    # ---- file ------------------------------------------------------------

    def _path(self, demo_id: str):
        return self._forge.demo_dir(demo_id) / FILENAME

    @contextmanager
    def _guard(self, demo_id: str):
        path = self._forge.demo_dir(demo_id) / '.takes.lock'
        with self._lock, path.open('a+b') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _snapshot(self, demo_id: str) -> dict[str, Any]:
        data = self._forge.load_spec(demo_id)
        self._forge._inline_screenshots(data, demo_id)
        return data

    def _read(self, demo_id: str) -> dict[str, Any] | None:
        path = self._path(demo_id)
        if not path.is_file():
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        return {"takes": [persistence.loads(text) for text in doc["takes"]],
                "selection": {int(k): v for k, v in doc["selection"].items()},
                "storeRevision": doc.get("storeRevision", 0),
                "contentRevision": doc.get("contentRevision"),
                "approvedContent": doc.get("approvedContent", {}),
                "history": doc.get("history", [])}

    def _write(self, demo_id: str, review: dict[str, Any]) -> None:
        path = self._path(demo_id)
        doc = {"takes": [persistence.dumps(t) for t in review["takes"]],
               "selection": {str(k): v for k, v in sorted(review["selection"].items())},
               "storeRevision": review.get("storeRevision", 0) + 1,
               "contentRevision": review.get("contentRevision"),
               "approvedContent": review.get("approvedContent", {}),
               "history": review.get("history", [])}
        tmp = path.with_name(f".{FILENAME}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        review['storeRevision'] = doc['storeRevision']

    # ---- reads -----------------------------------------------------------

    def review(self, demo_id: str) -> dict[str, Any] | None:
        """The demo's take set and selection, or None if no review is open."""
        return self._read(demo_id)

    def open_review(self, demo_id: str) -> dict[str, Any]:
        """Seed one take per step on first call; later calls return it as is."""
        with self._guard(demo_id):
            existing = self._read(demo_id)
            if existing is not None:
                revision = _content_revision(self._snapshot(demo_id))
                old = existing['contentRevision']
                if old != revision:
                    for index, take in enumerate(existing['takes']):
                        if take['state'] == 'approved':
                            existing['history'].append({'shotIndex': take['shotIndex'], 'takeIndex': take['takeIndex'],
                                'from': 'approved', 'to': 'reopened', 'oldContentRevision': old,
                                'newContentRevision': revision, 'at': _now()})
                            existing['takes'][index] = approval.revoke(take, by='system', at=_now(), reason='content changed')
                    existing['approvedContent'] = {}
                    existing['contentRevision'] = revision
                    self._write(demo_id, existing)
                return existing
            data = self._forge.load_spec(demo_id)
            self._forge._inline_screenshots(data, demo_id)
            at = _now()
            takes = [_seed_take(demo_id, shot, step, at)
                     for shot, step in enumerate(data.get("steps") or [])]
            review = {"takes": takes, "selection": {t["shotIndex"]: 0 for t in takes},
                      "contentRevision": _content_revision(data), "approvedContent": {}, "history": []}
            self._write(demo_id, review)
            return review

    def export_blocks(self, demo_id: str, data: dict[str, Any] | None = None) -> list[dict[str, Any]] | None:
        """§6 blocks for exporting the selected takes; None without a review."""
        review = self._read(demo_id)
        if review is None:
            return None
        shots = sorted({t["shotIndex"] for t in review["takes"]})
        included = sorted(review["selection"].items())
        blocks = export_blocked(shots, review["takes"], review["selection"], included)
        current = _content_revision(data if data is not None else self._snapshot(demo_id))
        for shot, take in included:
            if review['approvedContent'].get(f'{shot}:{take}') != current:
                blocks.append({'code': 'content_changed', 'shotIndex': shot, 'takeIndex': take,
                               'needed': 'approval of the current content'})
        return blocks

    # ---- moves -----------------------------------------------------------

    def _move(self, demo_id: str, shot: int, take: int, change) -> dict[str, Any]:
        observed = self._read(demo_id)
        with self._guard(demo_id):
            review = self._read(demo_id)
            if observed is not None and review is not None and observed['storeRevision'] != review['storeRevision']:
                raise TakeStoreError('stale review store revision')
            if review is None:
                raise TakeStoreError(f"no review is open for demo {demo_id!r}; call take.list first")
            for i, record in enumerate(review["takes"]):
                if record["shotIndex"] == shot and record["takeIndex"] == take:
                    try:
                        updated = change(record)
                    except (approval.ApprovalError, TakeError) as exc:
                        raise TakeStoreError(str(exc)) from exc
                    review["takes"][i] = updated
                    key = f'{shot}:{take}'
                    if updated['state'] == 'approved':
                        current = _content_revision(self._snapshot(demo_id))
                        if current != review['contentRevision']:
                            raise TakeStoreError('content changed; reopen review first')
                        review['approvedContent'][key] = current
                    else:
                        review['approvedContent'].pop(key, None)
                    self._write(demo_id, review)
                    return updated
            raise TakeStoreError(f"demo {demo_id!r} has no shot {shot} take {take}")

    def approve(self, demo_id: str, shot: int, take: int, *, by: str,
                expected_revision: str) -> dict[str, Any]:
        def change(record):
            if expected_revision != record["sourceRevision"]:
                raise RevisionConflict(expected_revision, record["sourceRevision"])
            return approval.approve(record, by=by, at=_now(), source_revision=expected_revision)
        return self._move(demo_id, shot, take, change)

    def reject(self, demo_id: str, shot: int, take: int, *, reason: str) -> dict[str, Any]:
        return self._move(demo_id, shot, take,
                          lambda record: approval.reject(record, reason=reason))

    def revoke(self, demo_id: str, shot: int, take: int, *, by: str, reason: str) -> dict[str, Any]:
        return self._move(demo_id, shot, take,
                          lambda record: approval.revoke(record, by=by, at=_now(), reason=reason))
