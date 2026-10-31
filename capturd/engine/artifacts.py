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
    """An artifact id is being re-registered at a different revision."""


def register(
    registry: Mapping[str, Any], *, artifact_id: str, revision: str, kind: str,
) -> dict[str, Any]:
    """Return a new mapping with `artifact_id` registered at `revision`.

    The same id at the same revision is a no-op: an equal mapping comes back
    and the recorded kind is left alone. The same id at a different revision
    raises ArtifactError. The input registry is never mutated.
    """
    known = registry.get(artifact_id)
    if known is not None:
        if known.get("revision") == revision:
            return dict(registry)
        raise ArtifactError(
            f"artifact {artifact_id!r} is registered at {known.get('revision')!r}, "
            f"not {revision!r}; a re-recorded artifact takes a new id")
    return {**registry, artifact_id: {"revision": revision, "kind": kind}}
