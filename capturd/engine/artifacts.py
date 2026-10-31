"""A pure registry of L8d artifact ids and revisions (contract §1, §7).

The caller hands in ids and revisions it already resolved from L8d; no bytes,
no fetch, no hash check, no revision resolution, no L8d client. One artifact
id keeps exactly one revision for the life of the registry: a re-recorded
artifact is a new artifact and must take a new id, so the same id at a
different revision is refused.
"""

from __future__ import annotations

from typing import Any, Mapping


class ArtifactError(ValueError):
    """An artifact id is being re-registered at a different revision, or the
    known entry is not an artifact mapping at all."""


def _copied(value: Any) -> Any:
    """A copy of `value` sharing no mapping with it, at any depth."""
    if isinstance(value, Mapping):
        return {key: _copied(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copied(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copied(item) for item in value)
    return value


def register(
    registry: Mapping[str, Any], *, artifact_id: str, revision: str, kind: str,
) -> dict[str, Any]:
    """Return a new mapping with `artifact_id` registered at `revision`.

    The same id at the same revision and kind is a no-op: an equal mapping
    comes back and the recorded entry is left alone. The same id at the same
    revision with a different kind raises ArtifactError — an artifact's kind
    cannot change. The same id at a different revision also raises
    ArtifactError, as does a known id mapped to `None` or to anything that
    is not a mapping. The returned mapping shares no per-artifact dict with
    `registry`, and the input is never mutated.
    """
    if artifact_id in registry:
        known = registry[artifact_id]
        if not isinstance(known, Mapping):
            raise ArtifactError(
                f"artifact {artifact_id!r} is registered as {known!r}, "
                "which is not an artifact mapping")
        if known.get("revision") == revision:
            if known.get("kind") != kind:
                raise ArtifactError(
                    f"artifact {artifact_id!r} is registered at revision "
                    f"{revision!r} as kind {known.get('kind')!r}, not {kind!r}; "
                    "an artifact's kind cannot change")
            return _copied(registry)
        raise ArtifactError(
            f"artifact {artifact_id!r} is registered at {known.get('revision')!r}, "
            f"not {revision!r}; a re-recorded artifact takes a new id")
    fresh = _copied(registry)
    fresh[artifact_id] = {"revision": revision, "kind": kind}
    return fresh
