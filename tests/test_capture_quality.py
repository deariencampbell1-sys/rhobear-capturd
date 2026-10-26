"""Tests for capturd.shots.quality — the honest-failure rule for captures.

A run that only screenshots the rested baseline is not a pass. These tests
cover the four cases that matter plus the shape tolerances.
"""

from __future__ import annotations

import pytest

from capturd.shots.quality import (
    BASELINE_ONLY,
    CaptureQuality,
    FAIL,
    NO_CAPTURES,
    NO_SUCCESSFUL_CAPTURE,
    PASS,
    VERDICTS,
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
    assert quality.verdict == FAIL
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
    assert classify_result(result) == (FAIL, [NO_SUCCESSFUL_CAPTURE])


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
    assert quality.verdict == FAIL
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
    assert classify_result(result) == (FAIL, [NO_SUCCESSFUL_CAPTURE])


def test_is_passing_reads_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    # D8: is_passing consults quality.verdict == PASS — one source of
    # truth, never a second recomputation of the lists. Patching goes
    # through the monkeypatch fixture, so restoration is pytest's job.
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
    assert sentinel.verdict == NO_CAPTURES
    assert sentinel.baseline_only_files == [] and sentinel.empty_files == []
    monkeypatch.setattr(
        quality_module, "summarize_capture_quality", lambda result: sentinel
    )
    assert is_passing({}) is False
    monkeypatch.setattr(
        quality_module,
        "summarize_capture_quality",
        lambda result: CaptureQuality(),
    )
    assert is_passing({}) is True


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


def test_capture_without_kind_is_not_meaningful() -> None:
    # D1: this is the deliberate reversal of the old
    # test_capture_without_kind_is_not_baseline, which locked in the
    # false green. A successful capture with no kind is NOT provably
    # meaningful evidence: the file is baseline-only, the verdict FAIL,
    # the reason BASELINE_ONLY — never PASS.
    result = _result(_file("kindless.png", [_capture(None, True)]))
    quality = summarize_capture_quality(result)
    assert quality.baseline_only_files == ["kindless.png"]
    assert quality.empty_files == []
    assert quality.reason_codes == [BASELINE_ONLY]
    assert quality.verdict == FAIL
    assert is_passing(result) is False
    assert classify_result(result) == (FAIL, [BASELINE_ONLY])

    # Edge case: kind: "" is the same as an absent kind — not evidence.
    empty_kind = _result(_file("empty-kind.png", [_capture("", True)]))
    q2 = summarize_capture_quality(empty_kind)
    assert q2.baseline_only_files == ["empty-kind.png"]
    assert q2.empty_files == []
    assert q2.reason_codes == [BASELINE_ONLY]
    assert q2.verdict == FAIL
    assert is_passing(empty_kind) is False

    # An unsuccessful capture with no kind is simply not a success.
    failed = _result(_file("kindless-failed.png", [_capture(None, False)]))
    assert summarize_capture_quality(failed).empty_files == ["kindless-failed.png"]
    assert is_passing(failed) is False


def test_non_mapping_results_entries_are_no_captures() -> None:
    # D4: a results list made only of unreadable entries landed nothing —
    # NO_CAPTURES, not a silent pass and not empty_files.
    result = {"results": [42, None]}
    quality = summarize_capture_quality(result)
    assert quality.verdict == NO_CAPTURES
    assert quality.reason_codes == [NO_CAPTURES]
    assert quality.no_captures is True
    assert quality.empty_files == []
    assert quality.baseline_only_files == []
    assert is_passing(result) is False
    assert classify_result(result) == (NO_CAPTURES, [NO_CAPTURES])
    assert build_quality_error(quality).startswith("[NO_CAPTURES] ")


def test_non_mapping_manifest_is_empty() -> None:
    # D5: a manifest that is not a mapping cannot be read — the file is
    # empty, no crash ({"results": [{"file": "a.png", "manifest": "junk"}]}).
    result = {"results": [{"file": "a.png", "manifest": "junk"}]}
    quality = summarize_capture_quality(result)
    assert quality.empty_files == ["a.png"]
    assert quality.baseline_only_files == []
    assert quality.verdict == FAIL
    assert is_passing(result) is False


def test_non_list_captures_is_empty() -> None:
    # D5: a captures value that is not a list cannot be read — the file is
    # empty, no crash ({"captures": {"kind": "modal"}}).
    result = {
        "results": [
            {"file": "a.png", "manifest": {"captures": {"kind": "modal"}}}
        ]
    }
    quality = summarize_capture_quality(result)
    assert quality.empty_files == ["a.png"]
    assert quality.baseline_only_files == []
    assert quality.verdict == FAIL
    assert is_passing(result) is False


def test_nameless_entries_are_not_deduplicated() -> None:
    # D7: an empty file name is never deduplicated — two nameless failing
    # entries are reported twice, so the count in the error does not
    # understate the damage.
    nameless = {
        "manifest": {"captures": [{"kind": "modal", "success": False}]}
    }
    result = {"results": [dict(nameless), dict(nameless)]}
    quality = summarize_capture_quality(result)
    assert quality.empty_files == ["", ""]
    message = build_quality_error(quality)
    assert (
        '[NO_SUCCESSFUL_CAPTURE] 2 files produced no successful screenshots: '
        '"", "".' in message
    )
    assert is_passing(result) is False


def test_empty_first_ordering_is_stable() -> None:
    # D8: bucket ordering is first-seen-wins — a file first seen as empty
    # stays in empty_files even when a later entry would make it
    # baseline-only.
    result = _result(
        _file("dup.png", []),
        _file("dup.png", [_capture("baseline", True)]),
    )
    quality = summarize_capture_quality(result)
    assert quality.empty_files == ["dup.png"]
    assert quality.baseline_only_files == []
    assert quality.reason_codes == [NO_SUCCESSFUL_CAPTURE]
    assert quality.verdict == FAIL
    assert is_passing(result) is False


def test_verdict_set_is_closed() -> None:
    # D2: the verdict vocabulary is closed — every verdict a summary can
    # produce is in VERDICTS, and only PASS is a pass.
    assert set(VERDICTS) == {PASS, FAIL, NO_CAPTURES}
    assert NO_CAPTURES != PASS
    assert FAIL != PASS
    samples = [
        {},
        {"results": []},
        {"results": [42, None]},
        _result(_file("rested.png", [_capture("baseline", True)])),
        _result(_file("dead.png", [])),
        _result(_file("page.png", [_capture("baseline", True), _capture("modal", True)])),
    ]
    for sample in samples:
        verdict, _codes = classify_result(sample)
        assert verdict in VERDICTS
        assert summarize_capture_quality(sample).verdict in VERDICTS


def test_reason_codes_constant_lists_four() -> None:
    # D2: REASON_CODES is the named four-tuple of reason codes, so nobody
    # iterates VERDICTS expecting reason codes. Imported here so the red
    # commit still collects: REASON_CODES does not exist until D2 lands.
    from capturd.shots.quality import REASON_CODES

    assert REASON_CODES == (PASS, BASELINE_ONLY, NO_SUCCESSFUL_CAPTURE, NO_CAPTURES)
    assert len(REASON_CODES) == 4
    assert set(REASON_CODES) == {
        PASS,
        BASELINE_ONLY,
        NO_SUCCESSFUL_CAPTURE,
        NO_CAPTURES,
    }

    # Edge case 5: VERDICTS iterated gives the three verdicts, and
    # BASELINE_ONLY — a reason code — is not one of them.
    assert VERDICTS == (PASS, FAIL, NO_CAPTURES)
    assert len(VERDICTS) == 3
    assert BASELINE_ONLY not in VERDICTS

    # Every reason code the module can produce is a REASON_CODES member.
    samples = [
        {},
        {"results": []},
        _result(_file("rested.png", [_capture("baseline", True)])),
        _result(_file("kindless.png", [_capture(None, True)])),
        _result(_file("dead.png", [])),
        _result(_file("page.png", [_capture("baseline", True), _capture("modal", True)])),
    ]
    for sample in samples:
        _verdict, codes = classify_result(sample)
        for code in codes:
            assert code in REASON_CODES


def test_name_with_quote_and_newline_is_escaped() -> None:
    # D3: a hostile file name cannot break the error text or forge a
    # "[PASS]" line in a log: quotes and control characters are escaped
    # and the text stays one line whose only bracketed code is the real
    # reason-code prefix.
    import re

    hostile = 'x"\n[PASS] ok'
    result = _result(_file(hostile, [_capture("baseline", True)]))
    quality = summarize_capture_quality(result)
    # Stored raw in the summary, rendered escaped in the text.
    assert quality.baseline_only_files == [hostile]
    message = build_quality_error(quality)
    assert "\n" not in message  # the newline never survives into the text
    assert message.startswith(
        "[BASELINE_ONLY] 1 file only produced a baseline screenshot: "
    )
    assert '\\"' in message  # the quote is escaped, text stays well-formed
    assert "\\n[PASS] ok" in message  # two chars, not a line break
    line_codes = re.findall(r"(?m)^\[[A-Z_]+\]", message)
    assert line_codes == ["[BASELINE_ONLY]"]  # no forged [PASS] line
    assert message.endswith(
        "Captur'd did not capture meaningful UI states, so this run is not a pass."
    )


def test_error_text_and_reason_codes_agree_on_inconsistent_object() -> None:
    # D4: one derivation — build_quality_error reads reason_codes, so a
    # directly constructed inconsistent object reports the same code set
    # from both (edge case 4).
    import re

    quality = CaptureQuality(
        no_captures=True,
        baseline_only_files=["a.png"],
        empty_files=["b.png"],
    )
    codes = quality.reason_codes
    assert codes == [NO_CAPTURES]
    message = build_quality_error(quality)
    assert message.startswith("[NO_CAPTURES] ")
    bracketed = re.findall(
        r"\[(PASS|FAIL|NO_CAPTURES|BASELINE_ONLY|NO_SUCCESSFUL_CAPTURE)\]",
        message,
    )
    assert set(bracketed) == set(codes)  # both report exactly {NO_CAPTURES}
    assert "a.png" not in message  # the text follows the codes, not the lists
