"""The engine contract — the one implementation both surfaces serve.

The manifest is the single source of truth for engine id, engine version,
contract version and artifact kinds. Both the MCP tool (``engine.contract``)
and the hosted service's ``/healthz`` answer import :func:`engine_contract`
from here; neither grows its own parse or its own cache. The manifest is
read **only** through :func:`capturd.engine.load_manifest`, and the path the
operator tool reports is taken from :func:`capturd.engine.manifest_path` — the
loader's own file — never derived a second time from this module's location.

The answer has one shape in both directions: ``ok`` is ``True`` with the
manifest and contract version on success, ``False`` with ``error``/``detail``
when the packaged manifest is missing or invalid. ``include_path=True`` adds
``manifestPath`` for the operator-scoped MCP tool; the public health answer
never carries a filesystem path, and a failure's ``detail`` has every
absolute path stripped before it leaves this module.
"""

from __future__ import annotations

import copy
import re
from functools import lru_cache
from typing import Any

from capturd.engine import load_manifest, manifest_path

# The loader's own ``data`` seam: ``None`` reads the packaged file, bytes are
# parsed directly (tests hand in a manifest without touching disk). The parse
# is cached per distinct payload, so the packaged file is read once per
# process.
_MANIFEST_BYTES: bytes | None = None

# An absolute filesystem path — POSIX (``/...``) or Windows (``C:\...``). The
# loader's ``OSError`` text names the file it could not read, and a failure
# answer is public; strip any such path before it leaves this module. This is
# defensive: the damage modes measured on this head mostly produce path-free
# JSON/decoding messages, but a missing file does name its path.
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s'\"]+")


def _without_filesystem_paths(detail: str) -> str:
    """Replace every absolute-looking path in *detail* with its final name.

    ``/srv/capturd/manifest.json`` becomes ``manifest.json``: no directory
    tree survives, and the caller still learns which file failed.
    """
    def leaf(match: re.Match[str]) -> str:
        return match.group(0).replace("\\", "/").rsplit("/", 1)[-1]

    return _ABSOLUTE_PATH.sub(leaf, detail)


@lru_cache(maxsize=None)
def _manifest_for(data: bytes | None) -> dict[str, Any]:
    return load_manifest(data)


def _failure_answer(exc: Exception, *, include_path: bool) -> dict[str, Any]:
    """The one failure shape, with the exception's path text scrubbed."""
    answer: dict[str, Any] = {
        "ok": False,
        "manifest": None,
        "contractVersion": None,
        "error": type(exc).__name__,
        "detail": _without_filesystem_paths(str(exc)),
    }
    if include_path:
        answer["manifestPath"] = str(manifest_path())
    return answer


def engine_contract(*, include_path: bool = False) -> dict[str, Any]:
    """The engine's own contract, loaded once per process.

    Always returns the same keys — ``ok``, ``manifest``, ``contractVersion``,
    ``error``, ``detail`` — so a caller can branch on ``ok`` alone. The
    manifest is a fresh deep copy, so callers cannot mutate the cache. The
    filesystem path appears as ``manifestPath`` only when ``include_path`` is
    true (the operator-facing MCP tool), and it is the loader's own path asked
    for through :func:`capturd.engine.manifest_path`. The public ``/healthz``
    answer never carries it.

    ``ManifestError`` is the loader's declared failure, but the catch is
    deliberately broad: the loader wraps ``OSError`` (missing file, permission,
    a directory in the file's place) and ``ValueError`` (bad UTF-8, bad JSON),
    while a pathologically nested payload still raises ``RecursionError`` from
    ``json.loads``. This endpoint exists to report damage, so it reports that
    too with the same ``ok: False`` shape instead of 500ing.
    """
    try:
        manifest = copy.deepcopy(_manifest_for(_MANIFEST_BYTES))
    except Exception as exc:  # noqa: BLE001 — the endpoint must answer, see docstring
        return _failure_answer(exc, include_path=include_path)
    answer: dict[str, Any] = {
        "ok": True,
        "manifest": manifest,
        "contractVersion": manifest["contractVersion"],
        "error": None,
        "detail": None,
    }
    if include_path:
        answer["manifestPath"] = str(manifest_path())
    return answer
