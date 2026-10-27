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
    RestedCaptureError,
    build_capture_plan,
    build_quality_summary,
)
from capturd.shots.pathway_map import PathwayMapError, load_pathway_map
from capturd.shots import cli

FIXTURES = Path(__file__).parent / "fixtures" / "capture-foundations"
WIRING = Path(__file__).parent / "fixtures" / "capture-wiring"
TWO_CONTROL_HTML = (WIRING / "two-control-page.html").read_text(encoding="utf-8")

@pytest.fixture(autouse=True)
def _offline_test_dns(monkeypatch):
    # Only reserved test hosts and literal addresses; never a real DNS lookup.
    import ipaddress
    import socket

    def resolve(host, port=None, *args, **kwargs):
        aliases = {"base.test": "93.184.216.34", "example.com": "93.184.216.34",
                   "localhost": "127.0.0.1"}
        try:
            address = ipaddress.ip_address(aliases.get(host, host))
        except ValueError:
            raise socket.gaierror("unconfigured test host") from None
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        sockaddr = (str(address), port or 0, 0, 0) if address.version == 6 else (str(address), port or 0)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    monkeypatch.setattr("capturd.shots.capture.socket.getaddrinfo", resolve)



def _stub_loader(monkeypatch, html: str = TWO_CONTROL_HTML) -> None:
    monkeypatch.setattr(
        "capturd.shots.pathway_plan._load_pages_html",
        lambda urls, **_policy: {url: html for url in urls},
    )


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
        "manifest_text": (FIXTURES / "single-pathway-manifest.md").read_text(encoding="utf-8"),
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
    assert set(dataclasses.asdict(map_targets[0])) == {
        "index", "url", "viewport_id", "width", "height", "scheme", "pathway_id",
        "pathway_status", "pathway_trigger", "trigger_selector",
    }
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
        "manifest_text": (WIRING / "named-control-manifest.md").read_text(encoding="utf-8"),
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
    # D93: the gate is map-only — a map-driven run is where it decides.
    exit_code = cli.main([
        "--pathway-manifest", str(WIRING / "named-control-manifest.md"),
        "--base-url", "http://base.test",
    ])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code == cli.EXIT_QUALITY_FAIL == 4
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
    # D93: the gate is map-only — a map-driven run is where it decides.
    exit_code = cli.main([
        "--pathway-manifest", str(WIRING / "named-control-manifest.md"),
        "--base-url", "http://base.test",
    ])
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

    def _record(urls, **_policy) -> dict[str, str]:
        calls.extend(urls)
        raise AssertionError(f"a page read happened for {urls}")

    monkeypatch.setattr("capturd.shots.pathway_plan._load_pages_html", _record)
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


# ---------------------------------------------------------------------------
# W0-01d-r3 — M-expanded-targets: the refusal bounds the EXPANDED matrix
# ---------------------------------------------------------------------------


def test_r3_build_plan_enforces_expanded_matrix_limit(monkeypatch):
    calls: list[list[str]] = []

    def _record(urls, **_policy) -> dict[str, str]:
        calls.append(list(urls))
        return {url: "<html><body></body></html>" for url in urls}

    monkeypatch.setattr("capturd.shots.pathway_plan._load_pages_html", _record)
    map_text = _verifier_map_text([_finding("named-control", "click `#panel-btn`")])
    payload = {
        "map_text": map_text,
        "base_url": "http://base.test",
        "viewports": ["desktop", "tablet", "mobile"],
        "schemes": ["light", "dark"],
        "max_targets": 5,
    }

    # one pathway x 3 viewports x 2 schemes = 6 targets — refused at cap 5,
    # before any navigation
    with pytest.raises(RestedCaptureError) as excinfo:
        build_capture_plan(payload)
    assert str(excinfo.value) == "too many capture targets; maximum is 5"
    assert calls == [], "the expanded-matrix refusal must fire before any navigation"

    # positive control: exactly six targets at cap 6
    _urls, targets, settings = build_capture_plan({**payload, "max_targets": 6})
    assert len(targets) == 6
    assert settings["capture_count"] == 6

    # and the boundary is exact: seven targets at cap 6 is refused (7/6)
    seven = _verifier_map_text([_finding(f"p{i}", "click `#panel-btn`") for i in range(7)])
    before = len(calls)
    with pytest.raises(RestedCaptureError) as excinfo:
        build_capture_plan({
            **payload,
            "map_text": seven,
            "max_targets": 6,
            "viewports": ["desktop"],
            "schemes": ["light"],
        })
    assert str(excinfo.value) == "too many capture targets; maximum is 6"
    assert len(calls) == before, "the refusal must happen before any navigation"


# W0-28-r4: use parsed, existing manifests and real capture/manager contracts.
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from capturd.shots import capture, pathway_plan
from capturd.shots.quality import is_passing, summarize_capture_quality


def _control_less_map():
    result = load_pathway_map(manifest_path=FIXTURES / "single-pathway-manifest.md")
    pathway = dict(result["pathways"][0])
    pathway.update(trigger="", selector="", css="", location="", handler="",
                   downstreamCall="", downstream_call="", declaredIntent="http://169.254.169.254/latest")
    return {**result, "pathways": [pathway]}


def test_r5_control_less_private_map_is_refused_before_capture(monkeypatch):
    reader = Mock()
    monkeypatch.setattr(pathway_plan, "_load_pages_html", reader)
    with pytest.raises(RestedCaptureError, match="private/internal"):
        pathway_plan.plan_targets_from_map(_control_less_map(), "https://example.com")
    reader.assert_not_called()


def test_r5_loopback_is_cli_opt_in_not_payload_default(monkeypatch):
    _stub_loader(monkeypatch)
    payload = {"manifest_text": (WIRING / "named-control-manifest.md").read_text(),
               "base_url": "http://127.0.0.1:9", "viewports": ["desktop"], "schemes": ["light"]}
    with pytest.raises(RestedCaptureError, match="private/internal"):
        build_capture_plan(payload)
    _, targets, settings = build_capture_plan({**payload, "_allow_loopback": True})
    assert targets and settings["allow_loopback"] is True
    assert settings["loopback_host"] == "127.0.0.1"
    captured = []
    original = cli.build_capture_plan
    def record(payload):
        captured.append(payload)
        return original(payload)
    monkeypatch.setattr(cli, "build_capture_plan", record)
    assert cli.main(["--pathway-manifest", str(WIRING / "named-control-manifest.md"),
                     "--base-url", "http://127.0.0.1:9", "--dry-run"]) == 0
    assert captured[0]["_allow_loopback"] is True
    assert captured[0]["_allow_map_files"] is True


@pytest.mark.parametrize("key", ["map_path", "manifest_path"])
def test_r5_map_files_are_cli_only(monkeypatch, key):
    loader = Mock()
    monkeypatch.setattr(capture, "load_pathway_map", loader)
    with pytest.raises(RestedCaptureError) as exc:
        build_capture_plan({key: str(WIRING / "named-control-manifest.md"), "base_url": "http://base.test"})
    assert str(exc.value) == "map file paths are CLI-only; pass map_text or manifest_text"
    loader.assert_not_called()


@pytest.mark.parametrize("value, message", [("many", "max_targets must be a number"),
                                              (0, "max_targets must be at least 1"),
                                              (-1, "max_targets must be at least 1")])
def test_r5_max_targets_has_named_validation(value, message):
    with pytest.raises(RestedCaptureError) as exc:
        build_capture_plan({"manifest_text": (WIRING / "named-control-manifest.md").read_text(),
                            "base_url": "http://base.test", "max_targets": value})
    assert str(exc.value) == message


def test_r5_legacy_expansion_does_not_use_map_ceiling():
    urls, targets, settings = build_capture_plan({"urls": [f"https://example.com/{i}" for i in range(834)]})
    assert len(urls) == 834
    assert len(targets) == settings["capture_count"] == 5004


def test_r5_zero_input_cli_contract(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "one of the arguments" in capsys.readouterr().err
    with pytest.raises(PathwayMapError, match="a pathway map is required"):
        build_capture_plan({})
    for payload in ({"urls": []}, {"sitemap_url": ""}):
        with pytest.raises(RestedCaptureError) as exc:
            build_capture_plan(payload)
        assert str(exc.value) == "add at least one URL"


def test_r5_cli_max_targets_uses_shared_default():
    assert cli.build_parser().parse_args(["--urls", "example.com"]).max_targets == pathway_plan.MAX_TARGETS == 5000
    assert capture.TRIGGER_CLICK_TIMEOUT_MS == 2500


@pytest.mark.parametrize("status, expected", [("failed", 1), ("done_with_errors", 3)])
def test_r5_map_failure_precedes_quality(monkeypatch, capsys, status, expected):
    monkeypatch.setattr(cli, "RestedCaptureManager", lambda: _StubManager([], status))
    assert cli.main(["--pathway-manifest", str(WIRING / "named-control-manifest.md"),
                     "--base-url", "http://base.test"]) == expected
    out = capsys.readouterr()
    assert "quality:" not in out.out + out.err


def _capture_stack(tmp_path, monkeypatch, *, url="https://example.com/", trigger="", states=False):
    # Mock browser I/O only. Drive the real _capture_one AND _sweep_states.
    _, targets, settings = build_capture_plan({"urls": [url], "states": states, "retries": 0,
                                              "viewports": ["desktop"], "schemes": ["light"]})
    target = dataclasses.replace(targets[0], trigger_selector=trigger)
    page = AsyncMock()
    page.url = url
    page.set_default_timeout = Mock()
    async def screenshot(**kwargs):
        Path(kwargs["path"]).write_bytes(b"nonempty-test-screenshot")
    page.screenshot.side_effect = screenshot
    monkeypatch.setattr(capture, "_settle_page", AsyncMock())
    monkeypatch.setattr(capture, "_dismiss_overlays", AsyncMock())
    # Controls/labels originate in the real planner over the existing HTML fixture.
    controls = pathway_plan._plan_page(TWO_CONTROL_HTML)
    fingerprint = ["baseline"]
    element = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
    async def click(**kwargs):
        fingerprint[0] = "opened"
    element.click = AsyncMock(side_effect=click)
    page.locator = Mock(return_value=SimpleNamespace(first=element))
    async def evaluate(script):
        if script == capture._MAP_CONTROLS_JS:
            return [{"id": "0", "label": controls[0].label, "tag": "button"}]
        assert script == capture._FINGERPRINT_JS
        return fingerprint[0]
    async def escape(key):
        fingerprint[0] = "baseline"
    page.keyboard.press.side_effect = escape
    page.evaluate.side_effect = evaluate
    page.query_selector.return_value = element
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    return context, target, settings, tmp_path / "real-result.png", page


@pytest.mark.asyncio
async def test_r5_capture_refuses_private_target_before_goto(tmp_path, monkeypatch):
    context, target, settings, path, page = _capture_stack(tmp_path, monkeypatch, url="http://169.254.169.254/latest")
    result = await capture._capture_one(context, target, settings, path)
    assert result["status"] == "failed" and "private/internal" in result["error"]
    page.goto.assert_not_awaited()
    page.screenshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_r5_capture_one_returns_real_swept_states(tmp_path, monkeypatch):
    context, target, settings, path, page = _capture_stack(tmp_path, monkeypatch, states=True)
    result = await capture._capture_one(context, target, settings, path)
    assert result["status"] == "ok" and result["trigger_fired"] is False
    assert len(result["states"]) == 1
    state = result["states"][0]
    assert state["status"] == "ok" and state["state"] == "open"
    assert state["bytes"] == (path.parent / state["file"]).stat().st_size > 0
    captures = build_quality_summary([result])["results"][0]["manifest"]["captures"]
    assert captures == [{"kind": "baseline", "success": True}, {"kind": "state", "success": True}]
    assert is_passing(build_quality_summary([result])) is True


@pytest.mark.asyncio
async def test_r5_capture_one_propagates_real_trigger(tmp_path, monkeypatch):
    context, target, settings, path, page = _capture_stack(tmp_path, monkeypatch, trigger="#load-state")
    result = await capture._capture_one(context, target, settings, path)
    assert result["status"] == "ok" and result["trigger_fired"] is True
    assert is_passing(build_quality_summary([result])) is True


def test_r5_real_manager_get_preserves_results_for_quality_gate(tmp_path, monkeypatch, capsys):
    context, target, settings, path, page = _capture_stack(tmp_path, monkeypatch, states=True)
    result = asyncio.run(capture._capture_one(context, target, settings, path))
    manager = capture.RestedCaptureManager(tmp_path / "jobs")
    # Start the real job without a background worker; start/get allocate real shape.
    monkeypatch.setattr(capture.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    job = manager.start({"urls": ["https://example.com"], "viewports": ["desktop"], "schemes": ["light"]})
    job_id = job["job_id"]
    assert manager.get(job_id)["results"] == []
    assert is_passing(build_quality_summary(manager.get(job_id)["results"])) is False
    manager._record_result(job_id, result)
    manager._patch(job_id, {"status": "done"})
    job = manager.get(job_id)
    assert job["results"][0] is result
    assert job["completed"] == 1
    assert is_passing(build_quality_summary(job["results"])) is True
    monkeypatch.setattr(manager, "start", lambda payload: manager.get(job_id))
    monkeypatch.setattr(cli, "RestedCaptureManager", lambda: manager)
    assert cli.main(["--pathway-manifest", str(WIRING / "named-control-manifest.md"),
                     "--base-url", "http://base.test"]) == 0


@pytest.mark.asyncio
async def test_r5_capture_final_url_refused_before_screenshot(tmp_path, monkeypatch):
    context, target, settings, path, page = _capture_stack(tmp_path, monkeypatch)
    async def redirect(*args, **kwargs):
        page.url = "http://169.254.169.254/latest"
    page.goto.side_effect = redirect
    result = await capture._capture_one(context, target, settings, path)
    assert result["status"] == "failed" and "private/internal" in result["error"]
    page.screenshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_r5_capture_preserves_first_route_error(tmp_path, monkeypatch):
    context, target, settings, path, page = _capture_stack(tmp_path, monkeypatch)
    errors = []
    async def aborted(*args, **kwargs):
        errors.append(RestedCaptureError("first navigation guard refusal"))
        raise TimeoutError("browser navigation aborted")
    page.goto.side_effect = aborted
    result = await capture._capture_one(context, target, settings, path, guard_errors=errors)
    assert result["status"] == "failed"
    assert result["error"] == "first navigation guard refusal"
    page.screenshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_r6_capture_installs_guard_per_page_before_navigation(tmp_path, monkeypatch):
    context, target, settings, path, page = _capture_stack(tmp_path, monkeypatch, states=True)
    events = []
    async def route(pattern, handler):
        events.append("route")
        assert pattern == "**/*"
    async def new_page():
        events.append("page")
        return page
    page.route = AsyncMock(side_effect=route)
    context.route = AsyncMock()
    context.new_page.side_effect = new_page
    context.close = AsyncMock()
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    class Entry:
        async def __aenter__(self):
            return object()
        async def __aexit__(self, *args):
            return False
    monkeypatch.setattr("playwright.async_api.async_playwright", Entry)
    monkeypatch.setattr(capture, "_launch_browser", AsyncMock(return_value=browser))
    progress = Mock()
    results = await capture._run_capture_async([target], settings, tmp_path, progress)
    assert events == ["page", "route"]
    context.route.assert_not_called()
    assert results[0]["status"] == "ok" and len(results[0]["states"]) == 1
    progress.assert_called_once_with(results[0])
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()


# W0-28-r5: real local-browser boundaries and per-page refusal isolation.
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _R6PageHandler(BaseHTTPRequestHandler):
    body = b"<html><body><h1>local</h1></body></html>"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def _r6_local_url():
    original_body = _R6PageHandler.body
    server = ThreadingHTTPServer(("127.0.0.1", 0), _R6PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        _R6PageHandler.body = original_body
        server.shutdown()
        thread.join()
        server.server_close()


def test_r6_legacy_loopback_cli_captures(_r6_local_url, tmp_path):
    out = tmp_path / "shots"
    code = cli.main(["--urls", _r6_local_url, "--out", str(out),
                     "--viewports", "desktop", "--schemes", "light"])
    assert code == 0
    assert any(path.stat().st_size > 0 for path in out.rglob("*.png"))


def test_r6_private_subresource_does_not_fail_capture(_r6_local_url, tmp_path):
    _R6PageHandler.body = b'<html><body><img src="http://10.0.0.5/private.png"></body></html>'
    _, targets, settings = capture.build_capture_plan({
        "urls": [_r6_local_url], "viewports": ["desktop"], "schemes": ["light"], "retries": 0,
    })
    with pytest.warns(RuntimeWarning, match="capture subresource refused"):
        results = asyncio.run(capture._run_capture_async(targets, settings, tmp_path, lambda result: None))
    assert [result["status"] for result in results] == ["ok"]
    assert (tmp_path / results[0]["file"]).stat().st_size > 0


@pytest.mark.asyncio
async def test_r6_document_refusal_does_not_poison_sibling_page(tmp_path, monkeypatch):
    _, targets, settings = capture.build_capture_plan({
        "urls": ["http://93.184.216.34/bad", "http://93.184.216.34/good"],
        "viewports": ["desktop"], "schemes": ["light"], "retries": 0, "concurrency": 2,
    })
    bad_ready = asyncio.Event()
    shared = {"handler": None}

    class Page:
        def __init__(self, bad):
            self.bad = bad
            self.url = "about:blank"
            self.handler = None

        async def route(self, pattern, handler):
            assert pattern == "**/*"
            self.handler = handler

        def set_default_timeout(self, timeout):
            pass

        async def goto(self, url, **kwargs):
            self.url = url
            if self.bad:
                route = SimpleNamespace(
                    request=SimpleNamespace(url="http://169.254.169.254/latest", resource_type="document"),
                    abort=AsyncMock(), continue_=AsyncMock(),
                )
                await (self.handler or shared["handler"])(route)
                bad_ready.set()
            else:
                await bad_ready.wait()

        async def wait_for_load_state(self, *args, **kwargs):
            pass

        async def screenshot(self, **kwargs):
            Path(kwargs["path"]).write_bytes(b"png")

        async def close(self):
            pass

    pages = [Page(True), Page(False)]

    class Context:
        route = AsyncMock()

        async def new_page(self):
            return pages.pop(0)

        async def add_cookies(self, cookies):
            pass

        async def close(self):
            pass

    context = Context()
    async def context_route(pattern, handler):
        shared["handler"] = handler
    context.route.side_effect = context_route
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())

    class Entry:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("playwright.async_api.async_playwright", Entry)
    monkeypatch.setattr(capture, "_launch_browser", AsyncMock(return_value=browser))
    monkeypatch.setattr(capture, "_settle_page", AsyncMock())
    monkeypatch.setattr(capture, "_dismiss_overlays", AsyncMock())
    results = await capture._run_capture_async(targets, settings, tmp_path, lambda result: None)
    by_url = {result["url"]: result for result in results}
    bad = by_url["http://93.184.216.34/bad"]
    good = by_url["http://93.184.216.34/good"]
    assert bad["status"] == "failed" and "private/internal" in bad["error"]
    assert good["status"] == "ok"
    assert (tmp_path / good["file"]).stat().st_size > 0
    assert not (tmp_path / capture._capture_file_name(targets[0], settings["format"])).exists()
    context.route.assert_not_called()


@pytest.mark.asyncio
async def test_r6_legacy_loopback_host_is_derived_per_target(tmp_path, monkeypatch):
    context, target, settings, path, page = _capture_stack(tmp_path, monkeypatch)
    installed = []
    real_install = capture._install_navigation_guard

    async def spy(page_arg, **kwargs):
        installed.append(kwargs)
        return await real_install(page_arg, **kwargs)

    monkeypatch.setattr(capture, "_install_navigation_guard", spy)
    for host in ("127.0.0.1", "localhost"):
        page.url = f"http://{host}/"
        page.goto.reset_mock()
        page.screenshot.reset_mock()
        page.route = AsyncMock()
        result = await capture._capture_one(
            context, dataclasses.replace(target, url=page.url), settings,
            tmp_path / f"{host.replace('.', '-')}.png",
        )
        assert result["status"] == "ok"
    assert settings["loopback_host"] is None
    assert installed == [
        {"allow_loopback": True, "loopback_host": "127.0.0.1", "guard_errors": []},
        {"allow_loopback": True, "loopback_host": "localhost", "guard_errors": []},
    ]

    async def redirect(*args, **kwargs):
        page.url = "http://localhost/"

    page.goto.side_effect = redirect
    page.screenshot.reset_mock()
    result = await capture._capture_one(
        context, dataclasses.replace(target, url="http://127.0.0.1/"), settings,
        tmp_path / "cross-host.png",
    )
    assert result["status"] == "failed" and "private/internal" in result["error"]
    page.screenshot.assert_not_awaited()
