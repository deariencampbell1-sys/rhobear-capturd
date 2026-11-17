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
from datetime import datetime, timezone
from typing import Any

from . import approval, persistence, shots
from .shots import export_blocked
from .takes import KIND, TakeError, transition

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


class DirectionError(TakeStoreError):
    """A direction field is malformed or not one this engine renders yet."""

    def __init__(self, field: str, detail: str):
        super().__init__(f"direction {field}: {detail}")
        self.field = field

    def payload(self) -> dict[str, Any]:
        return {"code": "invalid_direction", "field": self.field, "detail": str(self)}


#: The direction fields a take can carry today, each one rendered or kept:
#: `caption` replaces the step's on-screen line in the export, `note` keeps the
#: person's own words for the agent, `camera` is the semantic camera. Voice,
#: cursor, effect and transition direction are refused by name until the
#: export renders them (GAPS-NOW M6).
_CAPTION_MAX, _NOTE_MAX = 500, 2000
_CAMERAS = ("semantic",)


def validate_direction(direction: Any) -> dict[str, Any]:
    """Return the direction with defaults filled, or raise DirectionError."""
    if not isinstance(direction, dict) or not direction:
        raise DirectionError("direction", "is a non-empty object")
    for field in direction:
        if field not in ("caption", "note", "camera"):
            raise DirectionError(field, "is not a direction this engine renders yet")
    for field, limit in (("caption", _CAPTION_MAX), ("note", _NOTE_MAX)):
        value = direction.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise DirectionError(field, f"is text of at most {limit} characters")
    camera = direction.get("camera", "semantic")
    if camera not in _CAMERAS:
        raise DirectionError("camera", f"is one of {list(_CAMERAS)}")
    return {"caption": direction.get("caption"), "note": direction.get("note"),
            "camera": camera}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
        # caption None = the step's own line, whatever enrichment makes it.
        "direction": {"caption": None, "note": None, "camera": "semantic"},
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

    def _read(self, demo_id: str) -> dict[str, Any] | None:
        path = self._path(demo_id)
        if not path.is_file():
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        return {"takes": [persistence.loads(text) for text in doc["takes"]],
                "selection": {int(k): v for k, v in doc["selection"].items()}}

    def _write(self, demo_id: str, review: dict[str, Any]) -> None:
        path = self._path(demo_id)
        doc = {"takes": [persistence.dumps(t) for t in review["takes"]],
               "selection": {str(k): v for k, v in sorted(review["selection"].items())}}
        tmp = path.with_name(f".{FILENAME}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # ---- reads -----------------------------------------------------------

    def review(self, demo_id: str) -> dict[str, Any] | None:
        """The demo's take set and selection, or None if no review is open."""
        return self._read(demo_id)

    def open_review(self, demo_id: str) -> dict[str, Any]:
        """Seed one take per step on first call; later calls return it as is."""
        with self._lock:
            existing = self._read(demo_id)
            if existing is not None:
                return existing
            data = self._forge.load_spec(demo_id)
            self._forge._inline_screenshots(data, demo_id)
            at = _now()
            takes = [_seed_take(demo_id, shot, step, at)
                     for shot, step in enumerate(data.get("steps") or [])]
            review = {"takes": takes, "selection": {t["shotIndex"]: 0 for t in takes}}
            self._write(demo_id, review)
            return review

    def export_blocks(self, demo_id: str) -> list[dict[str, Any]] | None:
        """§6 blocks for exporting the selected takes; None without a review."""
        review = self._read(demo_id)
        if review is None:
            return None
        shots = sorted({t["shotIndex"] for t in review["takes"]})
        included = sorted(review["selection"].items())
        return export_blocked(shots, review["takes"], review["selection"], included)

    # ---- moves -----------------------------------------------------------

    def _move(self, demo_id: str, shot: int, take: int, change) -> dict[str, Any]:
        with self._lock:
            review = self._read(demo_id)
            if review is None:
                raise TakeStoreError(f"no review is open for demo {demo_id!r}; call take.list first")
            for i, record in enumerate(review["takes"]):
                if record["shotIndex"] == shot and record["takeIndex"] == take:
                    try:
                        updated = change(record)
                    except (approval.ApprovalError, TakeError) as exc:
                        raise TakeStoreError(str(exc)) from exc
                    review["takes"][i] = updated
                    if updated["state"] == "approved":
                        # A person chose this take for the shot.
                        review["selection"][shot] = take
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

    def retake(self, demo_id: str, shot: int, take: int, *, direction: Any,
               expected_revision: str) -> dict[str, Any]:
        """§4 direction-only retake: same pixels, new direction, a new take.

        The new take is `ready` at once (nothing is re-recorded) and is not
        selected until a person approves it; every other record is untouched.
        """
        clean = validate_direction(direction)
        with self._lock:
            review = self._read(demo_id)
            if review is None:
                raise TakeStoreError(f"no review is open for demo {demo_id!r}; call take.list first")
            shot_takes = [t for t in review["takes"] if t["shotIndex"] == shot]
            base = next((t for t in shot_takes if t["takeIndex"] == take), None)
            if base is None:
                raise TakeStoreError(f"demo {demo_id!r} has no shot {shot} take {take}")
            if expected_revision != base["sourceRevision"]:
                raise RevisionConflict(expected_revision, base["sourceRevision"])
            if base["state"] == "failed" or base["sourceArtifactId"] is None:
                raise TakeStoreError(
                    f"shot {shot} take {take} has no pixels to keep; record the shot again")
            draft = shots.retake(
                base, take_index=max(t["takeIndex"] for t in shot_takes) + 1,
                job_id=demo_id, attempt_id=f"att_{uuid.uuid4().hex}",
                direction=clean, created_at=_now())
            new = transition(draft, "ready")
            review["takes"].append(new)
            self._write(demo_id, review)
            return new

    def selected_directions(self, demo_id: str) -> dict[int, dict[str, Any]]:
        """The selected take's direction per shot; empty without a review."""
        review = self._read(demo_id)
        if review is None:
            return {}
        by_pair = {(t["shotIndex"], t["takeIndex"]): t for t in review["takes"]}
        return {shot: by_pair[(shot, take)]["direction"]
                for shot, take in review["selection"].items() if (shot, take) in by_pair}
