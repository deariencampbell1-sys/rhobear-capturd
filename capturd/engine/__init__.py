"""Captur'd engine: the packaged manifest and the artifact byte cap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_ARTIFACT_BYTES = 1_073_741_824  # the manifest's maxArtifactBytes
_MANIFEST_PATH = Path(__file__).with_name("manifest.json")

# A deployable manifest carries every ADOPTION.md field with the right shape.
_MANIFEST_REQUIRED: dict[str, type] = {
    "schemaVersion": int, "contractVersion": str, "engine": str, "engineVersion": str,
    "displayName": str, "mark": str, "inputSchema": dict, "outputSchema": dict,
    "artifactKinds": list, "capabilities": list, "limits": dict, "execution": dict,
}


class ManifestError(RuntimeError):
    """The packaged engine manifest is missing or unreadable."""


class ArtifactTooLarge(ValueError):
    """A produced artifact exceeds the engine's maxArtifactBytes cap.

    Never retried: an artifact's size cannot change on a second attempt.
    Carries the measured ``size`` and the ``cap`` it broke.
    """

    def __init__(self, message: str, *, size: int, cap: int) -> None:
        super().__init__(message)
        self.size = size
        self.cap = cap


def load_manifest(data: bytes | None = None) -> dict[str, Any]:
    """Parse the packaged manifest (or given bytes); a fresh, equal object per call."""
    if data is None:
        try:
            data = _MANIFEST_PATH.read_bytes()
        except OSError as exc:
            raise ManifestError(f"packaged engine manifest unreadable: {exc}") from exc
    try:
        manifest = json.loads(data)
    except ValueError as exc:
        raise ManifestError(f"packaged engine manifest unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("packaged engine manifest is not a JSON object")
    for key, expected in _MANIFEST_REQUIRED.items():
        if key not in manifest:
            raise ManifestError(f"packaged engine manifest is missing required key: {key}")
        if isinstance(manifest[key], bool) or not isinstance(manifest[key], expected):
            raise ManifestError(
                f"packaged engine manifest key {key!r} must be {expected.__name__}, "
                f"not {type(manifest[key]).__name__}")
    return manifest


def manifest_path() -> Path:
    """The file :func:`load_manifest` reads, as a path — never re-derived.

    Public so callers that must report *where* the manifest came from ask the
    loader itself instead of rebuilding the path from ``__file__`` or
    reaching into this module's private state.
    """
    return _MANIFEST_PATH


def check_artifact_size(path: str | Path) -> int:
    """Return the file's byte count; refuse anything over MAX_ARTIFACT_BYTES.

    A missing file raises its normal file error and never passes. The refusal
    names the file's size and the cap; whether the file stays on disk is the
    caller's ruling (the still funnel deletes it, the local export keeps it).
    """
    size = Path(path).stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ArtifactTooLarge(
            f"artifact {path} is {size} bytes, over the {MAX_ARTIFACT_BYTES}-byte cap",
            size=size, cap=MAX_ARTIFACT_BYTES,
        )
    return size
