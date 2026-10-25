"""Tests for capturd.shots.quality — the honest-failure rule for captures.

A run that only screenshots the rested baseline is not a pass. These tests
cover the four cases that matter plus the shape tolerances.
"""

from __future__ import annotations

from capturd.shots.quality import (
    BASELINE_ONLY,
    NO_SUCCESSFUL_CAPTURE,
    PASS,
    build_quality_error,
    classify_result,
    is_passing,
    summarize_capture_quality,
)


def _capture(kind: str | None, success: bool) -> dict[str, object]:
    capture: dict[str, object] = {"success": success}
    if kind is not None:
        capture["kind"] = kind
    return capture


def _result(*files: dict[str, object]) -> dict[str, object]:
    return {"results": list(files)}


def _file(
    name: str,
    captures: list[dict[str, object]] | None = None,
    *,
    with_manifest: bool = True,
    with_captures: bool = True,
) -> dict[str, object]:
    entry: dict[str, object] = {"file": name}
    if with_manifest:
        manifest: dict[str, object] = {}
        if with_captures:
            manifest["captures"] = captures or []
        entry["manifest"] = manifest
    return entry


def test_all_baseline_is_baseline_only_and_not_passing() -> None:
    result = _result(
        _file("rested.png", [_capture("baseline", True), _capture("baseline", True)]),
    )
    quality = summarize_capture_quality(result)
    assert quality.baseline_only_files == ["rested.png"]
    assert quality.empty_files == []
    assert quality.reason_codes == [BASELINE_ONLY]
    assert quality.verdict == "FAIL"
    assert is_passing(result) is False
    verdict, codes = classify_result(result)
    assert verdict == "FAIL"
    assert codes == [BASELINE_ONLY]


def test_no_successful_capture_is_empty_and_not_passing() -> None:
    result = _result(
        _file("dead.png", [_capture("modal", False), _capture("baseline", False)]),
    )
    quality = summarize_capture_quality(result)
    assert quality.baseline_only_files == []
    assert quality.empty_files == ["dead.png"]
    assert quality.reason_codes == [NO_SUCCESSFUL_CAPTURE]
    assert is_passing(result) is False
    assert classify_result(result) == ("FAIL", [NO_SUCCESSFUL_CAPTURE])


def test_baseline_plus_one_real_state_passes() -> None:
    result = _result(
        _file("page.png", [_capture("baseline", True), _capture("modal", True)]),
    )
    quality = summarize_capture_quality(result)
    assert quality.baseline_only_files == []
    assert quality.empty_files == []
    assert quality.reason_codes == [PASS]
    assert quality.verdict == PASS
    assert is_passing(result) is True
    assert classify_result(result) == (PASS, [PASS])


def test_mixed_two_files_names_both_counts_with_pluralisation() -> None:
    result = _result(
        _file("only-rested.png", [_capture("baseline", True)]),
        _file("nothing.png", [_capture("modal", False)]),
    )
    quality = summarize_capture_quality(result)
    assert quality.baseline_only_files == ["only-rested.png"]
    assert quality.empty_files == ["nothing.png"]
    assert quality.reason_codes == [BASELINE_ONLY, NO_SUCCESSFUL_CAPTURE]

    message = build_quality_error(quality)
    assert message == (
        "1 file only produced a baseline screenshot. "
        "1 file produced no successful screenshots. "
        "Captur'd did not capture meaningful UI states, so this run is not a pass."
    )

    plural = summarize_capture_quality(
        _result(
            _file("a.png", [_capture("baseline", True)]),
            _file("b.png", [_capture("baseline", True)]),
            _file("c.png", []),
            _file("d.png", []),
        )
    )
    plural_message = build_quality_error(plural)
    assert "2 files only produced a baseline screenshot." in plural_message
    assert "2 files produced no successful screenshots." in plural_message


def test_missing_manifest_is_empty() -> None:
    result = _result(_file("bare.png", with_manifest=False))
    quality = summarize_capture_quality(result)
    assert quality.empty_files == ["bare.png"]
    assert quality.baseline_only_files == []
    assert quality.reason_codes == [NO_SUCCESSFUL_CAPTURE]
    assert is_passing(result) is False


def test_missing_captures_is_empty() -> None:
    result = _result(_file("hollow.png", with_captures=False))
    quality = summarize_capture_quality(result)
    assert quality.empty_files == ["hollow.png"]
    assert quality.baseline_only_files == []
    assert is_passing(result) is False


def test_capture_without_kind_is_not_baseline() -> None:
    # A successful capture with no kind is treated as NOT baseline: never
    # invent a baseline, never invent a success.
    result = _result(_file("kindless.png", [_capture(None, True)]))
    quality = summarize_capture_quality(result)
    assert quality.baseline_only_files == []
    assert quality.empty_files == []
    assert quality.reason_codes == [PASS]
    assert is_passing(result) is True

    # An unsuccessful capture with no kind is simply not a success.
    failed = _result(_file("kindless-failed.png", [_capture(None, False)]))
    assert summarize_capture_quality(failed).empty_files == ["kindless-failed.png"]
    assert is_passing(failed) is False
