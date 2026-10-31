"""The engine contract — the one implementation both surfaces serve.

The manifest is the single source of truth for engine id, engine version,
contract version and artifact kinds. Both the MCP tool (``engine.contract``)
and the hosted service's ``/healthz`` answer import :func:`engine_contract`
from here; neither grows its own parse or its own cache. The manifest is
read **only** through :func:`capturd.engine.load_manifest`.

The answer has one shape in both directions: ``ok`` is ``True`` with the
manifest and contract version on success, ``False`` with ``error``/``detail``
when the packaged manifest is missing or invalid. ``include_path=True`` adds
``manifestPath`` for the operator-scoped MCP tool; the public health answer
never carries a filesystem path.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any

from capturd.engine import ManifestError, load_manifest

# This module sits beside the packaged manifest, so the path it reports is the
# same file ``capturd.engine.load_manifest`` reads — without reaching into that
# module's private ``_MANIFEST_PATH``.
MANIFEST_PATH = Path(__file__).with_name("manifest.json")

# The loader's own ``data`` seam: ``None`` reads the packaged file, bytes are
# parsed directly (tests hand in a manifest without touching disk). The parse
# is cached per distinct payload, so the packaged file is read once per
# process.
_MANIFEST_BYTES: bytes | None = None


@lru_cache(maxsize=None)
def _manifest_for(data: bytes | None) -> dict[str, Any]:
    return load_manifest(data)


def engine_contract(*, include_path: bool = False) -> dict[str, Any]:
    """The engine's own contract, loaded once per process.

    Always returns the same keys — ``ok``, ``manifest``, ``contractVersion``,
    ``error``, ``detail`` — so a caller can branch on ``ok`` alone. The
    manifest is a fresh deep copy, so callers cannot mutate the cache. The
    filesystem path appears as ``manifestPath`` only when ``include_path`` is
    true (the operator-facing MCP tool); the public ``/healthz`` answer never
    carries it, and a failure's ``detail`` names the file without the
    directory.
    """
    try:
        manifest = copy.deepcopy(_manifest_for(_MANIFEST_BYTES))
    except ManifestError as exc:
        detail = str(exc).replace(str(MANIFEST_PATH), MANIFEST_PATH.name)
        answer: dict[str, Any] = {
            "ok": False,
            "manifest": None,
            "contractVersion": None,
            "error": type(exc).__name__,
            "detail": detail,
        }
    else:
        answer = {
            "ok": True,
            "manifest": manifest,
            "contractVersion": manifest["contractVersion"],
            "error": None,
            "detail": None,
        }
    if include_path:
        answer["manifestPath"] = str(MANIFEST_PATH)
    return answer
