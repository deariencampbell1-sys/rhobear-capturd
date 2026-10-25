"""Tests for capturd.shots.quality — the honest-failure rule for captures.

A run that only screenshots the rested baseline is not a pass. These tests
cover the four cases that matter plus the shape tolerances.
"""

from __future__ import annotations

from capturd.shots.quality import (
    BASELINE_ONLY,
    CaptureQuality,
    NO_CAPTURES,
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
        '[BASELINE_ONLY] 1 file only produced a baseline screenshot: "only-rested.png". '
        '[NO_SUCCESSFUL_CAPTURE] 1 file produced no successful screenshots: "nothing.png". '
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
    assert (
        '[BASELINE_ONLY] 2 files only produced a baseline screenshot: '
        '"a.png", "b.png".' in plural_message
    )
    assert (
        '[NO_SUCCESSFUL_CAPTURE] 2 files produced no successful screenshots: '
        '"c.png", "d.png".' in plural_message
    )


def test_error_carries_the_reason_code() -> None:
    # D4: each reason sentence is prefixed with its own reason code.
    baseline = build_quality_error(
        summarize_capture_quality(
            _result(_file("rested.png", [_capture("baseline", True)]))
        )
    )
    assert baseline.startswith("[BASELINE_ONLY] ")
    assert "[BASELINE_ONLY] 1 file only produced a baseline screenshot" in baseline

    empty = build_quality_error(
        summarize_capture_quality(_result(_file("dead.png", [])))
    )
    assert empty.startswith("[NO_SUCCESSFUL_CAPTURE] ")
    assert "[NO_SUCCESSFUL_CAPTURE] 1 file produced no successful screenshots" in empty

    none = build_quality_error(summarize_capture_quality({}))
    assert none.startswith("[NO_CAPTURES] ")
    assert "[NO_CAPTURES]" in none
    assert "not a pass" in none


def test_non_mapping_result_is_tolerated() -> None:
    # A non-mapping result cannot be read: no crash, and never a silent
    # pass — it is the no-captures failure (D5).
    for result in (None, "results", 7, []):
        quality = summarize_capture_quality(result)  # type: ignore[arg-type]
        assert quality.verdict == NO_CAPTURES
        assert is_passing(result) is False  # type: ignore[arg-type]
        message = build_quality_error(quality)
        assert message.startswith("[NO_CAPTURES] ")


def test_non_mapping_capture_entry_is_tolerated() -> None:
    # A captures entry that is not a mapping cannot be read: never a
    # success, never a crash (D5).
    junk_only = {
        "results": [
            {"file": "junk.png", "manifest": {"captures": [42, None, "ok"]}}
        ]
    }
    quality = summarize_capture_quality(junk_only)
    assert quality.empty_files == ["junk.png"]
    assert quality.baseline_only_files == []
    assert quality.verdict == "FAIL"
    assert is_passing(junk_only) is False

    junk_plus_real = {
        "results": [
            {
                "file": "mixed.png",
                "manifest": {
                    "captures": [42, {"kind": "modal", "success": True}]
                },
            }
        ]
    }
    assert summarize_capture_quality(junk_plus_real).verdict == PASS
    assert is_passing(junk_plus_real) is True


def test_empty_filename_fallback_is_visible() -> None:
    # An entry with no ``file`` key is reported as "" rather than crashing,
    # and the "" fallback is visible in the error text (D5).
    result = {
        "results": [
            {"manifest": {"captures": [{"kind": "modal", "success": False}]}}
        ]
    }
    quality = summarize_capture_quality(result)
    assert quality.empty_files == [""]
    message = build_quality_error(quality)
    assert message.startswith("[NO_SUCCESSFUL_CAPTURE] ")
    assert '""' in message
    assert is_passing(result) is False


def test_empty_results_is_not_a_pass() -> None:
    # A run whose results collection is empty landed nothing: that is a
    # failure with its own reason code, never a pass (D1).
    quality = summarize_capture_quality({})
    assert quality.verdict == NO_CAPTURES
    assert quality.reason_codes == [NO_CAPTURES]
    assert is_passing({}) is False

    empty_list = summarize_capture_quality({"results": []})
    assert empty_list.verdict == NO_CAPTURES
    assert is_passing({"results": []}) is False


def test_results_key_absent_is_not_a_pass() -> None:
    # {} — a result with no ``results`` key at all: NO_CAPTURES (D1).
    quality = summarize_capture_quality({})
    assert quality.no_captures is True
    assert quality.baseline_only_files == []
    assert quality.empty_files == []
    assert classify_result({}) == (NO_CAPTURES, [NO_CAPTURES])
    assert is_passing({}) is False


def test_results_that_is_not_a_list_is_not_a_pass() -> None:
    # A truthy non-list ``results`` (string, dict, number) is treated as
    # no captures — same NO_CAPTURES failure, never a silent pass (D3).
    for value in ("ab", {"file": "page-01.png"}, 7, True, None, ""):
        result = {"results": value}
        quality = summarize_capture_quality(result)  # type: ignore[arg-type]
        assert quality.verdict == NO_CAPTURES
        assert quality.reason_codes == [NO_CAPTURES]
        assert is_passing(result) is False  # type: ignore[arg-type]


def test_duplicate_filenames_are_reported_once() -> None:
    # D6: file names are deduplicated, first-seen order preserved — one
    # file with two failing entries is reported once (deliberate
    # improvement over upstream, which keys on entries).
    twice_baseline = _result(
        _file("same.png", [_capture("baseline", True)]),
        _file("other.png", [_capture("baseline", True)]),
        _file("same.png", [_capture("baseline", True)]),
    )
    quality = summarize_capture_quality(twice_baseline)
    assert quality.baseline_only_files == ["same.png", "other.png"]
    assert quality.reason_codes == [BASELINE_ONLY]
    message = build_quality_error(quality)
    assert (
        '[BASELINE_ONLY] 2 files only produced a baseline screenshot: '
        '"same.png", "other.png".' in message
    )

    twice_empty = _result(_file("dead.png", []), _file("dead.png", []))
    q2 = summarize_capture_quality(twice_empty)
    assert q2.empty_files == ["dead.png"]
    assert build_quality_error(q2).startswith(
        '[NO_SUCCESSFUL_CAPTURE] 1 file produced no successful screenshots: '
        '"dead.png".'
    )

    # Same file, one baseline-only entry and one empty entry: reported
    # once, in first-seen order.
    mixed_dup = _result(
        _file("dup.png", [_capture("baseline", True)]),
        _file("dup.png", []),
    )
    q3 = summarize_capture_quality(mixed_dup)
    assert q3.baseline_only_files == ["dup.png"]
    assert q3.empty_files == []


def test_string_success_is_not_a_success() -> None:
    # D7: capture.get("success") is True — a truthy string is not a
    # success (edge case: {"success": "false"}).
    result = {
        "results": [
            {
                "file": "lying.png",
                "manifest": {"captures": [{"kind": "modal", "success": "false"}]},
            }
        ]
    }
    quality = summarize_capture_quality(result)
    assert quality.empty_files == ["lying.png"]
    assert quality.baseline_only_files == []
    assert quality.reason_codes == [NO_SUCCESSFUL_CAPTURE]
    assert is_passing(result) is False
    assert classify_result(result) == ("FAIL", [NO_SUCCESSFUL_CAPTURE])


def test_is_passing_reads_the_verdict() -> None:
    # D8: is_passing consults quality.verdict == PASS — one source of
    # truth, never a second recomputation of the lists.
    import capturd.shots.quality as quality_module

    cases = [
        {},
        {"results": []},
        _result(_file("rested.png", [_capture("baseline", True)])),
        _result(_file("dead.png", [_capture("modal", False)])),
        _result(_file("page.png", [_capture("baseline", True), _capture("modal", True)])),
    ]
    for result in cases:
        quality = summarize_capture_quality(result)
        assert is_passing(result) is (quality.verdict == PASS)

    # A verdict that does not match the lists still decides the predicate.
    sentinel = CaptureQuality(no_captures=True)
    monkeypatched = lambda result: sentinel
    original = quality_module.summarize_capture_quality
    quality_module.summarize_capture_quality = monkeypatched  # type: ignore[assignment]
    try:
        assert sentinel.verdict == NO_CAPTURES
        assert sentinel.baseline_only_files == [] and sentinel.empty_files == []
        assert is_passing({}) is False
        passing = CaptureQuality()
        quality_module.summarize_capture_quality = lambda result: passing  # type: ignore[assignment]
        assert is_passing({}) is True
    finally:
        quality_module.summarize_capture_quality = original  # type: ignore[assignment]


def test_passing_summary_has_no_error_text() -> None:
    # D9: a passing summary produces "", so no caller can print a false
    # "this run is not a pass".
    passing_result = _result(
        _file("page.png", [_capture("baseline", True), _capture("modal", True)])
    )
    quality = summarize_capture_quality(passing_result)
    assert quality.verdict == PASS
    assert build_quality_error(quality) == ""
    assert is_passing(passing_result) is True


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
