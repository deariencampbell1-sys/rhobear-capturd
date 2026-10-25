"""Capture-quality summary for stills mode: name runs that captured no evidence.

The honest-failure rule: a run that only screenshots the rested baseline is
not a pass. A page where nothing meaningful opened can still produce files
and a green-looking manifest, so this module reads the capture result and
names that state loudly instead of letting it masquerade as success.

This module only reads. It never adds, repairs, retries or deletes captures,
and it never touches the filesystem.

Result shape accepted (the shape that exists today)::

    result = {
        "results": [
            {
                "file": "page-01.png",
                "manifest": {"captures": [{"kind": "baseline", "success": True}]},
            },
            ...
        ]
    }

Tolerances (each documented here, per the lane rule):
- a result without ``results`` (or a non-mapping result) means no files;
- an entry without ``manifest`` has no captures, so it is empty;
- a ``manifest`` without ``captures`` (or with a non-list ``captures``)
  has no captures, so it is empty;
- a captures entry without ``kind`` is treated as NOT baseline — a missing
  kind is never invented into a baseline, and a missing success is never
  invented into a success;
- a captures entry that is not a mapping cannot be read, so it is not a
  successful capture;
- an entry without a usable ``file`` string is still counted, recorded as
  ``""`` rather than given an invented name.

Verdicts: ``PASS`` when meaningful states landed, ``FAIL`` when files
failed, ``NO_CAPTURES`` when nothing landed at all. An empty or absent
``results`` collection is ``NO_CAPTURES`` — a deliberate divergence from
upstream ``isPassingResult``, which has the same hole and passes on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BASELINE_ONLY",
    "BASELINE_KIND",
    "FAIL",
    "NO_CAPTURES",
    "NO_SUCCESSFUL_CAPTURE",
    "PASS",
    "CaptureQuality",
    "build_quality_error",
    "classify_result",
    "is_passing",
    "summarize_capture_quality",
]

# Reason codes: fixed spelling, consumed by later briefs. This lane has
# already been burned by two spellings of one status — do not re-spell these.
PASS = "PASS"
BASELINE_ONLY = "BASELINE_ONLY"
NO_SUCCESSFUL_CAPTURE = "NO_SUCCESSFUL_CAPTURE"
# Nothing landed at all: empty or unreadable results collection (D1).
NO_CAPTURES = "NO_CAPTURES"

# Verdict values returned by classify_result / CaptureQuality.verdict.
FAIL = "FAIL"

# The manifest kind that marks a rested-baseline screenshot.
BASELINE_KIND = "baseline"


@dataclass
class CaptureQuality:
    """Which files captured no evidence, and how they failed.

    ``baseline_only_files``: files whose successful captures are all
    rested-baseline screenshots. ``empty_files``: files with no successful
    capture at all. ``no_captures``: the run landed no readable files at
    all. A run passes only when none of the three is populated.
    """

    baseline_only_files: list[str] = field(default_factory=list)
    empty_files: list[str] = field(default_factory=list)
    no_captures: bool = False

    @property
    def reason_codes(self) -> list[str]:
        """Named reason codes for this quality summary (fixed spellings)."""
        if self.no_captures:
            return [NO_CAPTURES]
        codes: list[str] = []
        if self.baseline_only_files:
            codes.append(BASELINE_ONLY)
        if self.empty_files:
            codes.append(NO_SUCCESSFUL_CAPTURE)
        if not codes:
            codes.append(PASS)
        return codes

    @property
    def verdict(self) -> str:
        """``NO_CAPTURES`` when nothing landed, ``PASS`` only when no file
        is baseline-only and no file is empty, else ``FAIL``."""
        if self.no_captures:
            return NO_CAPTURES
        if not self.baseline_only_files and not self.empty_files:
            return PASS
        return FAIL


def _file_name(item: Mapping[str, Any]) -> str:
    name = item.get("file")
    return name if isinstance(name, str) else ""


def summarize_capture_quality(result: Any) -> CaptureQuality:
    """Split a capture result's files into baseline-only and empty lists.

    A file whose manifest has no successful capture is empty; a file whose
    successful captures are all ``kind == "baseline"`` is baseline-only;
    a result whose ``results`` is absent, empty or not a list reports
    ``no_captures`` (``NO_CAPTURES``). Read-only: nothing here mutates
    ``result`` or the filesystem.
    """
    if not isinstance(result, Mapping):
        return CaptureQuality(no_captures=True)
    results = result.get("results")
    if not isinstance(results, list):
        # Absent, empty-shaped or truthy non-list (string, dict, number):
        # no captures, never a silent pass (D1/D3).
        return CaptureQuality(no_captures=True)

    baseline_only: list[str] = []
    empty: list[str] = []
    read_any = False
    for item in results:
        if not isinstance(item, Mapping):
            continue
        read_any = True
        manifest = item.get("manifest")
        captures: list[Any] = []
        if isinstance(manifest, Mapping) and isinstance(manifest.get("captures"), list):
            captures = manifest["captures"]
        successful = [
            capture
            for capture in captures
            if isinstance(capture, Mapping) and capture.get("success")
        ]
        useful = [
            capture
            for capture in successful
            if capture.get("kind") != BASELINE_KIND
        ]
        if not successful:
            empty.append(_file_name(item))
        elif not useful:
            baseline_only.append(_file_name(item))

    if not read_any:
        # A results list of unreadable entries means nothing landed.
        return CaptureQuality(no_captures=True)
    return CaptureQuality(baseline_only_files=baseline_only, empty_files=empty)


def classify_result(result: Any) -> tuple[str, list[str]]:
    """Map a capture result to ``(verdict, reason_codes)`` in one step."""
    quality = summarize_capture_quality(result)
    return quality.verdict, quality.reason_codes


def _named(files: list[str]) -> str:
    """Render file names for the error text, ``""`` fallback included."""
    return ", ".join(f'"{name}"' for name in files)


def build_quality_error(quality: CaptureQuality) -> str:
    """One sentence per failing reason, prefixed with its own reason code
    (D4), naming the files behind it; never a bare "failed"."""
    parts: list[str] = []
    baseline_count = len(quality.baseline_only_files)
    if baseline_count:
        plural = "" if baseline_count == 1 else "s"
        parts.append(
            f"[{BASELINE_ONLY}] {baseline_count} file{plural} only produced a "
            f"baseline screenshot: {_named(quality.baseline_only_files)}."
        )
    empty_count = len(quality.empty_files)
    if empty_count:
        plural = "" if empty_count == 1 else "s"
        parts.append(
            f"[{NO_SUCCESSFUL_CAPTURE}] {empty_count} file{plural} produced no "
            f"successful screenshots: {_named(quality.empty_files)}."
        )
    if quality.no_captures:
        parts.append(f"[{NO_CAPTURES}] The run produced no capture files at all.")
    parts.append("Captur'd did not capture meaningful UI states, so this run is not a pass.")
    return " ".join(parts)


def is_passing(result: Any) -> bool:
    """True only when the verdict is ``PASS`` — one source of truth (D8)."""
    quality = summarize_capture_quality(result)
    return quality.verdict == PASS
