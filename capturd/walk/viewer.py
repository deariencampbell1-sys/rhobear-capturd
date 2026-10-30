r"""Viewer renderer — turn a DemoSpec (dict) into the self-contained HTML page.

The viewer template (``templates/viewer.html``) ships with the package as
``package_data``. It contains a placeholder ``<script id="demo-data">`` block
that gets replaced with the actual demo JSON at render time.

Design notes:

* The output is a single self-contained file (zero deps, works from ``file://``).
* Screenshots + voiceover audio should be inlined as base64 BEFORE rendering.
  See ``coordinator.DemoForge._inline_screenshots`` for the inlining routine —
  callers can reuse it or do their own.
* The substitution uses ``re.sub`` with a lambda so JSON backslash sequences
  (``\1``, ``\g<name>``, screenshot paths on Windows) are NOT interpreted by
  the regex engine.
* Line endings on write are LF for cross-platform portability — the template's
  regex depends on it.

This module is the authoritative renderer. ``DemoForge.export_demo`` has its
own copy of the substitution for now (see coordinator.py); W1/W2 workers may
consolidate on this function.

Refusals on this path (missing template, malformed choices) are always
``ViewerRenderError``; the export boundary re-types them as
``DemoExportError`` (see coordinator.py).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from capturd.walk.schema import normalize_target, validate_choices


_SCRIPT_BLOCK_RE = re.compile(
    r'<script id="demo-data"[^>]*>\s*\n.*?\n\s*</script>',
    re.DOTALL,
)


class ViewerRenderError(RuntimeError):
    """Raised when the template is missing or malformed."""


def embed_json(obj: Any) -> str:
    """Serialize *obj* for safe embedding in the ``#demo-data`` block.

    Every ``<`` becomes ``\\u003c`` so no free-text field (a choice label,
    a step annotation) can end the ``<script>`` block early or inject a
    live tag — ``</script>``, ``<script>`` and ``<!--`` all lose their HTML
    meaning, while ``json.loads`` still returns the identical data. Both
    sinks (``render_viewer`` and ``DemoForge.export_demo``) share it.
    """
    return json.dumps(obj, indent=2).replace("<", "\\u003c")
def propagate_target_metadata(spec: dict[str, Any]) -> dict[str, Any]:
    """Normalize every step's ``interaction.target`` for the semantic camera.

    Legacy specs (pre W1-03-r1) gain the optional ``role`` / ``viewport`` /
    ``captureSpace`` / ``region`` keys; recorded hints survive. Runs at
    render time so old recordings carry usable target metadata without a
    re-record. The spec is modified in place and returned.
    """
    for step in spec.get("steps") or []:
        interaction = step.get("interaction") if isinstance(step, dict) else None
        target = interaction.get("target") if isinstance(interaction, dict) else None
        if isinstance(target, dict):
            interaction["target"] = normalize_target(target, spec.get("viewport"))
    return spec


def validate_spec_choices(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize every step's choices before rendering
    (Branching V2).

    Steps without ``choices`` (every old spec) pass through untouched. A
    malformed choice list — including an empty one — raises
    ``ViewerRenderError`` carrying ``schema.validate_choices``' message
    naming the offending field, instead of shipping a viewer whose buttons
    cannot work. The normalized list (exactly ``id`` / ``label`` /
    ``destination``) is written back into the step, so any key
    ``validate_choices`` strips (CTA / tracking / analytics-shaped fields)
    never reaches the embedded ``#demo-data`` payload of a render or an
    export. The spec is modified in place and returned.
    """
    steps = spec.get("steps") or []
    n = len(steps)
    for idx, step in enumerate(steps):
        if isinstance(step, dict) and step.get("choices") is not None:
            try:
                step["choices"] = validate_choices(step["choices"], n, step_index=idx)
            except ValueError as exc:
                raise ViewerRenderError(str(exc)) from exc
    return spec


def default_template_path() -> Path:
    """Path to the packaged viewer template."""
    return Path(__file__).resolve().parent / "templates" / "viewer.html"


def render_viewer(
    spec: dict[str, Any],
    *,
    template_path: Path | None = None,
) -> str:
    """Render a DemoSpec (as dict) into a self-contained HTML string.

    Parameters
    ----------
    spec:
        A ``DemoSpec.to_dict()`` payload. Screenshots + audio should already
        be inlined as base64 if you want a portable single-file result.
    template_path:
        Override the packaged template. Defaults to
        ``capturd/walk/templates/viewer.html``.

    Returns
    -------
    The rendered HTML as a UTF-8 string. Caller is responsible for writing it
    to disk with LF line endings (``.encode("utf-8")`` + ``write_bytes``).
    """
    tpath = template_path or default_template_path()
    if not tpath.is_file():
        raise ViewerRenderError(f"viewer template not found: {tpath}")

    spec = propagate_target_metadata(spec)
    validate_spec_choices(spec)

    template = tpath.read_text(encoding="utf-8")
    if not _SCRIPT_BLOCK_RE.search(template):
        raise ViewerRenderError(
            'viewer template missing <script id="demo-data"> placeholder block'
        )

    spec_json = embed_json(spec)
    new_block = (
        f'<script id="demo-data" type="application/json">\n{spec_json}\n  </script>'
    )
    # Lambda replacement — do NOT let re.sub interpret backslash escapes.
    return _SCRIPT_BLOCK_RE.sub(lambda _m: new_block, template, count=1)


def render_viewer_to_file(
    spec: dict[str, Any],
    out_path: Path,
    *,
    template_path: Path | None = None,
) -> Path:
    """Convenience: render and write in one call. Returns the output path."""
    html = render_viewer(spec, template_path=template_path)
    out_path.write_bytes(html.encode("utf-8"))
    return out_path


__all__ = [
    "ViewerRenderError",
    "default_template_path",
    "embed_json",
    "propagate_target_metadata",
    "render_viewer",
    "render_viewer_to_file",
    "validate_spec_choices",
]
