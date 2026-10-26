"""W0-01d-r1 — capture wiring: the seam where the map becomes a plan.

A plan built from a pathway map must be the SAME shape as one built from a
URL — same settings keys, same CaptureTarget fields — so nothing downstream
branches on which input arrived.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from capturd.shots.capture import (
    CaptureTarget,
    build_capture_plan,
    build_quality_summary,
)
from capturd.shots.pathway_map import PathwayMapError, load_pathway_map
from capturd.shots import cli

FIXTURES = Path(__file__).parent / "fixtures" / "capture-foundations"
WIRING = Path(__file__).parent / "fixtures" / "capture-wiring"
TWO_CONTROL_HTML = (WIRING / "two-control-page.html").read_text(encoding="utf-8")


def _stub_loader(monkeypatch, html: str = TWO_CONTROL_HTML) -> None:
    monkeypatch.setattr("capturd.shots.pathway_plan._load_page_html", lambda url: html)


# ---------------------------------------------------------------------------
# 1. same shape, whichever input arrived
# ---------------------------------------------------------------------------


def test_map_plan_matches_url_plan_shape(monkeypatch):
    _stub_loader(monkeypatch)
    url_plan = build_capture_plan({
        "urls": ["example.com"],
        "viewports": ["desktop"],
        "schemes": ["light"],
    })
    map_plan = build_capture_plan({
        "manifest_path": str(FIXTURES / "single-pathway-manifest.md"),
        "base_url": "http://base.test",
        "viewports": ["desktop"],
        "schemes": ["light"],
    })

    url_urls, url_targets, url_settings = url_plan
    map_urls, map_targets, map_settings = map_plan

    assert isinstance(map_urls, list) and isinstance(map_targets, list)
    # the dict shape of the plan: same settings keys, no branching downstream
    assert set(url_settings) == set(map_settings)
    # same CaptureTarget fields — both plans hold CaptureTargets
    assert [f.name for f in dataclasses.fields(CaptureTarget)] == [
        f.name for f in dataclasses.fields(type(map_targets[0]))
    ]
    assert all(isinstance(target, CaptureTarget) for target in map_targets)
    assert url_targets and map_targets
    # and the per-target state key downstream reads is present in both
    assert all(target.state_id for target in url_targets + map_targets)


# ---------------------------------------------------------------------------
# 2. exactly one input
# ---------------------------------------------------------------------------


def test_exactly_one_input_is_enforced():
    # zero inputs and two inputs both raise the SAME named error
    # load_pathway_map uses — PathwayMapError, never a raw ValueError.
    with pytest.raises(PathwayMapError) as zero:
        build_capture_plan({"viewports": ["desktop"], "schemes": ["light"]})

    with pytest.raises(PathwayMapError) as two_url_map:
        build_capture_plan({
            "url": "example.com",
            "map_text": "{}",
            "base_url": "http://base.test",
        })

    with pytest.raises(PathwayMapError) as two_maps:
        build_capture_plan({
            "manifest_text": "# x",
            "map_text": "{}",
            "base_url": "http://base.test",
        })

    with pytest.raises(PathwayMapError) as loader_refusal:
        load_pathway_map(manifest_path="a.md", map_path="b.json")

    refusal_type = type(loader_refusal.value)
    assert type(zero.value) is refusal_type
    assert type(two_url_map.value) is refusal_type
    assert type(two_maps.value) is refusal_type
    assert "a pathway map is required" in str(zero.value)
    assert "not both" in str(two_url_map.value)
    assert "not both" in str(two_maps.value)


# ---------------------------------------------------------------------------
# Edge 2 — UNWIRED: planned AND flagged, with the plan saying why
# ---------------------------------------------------------------------------


def test_unwired_pathway_is_planned_and_flagged(monkeypatch):
    _stub_loader(monkeypatch, "<html><body></body></html>")
    _urls, targets, settings = build_capture_plan({
        "manifest_path": str(WIRING / "named-control-manifest.md"),
        "base_url": "http://base.test",
        "viewports": ["desktop"],
        "schemes": ["light"],
    })

    unwired = [t for t in targets if t.pathway_status == "UNWIRED"]
    assert unwired, "the UNWIRED pathway must be planned, not silently dropped"
    assert {t.pathway_id for t in unwired} == {"open-missing"}

    flags = settings["pathway_flags"]
    assert flags, "the plan must say why a pathway is flagged"
    by_id = {flag["pathway_id"]: flag for flag in flags}
    assert "open-missing" in by_id
    assert by_id["open-missing"]["flag"] == "UNWIRED"
    assert by_id["open-missing"]["reason"]


# ---------------------------------------------------------------------------
# 5. quality gate — the reason code reaches the caller unchanged
# ---------------------------------------------------------------------------


class _StubManager:
    def __init__(self, results: list[dict], status: str = "done") -> None:
        self._results = results
        self._status = status

    def start(self, payload: dict) -> dict:
        return {"job_id": "stub-job"}

    def get(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "status": self._status,
            "message": "Done",
            "completed": len(self._results),
            "total": len(self._results),
            "failed": sum(1 for r in self._results if r.get("status") != "ok"),
            "results": self._results,
            "zip_path": "",
            "output_dir": "",
        }


@pytest.mark.parametrize(
    ("results", "reason_code"),
    [
        # Edge 5: a capture that returns only the rested baseline.
        ([{"file": "001-home-desktop-light.png", "status": "ok", "states": []}], "BASELINE_ONLY"),
        ([{"file": "001-home-desktop-light.png", "status": "failed", "error": "boom"}], "NO_SUCCESSFUL_CAPTURE"),
    ],
)
def test_quality_reason_code_reaches_the_caller_unchanged(monkeypatch, capsys, results, reason_code):
    monkeypatch.setattr(cli, "RestedCaptureManager", lambda: _StubManager(results))
    exit_code = cli.main(["--urls", "example.com"])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code != 0, "a failing run must exit non-zero"
    # the reason code reaches the caller unchanged — no re-wording downstream
    assert f"quality: {reason_code}" in output


def test_quality_gate_passes_a_meaningful_run(monkeypatch, capsys):
    results = [{
        "file": "001-home-desktop-light.png",
        "status": "ok",
        "trigger_fired": True,
        "states": [],
    }]
    monkeypatch.setattr(cli, "RestedCaptureManager", lambda: _StubManager(results))
    exit_code = cli.main(["--urls", "example.com"])
    assert exit_code == 0


def test_build_quality_summary_reads_run_results():
    summary = build_quality_summary([
        {"file": "a.png", "status": "ok", "states": []},
        {"file": "b.png", "status": "failed", "error": "x", "states": []},
    ])
    assert set(summary) == {"results"}
    files = [entry["file"] for entry in summary["results"]]
    assert files == ["a.png", "b.png"]
    kinds = [
        capture["kind"]
        for entry in summary["results"]
        for capture in entry["manifest"]["captures"]
    ]
    assert kinds[0] == "baseline"


# ---------------------------------------------------------------------------
# 6. CLI: a pasted map and a file map are identical
# ---------------------------------------------------------------------------


def _plan_json_line(capsys) -> str:
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.startswith("plan-json: ")]
    assert lines, f"no plan-json line in output:\n{captured.out}"
    return lines[0]


def test_cli_accepts_a_pasted_map_and_a_file_map_identically(monkeypatch, capsys):
    _stub_loader(monkeypatch)
    map_path = FIXTURES / "sample-verifier-map.json"
    pasted = map_path.read_text(encoding="utf-8")

    code_pasted = cli.main([
        "--map-text", pasted,
        "--base-url", "http://base.test",
        "--dry-run",
    ])
    line_pasted = _plan_json_line(capsys)

    code_file = cli.main([
        "--pathway-map", str(map_path),
        "--base-url", "http://base.test",
        "--dry-run",
    ])
    line_file = _plan_json_line(capsys)

    assert code_pasted == 0 and code_file == 0
    assert line_pasted == line_file
    plan = json.loads(line_pasted[len("plan-json: "):])
    assert plan["targets"], "the plan must carry targets"
    # D92: dry-run binds nothing. This map names its controls only by the
    # calls they make, so they are all reported unbound (selector=-) with a
    # flag — the pasted and file flavours say so identically.
    unbound = {
        flag["pathway_id"]
        for flag in plan["settings"]["pathway_flags"]
        if flag["flag"] == "UNBOUND"
    }
    assert unbound == {target["pathway_id"] for target in plan["targets"]}


def test_cli_map_input_needs_a_base_url(monkeypatch, capsys):
    exit_code = cli.main(["--pathway-map", str(FIXTURES / "sample-verifier-map.json")])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "base_url" in captured.err or "base-url" in captured.err


def test_cli_help_names_the_map_inputs(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    help_text = capsys.readouterr().out
    for flag, one_liner in (
        ("--pathway-map", "verifier JSON"),
        ("--pathway-manifest", "markdown"),
        ("--map-text", "pasted"),
    ):
        assert flag in help_text
        assert one_liner in help_text


# ---------------------------------------------------------------------------
# W0-28-r1 — D92: --dry-run is dry (no navigation, no network call)
# ---------------------------------------------------------------------------


def _forbid_page_reads(monkeypatch) -> list[str]:
    """The page-reading seam, recorded and made fatal: a call is proof that
    something navigated."""
    calls: list[str] = []

    def _record(url: str) -> str:
        calls.append(url)
        raise AssertionError(f"a page read happened for {url}")

    monkeypatch.setattr("capturd.shots.pathway_plan._load_page_html", _record)
    return calls


def _plan_from_output(out: str) -> dict:
    lines = [line for line in out.splitlines() if line.startswith("plan-json: ")]
    assert lines, f"no plan-json line in output:\n{out}"
    return json.loads(lines[0][len("plan-json: "):])


def _verifier_map_text(findings: list[dict]) -> str:
    return json.dumps({
        "checks": {
            "smoke-check": {
                "name": "smoke-check",
                "passed": False,
                "findings": findings,
            }
        }
    })


def _finding(fid: str, code: str) -> dict:
    return {
        "id": fid,
        "check": "smoke-check",
        "severity": "high",
        "code": code,
        "message": f"{fid} finding",
    }


def test_dry_run_performs_no_navigation(monkeypatch, capsys):
    # D92: in dry-run the planner prints the plan without launching a browser
    # and without any network call: a target's trigger_selector comes only
    # from what the map itself names (selector/css column, or a selector-shaped
    # token in trigger), and a named control that would need the page to bind
    # is reported as unbound (selector=-) with a flag saying so.
    calls = _forbid_page_reads(monkeypatch)
    map_text = _verifier_map_text([
        _finding("named-selector", "click `#panel-btn`"),
        _finding("named-by-label", "click Settings button"),
    ])

    exit_code = cli.main([
        "--map-text", map_text,
        "--base-url", "http://base.test",
        "--viewports", "desktop", "--schemes", "light",
        "--dry-run",
    ])
    captured = capsys.readouterr()
    out = captured.out + captured.err

    assert exit_code == 0
    assert calls == [], "dry-run must perform no navigation and no network call"
    plan = _plan_from_output(out)
    selectors = {t["pathway_id"]: t["trigger_selector"] for t in plan["targets"]}
    # the printed plan names the explicit selector the map carries...
    assert selectors["named-selector"] == "#panel-btn"
    # ...and marks the label-named control unbound, flagged
    assert selectors["named-by-label"] == ""
    assert "selector=-" in out
    unbound = [f for f in plan["settings"]["pathway_flags"] if f["flag"] == "UNBOUND"]
    assert [f["pathway_id"] for f in unbound] == ["named-by-label"]
    assert "unbound" in unbound[0]["reason"].lower()


def test_dry_run_carries_explicit_selectors_without_a_page_read(monkeypatch, capsys):
    # Edge 1: a map naming explicit selectors — plan printed, selectors
    # present, NO page read.
    calls = _forbid_page_reads(monkeypatch)
    exit_code = cli.main([
        "--pathway-manifest", str(WIRING / "named-control-manifest.md"),
        "--base-url", "http://base.test",
        "--viewports", "desktop", "--schemes", "light",
        "--dry-run",
    ])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == [], "dry-run must not read any page"
    plan = _plan_from_output(captured.out)
    selectors = {t["pathway_id"]: t["trigger_selector"] for t in plan["targets"]}
    assert selectors["open-settings"] == "#open-settings"
    assert selectors["open-missing"] == "#missing-btn"


def test_dry_run_flags_a_label_only_control_unbound(monkeypatch, capsys):
    # Edge 2: a map naming a control only by label — plan printed,
    # selector=-, flagged unbound.
    calls = _forbid_page_reads(monkeypatch)
    map_text = _verifier_map_text([_finding("label-only", "click Settings button")])

    exit_code = cli.main([
        "--map-text", map_text,
        "--base-url", "http://base.test",
        "--viewports", "desktop", "--schemes", "light",
        "--dry-run",
    ])
    captured = capsys.readouterr()
    out = captured.out + captured.err

    assert exit_code == 0
    assert calls == []
    plan = _plan_from_output(out)
    target = plan["targets"][0]
    assert target["pathway_id"] == "label-only"
    assert target["trigger_selector"] == ""
    assert "selector=-" in out
    unbound = [f for f in plan["settings"]["pathway_flags"] if f["flag"] == "UNBOUND"]
    assert [f["pathway_id"] for f in unbound] == ["label-only"]
    assert "unbound" in unbound[0]["reason"].lower()


# ---------------------------------------------------------------------------
# W0-28-r1 — D93: legacy runs keep their exit codes; the gate is map-only
# ---------------------------------------------------------------------------


def test_legacy_runs_keep_their_exit_codes(monkeypatch, capsys):
    # D93: the quality gate applies only to map-driven runs. A --urls /
    # --sitemap / --crawl / --local run exits exactly as it did before this
    # PR: a baseline-only run is 0, a failed job keeps its old code, and
    # `quality: <CODE>` is printed only when a gate runs.
    baseline = [{"file": "001-home-desktop-light.png", "status": "ok", "states": []}]
    mixed = [
        {"file": "a.png", "status": "ok", "trigger_fired": True, "states": []},
        {"file": "b.png", "status": "failed", "error": "boom", "states": []},
    ]
    broken = [{"file": "a.png", "status": "failed", "error": "boom", "states": []}]
    legacy_modes = {
        "--urls": ["--urls", "example.com"],
        "--sitemap": ["--sitemap", "http://example.com/sitemap.xml"],
        "--crawl": ["--site", "http://example.com"],
        "--local": ["--local", "build"],
    }
    for mode, argv in legacy_modes.items():
        for results, status, expected in (
            (baseline, "done", 0),
            (mixed, "done_with_errors", 3),
            (broken, "failed", 1),
        ):
            monkeypatch.setattr(
                cli,
                "RestedCaptureManager",
                lambda results=results, status=status: _StubManager(results, status),
            )
            exit_code = cli.main(argv)
            captured = capsys.readouterr()
            output = captured.out + captured.err
            assert exit_code == expected, (
                f"{mode} run with status={status} must exit {expected}, "
                f"got {exit_code}"
            )
            assert "quality:" not in output, (
                f"{mode} is a legacy run — no quality gate runs, so no quality line"
            )

    # and a map-driven run still exits 4 on a quality refusal, gate code and all
    monkeypatch.setattr(cli, "RestedCaptureManager", lambda: _StubManager(baseline, "done"))
    exit_code = cli.main([
        "--pathway-manifest", str(WIRING / "named-control-manifest.md"),
        "--base-url", "http://base.test",
    ])
    captured = capsys.readouterr()
    assert exit_code == 4
    assert "quality: BASELINE_ONLY" in captured.out + captured.err


def test_map_driven_baseline_only_run_still_exits_four(monkeypatch, capsys):
    # Edge 3 (second half): the gate keeps refusing a baseline-only
    # MAP-driven run with EXIT_QUALITY_FAIL — its reason code printed.
    results = [{"file": "001-home-desktop-light.png", "status": "ok", "states": []}]
    monkeypatch.setattr(cli, "RestedCaptureManager", lambda: _StubManager(results, "done"))
    exit_code = cli.main([
        "--pathway-manifest", str(WIRING / "named-control-manifest.md"),
        "--base-url", "http://base.test",
    ])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_QUALITY_FAIL == 4
    assert "quality: BASELINE_ONLY" in captured.out + captured.err
