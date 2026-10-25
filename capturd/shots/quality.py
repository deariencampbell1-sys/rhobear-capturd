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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BASELINE_ONLY",
    "BASELINE_KIND",
    "FAIL",
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

# Verdict values returned by classify_result / CaptureQuality.verdict.
FAIL = "FAIL"

# The manifest kind that marks a rested-baseline screenshot.
BASELINE_KIND = "baseline"


@dataclass
class CaptureQuality:
    """Which files captured no evidence, and how they failed.

    ``baseline_only_files``: files whose successful captures are all
    rested-baseline screenshots. ``empty_files``: files with no successful
    capture at all. A run passes only when both lists are empty.
    """

    baseline_only_files: list[str] = field(default_factory=list)
    empty_files: list[str] = field(default_factory=list)

    @property
    def reason_codes(self) -> list[str]:
        """Named reason codes for this quality summary (fixed spellings)."""
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
        """``PASS`` only when neither list is populated, else ``FAIL``."""
        if not self.baseline_only_files and not self.empty_files:
            return PASS
        return FAIL


def _file_name(item: Mapping[str, Any]) -> str:
    name = item.get("file")
    return name if isinstance(name, str) else ""


def summarize_capture_quality(result: Mapping[str, Any]) -> CaptureQuality:
    """Split a capture result's files into baseline-only and empty lists.

    A file whose manifest has no successful capture is empty; a file whose
    successful captures are all ``kind == "baseline"`` is baseline-only.
    Read-only: nothing here mutates ``result`` or the filesystem.
    """
    baseline_only: list[str] = []
    empty: list[str] = []
    if not isinstance(result, Mapping):
        return CaptureQuality(baseline_only_files=baseline_only, empty_files=empty)

    for item in result.get("results") or []:
        if not isinstance(item, Mapping):
            continue
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

    return CaptureQuality(baseline_only_files=baseline_only, empty_files=empty)


def classify_result(result: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Map a capture result to ``(verdict, reason_codes)`` in one step."""
    quality = summarize_capture_quality(result)
    return quality.verdict, quality.reason_codes


def build_quality_error(quality: CaptureQuality) -> str:
    """Human sentence naming the counts, then stating the run is not a pass."""
    parts: list[str] = []
    baseline_count = len(quality.baseline_only_files)
    if baseline_count:
        plural = "" if baseline_count == 1 else "s"
        parts.append(
            f"{baseline_count} file{plural} only produced a baseline screenshot."
        )
    empty_count = len(quality.empty_files)
    if empty_count:
        plural = "" if empty_count == 1 else "s"
        parts.append(
            f"{empty_count} file{plural} produced no successful screenshots."
        )
    parts.append("Captur'd did not capture meaningful UI states, so this run is not a pass.")
    return " ".join(parts)


def is_passing(result: Mapping[str, Any]) -> bool:
    """True only when no file is baseline-only and no file is empty."""
    quality = summarize_capture_quality(result)
    return not quality.baseline_only_files and not quality.empty_files
