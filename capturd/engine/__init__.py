"""Captur'd engine: the packaged manifest and the artifact byte cap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_ARTIFACT_BYTES = 1_073_741_824  # the manifest's maxArtifactBytes
_MANIFEST_PATH = Path(__file__).with_name("manifest.json")


class ManifestError(RuntimeError):
    """The packaged engine manifest is missing or unreadable."""


class ArtifactTooLarge(ValueError):
    """A produced artifact exceeds the engine's maxArtifactBytes cap."""


def load_manifest() -> dict[str, Any]:
    """Parse the packaged manifest; every call returns a fresh, equal object."""
    try:
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"packaged engine manifest unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("packaged engine manifest is not a JSON object")
    return manifest


def check_artifact_size(path: str | Path) -> int:
    """Return the file's byte count; refuse anything over MAX_ARTIFACT_BYTES.

    A missing file raises its normal file error and never passes; an
    oversized file stays on disk while the error names its size and the cap.
    """
    size = Path(path).stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ArtifactTooLarge(
            f"artifact {path} is {size} bytes, over the {MAX_ARTIFACT_BYTES}-byte cap"
        )
    return size
