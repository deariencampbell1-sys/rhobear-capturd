"""Tests for capturd.shots.pathway_map — manifest/verifier map parsing.

Ports the meaningful tests of the sunsponge source (parse fixtures, paste-vs-
file equivalence) and adds the loader contract: refusals and deterministic
output. No network, no Playwright — this module is pure parsing.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import pytest

from capturd.shots.pathway_map import (
    PathwayMapError,
    load_pathway_map,
    parse_manifest_md,
    parse_manifest_text,
    parse_verifier_json,
    parse_verifier_json_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "capture-foundations"
HARDENING = FIXTURES / "pathway-manifest-hardening.md"


def test_parse_manifest_sample_extracts_pathways():
    parsed = parse_manifest_md(FIXTURES / "pathway-manifest-sample.md")
    ids = [p["id"] for p in parsed["pathways"]]
    assert ids == ["capture-start", "capture-job-poll", "catalog-search"]
    assert any(r["path"] == "/setup" for r in parsed["routes"])
    # Statuses come out in exactly one spelling, via _normalize_status.
    assert [p["status"] for p in parsed["pathways"]] == ["WIRED", "WIRED", "UNWIRED"]
    assert parsed["summary"]["pathwaysTotal"] == 3
    assert parsed["summary"]["wired"] == 2
    assert parsed["summary"]["unwired"] == 1


def test_parse_verifier_json_extracts_findings_as_pathways():
    parsed = parse_verifier_json(FIXTURES / "sample-verifier-map.json")
    assert len(parsed["pathways"]) == 2
    assert parsed["pathways"][0]["status"] == "UNWIRED"
    assert parsed["pathways"][0]["downstreamCall"] == "/api/state"
    assert parsed["pathways"][0]["check"] == "ui-route-diff"
    assert parsed["metadata"]["repo"] == "sample"


def test_normalize_status_has_one_spelling():
    # Normalisation is exercised through the public parse — no private reach-in.
    def status(raw_status: str) -> str:
        text = "\n".join([
            "# Pathways",
            "",
            "| id | status |",
            "|---|---|",
            f"| p | {raw_status} |",
        ])
        return parse_manifest_text(text)["pathways"][0]["status"]

    assert status("wired") == "WIRED"
    assert status("WIRED") == "WIRED"
    assert status("mocked") == "MOCKED"
    assert status("unwired") == "UNWIRED"
    assert status("UNWIRED FLAG") == "UNWIRED"
    assert status("incomplete") == "INCOMPLETE"
    assert status("UNCLEAR") == "UNCLEAR"
    assert status("yes") == "UNWIRED"
    assert status("⚠") == "UNWIRED"
    assert status("") == "UNKNOWN"


def test_pasted_markdown_manifest_matches_file():
    p = FIXTURES / "pathway-manifest-sample.md"
    from_file = load_pathway_map(manifest_path=p)
    from_text = load_pathway_map(manifest_text=p.read_text(encoding="utf-8"))
    # Same content dict, except the deliberate source label.
    assert from_text == {
        **from_file,
        "file": "<pasted manifest>",
        "metadata": {**from_file["metadata"], "file": "<pasted manifest>"},
    }


def test_pasted_verifier_map_matches_file():
    p = FIXTURES / "sample-verifier-map.json"
    from_file = load_pathway_map(map_path=p)
    from_text = load_pathway_map(map_text=p.read_text(encoding="utf-8"))
    assert from_text == {
        **from_file,
        "file": "<pasted map>",
        "metadata": {**from_file["metadata"], "file": "<pasted map>"},
    }


def test_parse_is_deterministic():
    raw = (FIXTURES / "pathway-manifest-sample.md").read_text(encoding="utf-8")
    first = parse_manifest_text(raw)
    second = parse_manifest_text(raw)
    assert first == second
    assert "parsedAt" not in first and "parsedAt" not in first["metadata"]


def test_verifier_route_method_comes_from_the_code():
    text = json.dumps({
        "summary": {},
        "checks": {
            "api-wiring": {
                "findings": [
                    {"id": "p1", "code": 'post("/api/jobs")', "path": "/api/jobs"},
                    {"id": "p2", "code": 'get("/api/state")', "path": "/api/state"},
                    {"id": "p3", "code": 'submit("/api/form")', "path": "/api/form"},
                ],
            },
        },
    })
    parsed = parse_verifier_json_text(text)
    methods = {r["path"]: r["method"] for r in parsed["routes"]}
    assert methods["/api/jobs"] == "POST"
    assert methods["/api/state"] == "GET"
    assert methods["/api/form"] == ""


def test_verifier_checks_must_be_an_object():
    with pytest.raises(PathwayMapError, match="checks"):
        parse_verifier_json_text('{"checks": []}')
    with pytest.raises(PathwayMapError, match="checks"):
        parse_verifier_json_text('{"checks": "x"}')


def test_empty_paste_is_rejected():
    with pytest.raises(PathwayMapError, match="empty"):
        load_pathway_map(manifest_text="   ")
    with pytest.raises(PathwayMapError, match="empty"):
        parse_manifest_text("")


def test_zero_inputs_is_rejected():
    with pytest.raises(PathwayMapError, match="pathway map is required"):
        load_pathway_map()


def test_two_inputs_at_once_is_rejected():
    p = FIXTURES / "pathway-manifest-sample.md"
    with pytest.raises(PathwayMapError, match="not both"):
        load_pathway_map(manifest_path=p, map_path=FIXTURES / "sample-verifier-map.json")
    with pytest.raises(PathwayMapError, match="not both"):
        load_pathway_map(manifest_path=p, manifest_text=p.read_text(encoding="utf-8"))


def test_malformed_manifest_raises_instead_of_half_filled_dict():
    with pytest.raises(PathwayMapError, match="does not look like a pathway manifest"):
        parse_manifest_text("hello world\nnot a manifest at all\n")
    # Same refusal through the loader.
    with pytest.raises(PathwayMapError, match="does not look like a pathway manifest"):
        load_pathway_map(manifest_text="hello world\n")


def test_malformed_verifier_json_raises():
    with pytest.raises(PathwayMapError, match="not valid verifier JSON"):
        parse_verifier_json_text("{not json")
    with pytest.raises(PathwayMapError, match="must be an object"):
        parse_verifier_json_text("[1, 2]")


def test_missing_file_names_input_not_path():
    with pytest.raises(PathwayMapError, match="manifest_path") as exc:
        load_pathway_map(manifest_path="/nonexistent/manifest.md")
    # The refusal names the offending INPUT; it never echoes the host path.
    assert "/nonexistent" not in str(exc.value)
    with pytest.raises(PathwayMapError, match="map_path") as exc:
        load_pathway_map(map_path="/nonexistent/map.json")
    assert "/nonexistent" not in str(exc.value)


def test_single_pathway_manifest():
    parsed = parse_manifest_md(FIXTURES / "single-pathway-manifest.md")
    assert [p["id"] for p in parsed["pathways"]] == ["home"]
    assert parsed["pathways"][0]["status"] == "WIRED"


def test_full_manifest_extracts_plugins_suspicious_and_anti_patterns():
    parsed = parse_manifest_md(FIXTURES / "pathway-manifest-full.md")
    assert parsed["plugins"] == [
        {
            "name": "rested-capture",
            "raw": {"name": "rested-capture", "kind": "core plugin", "notes": "wires the capture surface"},
        },
        {
            "name": "vault-sync",
            "raw": {"name": "vault-sync", "kind": "skill", "notes": "syncs vault state"},
        },
    ]
    assert parsed["suspicious"] == [
        {
            "call": "`eval(`",
            "location": "RestedCaptureView.jsx:101",
            "why it is risky": "dynamic code execution",
        },
        {
            "call": '`fetch(BASE + "/catalog")`',
            "location": "CatalogView.jsx:243",
            "why it is risky": "URL built from input",
        },
    ]
    # Exact dicts, counts included: "3+" → 3, "~12" → 12, "n/a" → None.
    assert parsed["antiPatterns"] == [
        {"category": "duplicate-fetch", "count": 3, "file": "CatalogView.jsx", "line": "243", "quote": '`fetch(BASE + "/catalog")`'},
        {"category": "console-log-noise", "count": 12, "file": "RestedCaptureView.jsx", "line": "57", "quote": "`console.log(state)`"},
        {"category": "dead-export", "count": None, "file": "hub.ts", "line": "88", "quote": "`export const unusedHelper`"},
    ]


def test_verifier_raw_keeps_the_source_text():
    text = '{"summary": {}, "checks": {}}'
    parsed = parse_verifier_json_text(text)
    assert parsed["raw"] == text


def test_wired_column_yes_is_wired_and_no_is_unwired():
    # A `wired` column carries its own polarity: `yes`/`true`/`✔`/`wired` are
    # WIRED and `no`/`false`/`✗`/`unwired` are UNWIRED — the word is read
    # through the column, so `yes` can never invert into UNWIRED.
    parsed = parse_manifest_md(FIXTURES / "pathway-manifest-boolean-columns.md")
    rows = {p["id"]: p for p in parsed["pathways"]}
    assert rows["boolean-yes"]["status"] == "WIRED"
    assert rows["boolean-yes"]["rawStatus"] == "yes"
    assert rows["boolean-no"]["status"] == "UNWIRED"
    assert rows["boolean-no"]["rawStatus"] == "no"


def test_unwired_column_sets_the_flag_and_the_status():
    # A header of exactly `unwired` is read with the same semantics as
    # `unwired flag`: it drives the status and it sets unwiredFlag.
    parsed = parse_manifest_md(FIXTURES / "pathway-manifest-boolean-columns.md")
    row = next(p for p in parsed["pathways"] if p["id"] == "unwired-flagged")
    assert row["status"] == "UNWIRED"
    assert row["rawStatus"] == "⚠"
    assert row["unwiredFlag"] is True


def test_unwired_flag_sees_a_bare_warning_marker():
    parsed = parse_manifest_md(HARDENING)
    row = next(p for p in parsed["pathways"] if p["id"] == "catalog-search")
    # A lone ⚠ in the unwired-flag cell (empty status column): the flag must
    # fire and the status must normalise to UNWIRED — never stay raw "⚠".
    assert row["unwiredFlag"] is True
    assert row["rawStatus"] == "⚠"
    assert row["status"] == "UNWIRED"


def test_low_severity_finding_is_never_labelled_wired():
    def status_for(severity: str | None) -> str:
        finding = {"id": "f1", "code": 'get("/api/x")', "path": "/api/x"}
        if severity is not None:
            finding["severity"] = severity
        text = json.dumps({"checks": {"api-wiring": {"findings": [finding]}}})
        return parse_verifier_json_text(text)["pathways"][0]["status"]

    # A finding exists — whatever its severity (or lack of one), it must never
    # read as the strongest "nothing wrong here" label.
    assert status_for("low") == "UNCLEAR"
    assert status_for("info") == "UNCLEAR"
    assert status_for(None) == "UNCLEAR"


def test_non_ui_route_diff_check_reads_severity():
    def status_for(check: str, severity: str) -> str:
        finding = {"id": "f1", "code": 'get("/api/x")', "path": "/api/x", "severity": severity}
        text = json.dumps({"checks": {check: {"findings": [finding]}}})
        return parse_verifier_json_text(text)["pathways"][0]["status"]

    assert status_for("suspicious-call", "critical") == "INCOMPLETE"
    assert status_for("suspicious-call", "high") == "INCOMPLETE"
    assert status_for("suspicious-call", "medium") == "UNCLEAR"
    # The ui-route-diff special case still wins, whatever the severity says.
    assert status_for("ui-route-diff", "low") == "UNWIRED"


def test_heading_inside_a_fenced_block_does_not_split_sections():
    parsed = parse_manifest_md(HARDENING)
    # The fenced ``` block sits mid-section and contains a `#` line; neither
    # the table above it nor the table below it may lose rows to a phantom
    # section carved out at the fenced heading.
    assert [p["id"] for p in parsed["pathways"]] == [
        "capture-start", "catalog-search", "export-rows", "settings-open",
    ]


def test_pipe_inside_a_code_span_does_not_shift_columns():
    parsed = parse_manifest_md(HARDENING)
    row = next(p for p in parsed["pathways"] if p["id"] == "export-rows")
    # The `a || b` cell keeps the row's column mapping intact — no shift, no
    # silent drop.
    assert row["downstreamCall"] == "`a || b`"
    assert row["status"] == "MOCKED"
    assert row["unwiredFlag"] is False


def test_non_list_findings_raise():
    # A non-list findings value must refuse, not silently parse to zero
    # pathways.
    with pytest.raises(PathwayMapError, match="findings must be a list"):
        parse_verifier_json_text('{"checks": {"api-wiring": {"findings": {"a": 1}}}}')
    with pytest.raises(PathwayMapError, match="findings must be a list"):
        parse_verifier_json_text('{"checks": {"api-wiring": {"findings": "oops"}}}')


def test_oserror_chain_carries_no_host_path(tmp_path):
    manifest = tmp_path / "manifest.md"
    manifest.write_bytes(b"\xff\xfe not utf-8")
    map_file = tmp_path / "map.json"
    map_file.write_bytes(b"\xff\xfe not utf-8")
    for path, key in ((manifest, "manifest_path"), (map_file, "map_path")):
        with pytest.raises(PathwayMapError) as excinfo:
            load_pathway_map(**{key: path})
        # Render the FULL exception chain: no host directory may appear in it.
        rendered = "".join(traceback.format_exception(
            type(excinfo.value), excinfo.value, excinfo.value.__traceback__,
        ))
        assert str(tmp_path) not in rendered
        assert excinfo.value.__cause__ is None


def test_empty_manifest_text_is_not_silently_ignored():
    # "" is a PROVIDED input, not an absent one: two inputs still collide…
    with pytest.raises(PathwayMapError, match="not both"):
        load_pathway_map(manifest_text="", map_text='{"checks": {}}')
    # …and a lone empty manifest_text must reach the parser's empty refusal,
    # not the loader's "required" complaint.
    with pytest.raises(PathwayMapError, match="empty"):
        load_pathway_map(manifest_text="")


def test_routes_do_not_invent_a_method():
    parsed = parse_manifest_md(HARDENING)
    methods = {r["path"]: r["method"] for r in parsed["routes"]}
    assert methods["/api/captures"] == "GET"
    # An empty Method cell stays "" — the parser never invents a verb.
    assert methods["/api/catalog"] == ""


def test_pathways_table_is_not_counted_as_routes():
    parsed = parse_manifest_md(HARDENING)
    paths = [r["path"] for r in parsed["routes"]]
    # The pathways table (keyed by a `pathway` column) must never feed
    # `routes` — only the routes table's paths may appear.
    assert paths == ["/api/captures", "/api/catalog"]
    assert "/settings" not in paths


def test_empty_manifest_summary_is_not_replaced_by_another_shape():
    text = json.dumps({
        "manifest_summary": {},
        "summary": {"pathwaysTotal": 99},
        "checks": {},
    })
    parsed = parse_verifier_json_text(text)
    # A present-but-empty manifest_summary wins over a differently-shaped
    # summary: every count stays unset instead of borrowing foreign fields.
    assert parsed["summary"] == {}
    assert parsed["summary"].get("pathwaysTotal") is None


def test_file_field_is_basename_never_a_host_path(tmp_path):
    text = (FIXTURES / "pathway-manifest-full.md").read_text(encoding="utf-8")
    left = tmp_path / "left-dir"
    right = tmp_path / "right-dir"
    left.mkdir()
    right.mkdir()
    left_manifest = left / "pathway-manifest-full.md"
    right_manifest = right / "pathway-manifest-full.md"
    left_manifest.write_text(text, encoding="utf-8")
    right_manifest.write_text(text, encoding="utf-8")
    from_left = parse_manifest_md(left_manifest)
    from_right = parse_manifest_md(right_manifest)
    # Same bytes in two different host directories → same dict.
    assert from_left == from_right
    assert from_left["file"] == "pathway-manifest-full.md"
    assert from_left["metadata"]["file"] == "pathway-manifest-full.md"
    dumped = json.dumps(from_left)
    assert str(left) not in dumped
    assert str(right) not in dumped
