"""Tests for capturd.shots.quality — the honest-failure rule for captures.

A run that only screenshots the rested baseline is not a pass. These tests
cover the four cases that matter plus the shape tolerances.
"""

from __future__ import annotations

import builtins
import copy
import io
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# D5: this suite must run under any invocation directory — `cd tests &&
# pytest`, an editor runner, xdist — not only when pytest starts at the
# repo root (where `python -m` puts cwd on sys.path). Anchor the import
# on this file's own location; a no-op when the root is already there.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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


# ---------------------------------------------------------------------------
# Read-only contract (W0-15): the module docstring promises, in as many
# words, "Read-only: nothing here mutates ``result`` or the filesystem."
# The tests below prove that promise for the whole public surface —
# summarize_capture_quality, build_quality_error, reason_codes, is_passing.
# A refactor that mutated the input in place would pass every test above
# and break every downstream caller that reuses the dict —
# the deepcopy test below is the guard against exactly that (locked
# D1), and the write recorder (D4) guards the filesystem half: a write
# is caught by intercepting it, never by listing a directory.
# ---------------------------------------------------------------------------


def _mixed_result() -> dict[str, object]:
    """A result mixing all four entry shapes (edge case 1): a passing
    capture, a baseline-only capture, a failing capture with a name, and
    an entry that is not a mapping."""
    return _result(
        _file("good.png", [_capture("baseline", True), _capture("modal", True)]),
        _file("rested.png", [_capture("baseline", True)]),
        _file("dead.png", [_capture("modal", False)]),
        42,  # not a mapping: tolerated, never read, never written
    )


def test_summary_does_not_mutate_its_input() -> None:
    # D1: deep-copy the mixed result, run every public call over it, and
    # assert the *input* is byte-for-byte what it was — assertEqual on the
    # deep copy, not on the returned summary. If this ever goes red, the
    # code is the bug: fix the mutation (copy on entry), never weaken this
    # assertion.
    result = _mixed_result()
    snapshot = copy.deepcopy(result)

    quality = summarize_capture_quality(result)
    message = build_quality_error(quality)
    codes = quality.reason_codes
    passing = is_passing(result)

    # The contract, checked on the input itself.
    assert result == snapshot

    # Non-vacuous guard: the calls above really did compute the expected
    # summary, so the equality is not passing because nothing happened.
    assert quality.baseline_only_files == ["rested.png"]
    assert quality.empty_files == ["dead.png"]
    assert codes == [BASELINE_ONLY, NO_SUCCESSFUL_CAPTURE]
    assert quality.verdict == FAIL
    assert message.startswith("[BASELINE_ONLY] 1 file only produced")
    assert passing is False

    # Edge case 2: a caller mutating the ``captures`` list *after* the
    # call must not reach back into the already-computed summary — the
    # summary is its own data, not a view onto the input.
    result["results"][0]["manifest"]["captures"].clear()
    result["results"][2]["manifest"]["captures"].append(_capture("modal", True))
    assert quality.baseline_only_files == ["rested.png"]
    assert quality.empty_files == ["dead.png"]
    assert quality.reason_codes == codes
    assert quality.verdict == FAIL
    assert build_quality_error(quality) == message
    # is_passing re-reads the (now mutated) input without writing to it:
    # the passing file lost its captures, so the run is still not a pass.
    assert is_passing(result) is False


# --- D4: the write recorder — a write is caught by intercepting it, ------
# never by listing a directory. A listing diff cannot see a sidecar in a
# subdirectory, a file in /tmp, or a content change to an existing file;
# a recorder installed before the call sees every write-mode open.

_WRITE_OPEN_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND


def _is_write_mode(mode: object) -> bool:
    """A mode opens for writing when it carries ``w``, ``a``, ``x`` or
    ``+`` — the read D4 names. ``r+`` is write-capable via ``+`` and is
    correctly recorded; plain ``r`` / ``rb`` are not."""
    return isinstance(mode, str) and any(flag in mode for flag in "wax+")


@pytest.fixture
def write_recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """D4: install the write recorder before the call; teardown runs under
    try/finally (``monkeypatch.undo()``), so a failing assertion still
    unpatches.

    Recorded, per D4: ``builtins.open`` in a mode containing
    ``w``/``a``/``x``/``+`` (and its twin reference ``io.open``, which is
    what ``Path.open`` reaches through); ``os.open`` with any of
    ``O_WRONLY``/``O_RDWR``/``O_CREAT``/``O_APPEND`` in flags;
    ``Path.write_text``/``write_bytes``/``touch``/``mkdir``/``unlink``;
    ``json.dump``. Yields the live list of recorded entries — the proof
    is that it stays empty across the public calls (D6: the source grep
    in test_summary_writes_nothing_to_disk is only the tripwire).
    """
    recorded: list[str] = []

    real_open = builtins.open

    def recording_open(file: Any, mode: Any = "r", *args: Any, **kwargs: Any) -> Any:
        if _is_write_mode(mode):
            recorded.append(f"open({file}, mode={mode!r})")
        return real_open(file, mode, *args, **kwargs)

    real_os_open = os.open

    def recording_os_open(path: Any, flags: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(flags, int) and flags & _WRITE_OPEN_FLAGS:
            recorded.append(f"os.open({path}, flags={flags!r})")
        return real_os_open(path, flags, *args, **kwargs)

    real_json_dump = json.dump

    def recording_json_dump(obj: Any, fp: Any, *args: Any, **kwargs: Any) -> Any:
        recorded.append(f"json.dump({type(obj).__name__} -> {fp})")
        return real_json_dump(obj, fp, *args, **kwargs)

    def recording_path_method(name: str) -> Any:
        original = getattr(Path, name)

        def method(self: Path, *args: Any, **kwargs: Any) -> Any:
            recorded.append(f"Path.{name}({self})")
            return original(self, *args, **kwargs)

        return method

    try:
        monkeypatch.setattr(builtins, "open", recording_open)
        monkeypatch.setattr(io, "open", recording_open)
        monkeypatch.setattr(os, "open", recording_os_open)
        for name in ("write_text", "write_bytes", "touch", "mkdir", "unlink"):
            monkeypatch.setattr(Path, name, recording_path_method(name))
        monkeypatch.setattr(json, "dump", recording_json_dump)
        yield recorded
    finally:
        monkeypatch.undo()


def _exercise_public_surface(
    result: Any,
) -> tuple[CaptureQuality, str, str, list[str], bool]:
    """Run all four public functions once — summarize_capture_quality,
    build_quality_error, classify_result, is_passing — and return what
    they computed, so every recorder assertion can also assert the calls
    really happened (an empty recording list must never mean "nothing
    ran")."""
    quality = summarize_capture_quality(result)
    message = build_quality_error(quality)
    verdict, codes = classify_result(result)
    passing = is_passing(result)
    return quality, message, verdict, codes, passing


def test_write_recorder_catches_real_writes(
    tmp_path: Path, write_recorder: list[str]
) -> None:
    """The test-of-the-test (D4, edge cases 1 and 2): before the recorder
    is trusted to prove the ABSENCE of writes, prove it records the
    PRESENCE of one. A recorder installed in the wrong place would record
    nothing and make every read-only assertion vacuous — each primitive
    D4 names is fired once, right here, on a throwaway path in tmp_path.
    """
    throwaway = tmp_path / "throwaway.txt"

    # Edge case 1: open(path, "a") is a write-mode open and is caught.
    with open(throwaway, "a") as handle:
        handle.write("sidecar")
    assert any(
        entry.startswith("open(") and "mode='a'" in entry
        for entry in write_recorder
    ), write_recorder

    # Edge case 2: os.open(path, os.O_WRONLY) is caught.
    fd = os.open(throwaway, os.O_WRONLY)
    os.close(fd)
    assert any(entry.startswith("os.open(") for entry in write_recorder), (
        write_recorder
    )

    # The rest of D4's list, each proven to fire on a real write.
    (tmp_path / "cache.json").write_text("{}")
    assert any("Path.write_text" in entry for entry in write_recorder)
    (tmp_path / "blob.bin").write_bytes(b"\x00")
    assert any("Path.write_bytes" in entry for entry in write_recorder)
    (tmp_path / "marker").touch()
    assert any("Path.touch" in entry for entry in write_recorder)
    (tmp_path / "subdir").mkdir()
    assert any("Path.mkdir" in entry for entry in write_recorder)
    (tmp_path / "marker").unlink()
    assert any("Path.unlink" in entry for entry in write_recorder)
    with open(tmp_path / "dumped.json", "w") as handle:
        json.dump({}, handle)
    assert any(entry.startswith("json.dump(") for entry in write_recorder)

    # The recorder counts writes, not reads: no read-mode open is ever
    # recorded, so the proof below cannot be inflated by ordinary reads.
    with open(throwaway, "r") as handle:
        handle.read()
    assert not any("mode='r'" in entry for entry in write_recorder), write_recorder


def test_quality_module_never_opens_for_writing(
    write_recorder: list[str],
) -> None:
    """D4 (edge case 4) — the proof: all four public functions run under
    the installed write recorder and the recorded list stays empty, so a
    write-mode open anywhere in capturd/shots/quality.py turns this test
    red. Which is proof, which is tripwire (D6): THIS assertion is the
    proof; the source grep kept in test_summary_writes_nothing_to_disk is
    only the tripwire — a cheap second pair of eyes over primitives the
    recorder does not intercept (shutil, tempfile, os.makedirs).
    """
    result = _mixed_result()
    quality, message, verdict, codes, passing = _exercise_public_surface(result)

    # Non-vacuous: the four calls really computed the expected verdict,
    # so an empty recording list cannot mean "nothing ran".
    assert (verdict, codes) == (FAIL, [BASELINE_ONLY, NO_SUCCESSFUL_CAPTURE])
    assert quality.verdict == FAIL
    assert message.startswith("[BASELINE_ONLY] 1 file only produced")
    assert passing is False

    # Every branch of the module, also under the recorder: no-captures,
    # empty-only, a clean passing run, and the junk-tolerance shapes.
    for shape in (
        {},
        {"results": []},
        {"results": [42, None]},
        {"results": [{"file": "dead.png", "manifest": "junk"}]},
        _result(_file("rested.png", [_capture("baseline", True)])),
        _result(
            _file(
                "page.png",
                [_capture("baseline", True), _capture("modal", True)],
            )
        ),
    ):
        summarize_capture_quality(shape)
        build_quality_error(summarize_capture_quality(shape))
        classify_result(shape)
        is_passing(shape)

    assert write_recorder == []


def test_read_only_tests_do_not_depend_on_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_recorder: list[str]
) -> None:
    """D5 (edge case 3): the read-only proof runs from a directory that is
    not the repo root. W0-15-r1's listing test pinned Path.cwd() to the
    repo root and guarded on "capturd" being in the listing — it would
    fail under `cd tests && pytest` before asserting anything about
    writes. Here the cwd is moved to tmp_path first, so the run is
    deterministic wherever pytest was started from: the recorder must
    still be empty and the only listing kept is of tmp_path itself,
    before and after.
    """
    monkeypatch.chdir(tmp_path)
    assert Path.cwd().resolve() == tmp_path.resolve()
    before = sorted(os.listdir(tmp_path))
    # This cwd is not the repo root — the old "capturd" in listing guard
    # would have died right here. Nothing here assumes the repo layout.
    assert "capturd" not in before

    result = _mixed_result()
    quality, message, verdict, codes, passing = _exercise_public_surface(result)
    assert verdict == FAIL and passing is False  # the calls really ran
    for shape in ({}, {"results": []}):
        classify_result(shape)
        is_passing(shape)

    assert Path.cwd().resolve() == tmp_path.resolve()
    assert sorted(os.listdir(tmp_path)) == before
    assert write_recorder == []


def test_summary_writes_nothing_to_disk() -> None:
    # D2: nothing on disk moves. Record Path.cwd() and the sorted
    # os.listdir of the repo root before and after the four public calls;
    # both must come back identical — no temp files, no .json, no summary
    # sidecar (edge case 3: the working-directory listing itself).
    import re

    import capturd.shots.quality as quality_module

    # The module source itself carries no write calls — stated here as an
    # assertion, mirroring the acceptance grep, so a future edit that adds
    # Path.write_text / a write-mode open() fails here, not just in review:
    source = Path(quality_module.__file__).read_text(encoding="utf-8")
    assert re.search(r"write_text|open\([^)]*['\"][wa]", source) is None, (
        "capturd/shots/quality.py must contain no write calls"
    )
    # json.dumps renders text; json.dump(target) writes a file — only the
    # former is allowed in a read-only module.
    assert "json.dump(" not in source

    cwd = Path.cwd()
    before = sorted(os.listdir(cwd))
    # Guard: this really is the repo root listing, not an empty no-op.
    assert "capturd" in before and "tests" in before

    result = _mixed_result()
    quality = summarize_capture_quality(result)
    build_quality_error(quality)
    quality.reason_codes
    is_passing(result)

    assert Path.cwd() == cwd
    assert sorted(os.listdir(cwd)) == before
