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
TWO_CONTROL_HTML = (FIXTURES / "two-control-page.html").read_text(encoding="utf-8")


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
        "manifest_path": str(FIXTURES / "named-control-manifest.md"),
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
    def __init__(self, results: list[dict]) -> None:
        self._results = results

    def start(self, payload: dict) -> dict:
        return {"job_id": "stub-job"}

    def get(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "status": "done",
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
    assert any(target["trigger_selector"] for target in plan["targets"])


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
