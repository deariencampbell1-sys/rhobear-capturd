"""Tests for capturd.shots.pathway_map — manifest/verifier map parsing.

Ports the meaningful tests of the sunsponge source (parse fixtures, paste-vs-
file equivalence) and adds the loader contract: refusals and deterministic
output. No network, no Playwright — this module is pure parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capturd.shots.pathway_map import (
    PathwayMapError,
    _normalize_status,
    load_pathway_map,
    parse_manifest_md,
    parse_manifest_text,
    parse_verifier_json,
    parse_verifier_json_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "capture-foundations"


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
    assert _normalize_status("wired") == "WIRED"
    assert _normalize_status("WIRED") == "WIRED"
    assert _normalize_status("mocked") == "MOCKED"
    assert _normalize_status("unwired") == "UNWIRED"
    assert _normalize_status("UNWIRED FLAG") == "UNWIRED"
    assert _normalize_status("incomplete") == "INCOMPLETE"
    assert _normalize_status("UNCLEAR") == "UNCLEAR"
    assert _normalize_status("yes") == "UNWIRED"
    assert _normalize_status("") == "UNKNOWN"


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
    with pytest.raises(ValueError, match="empty"):
        parse_manifest_text("")


def test_zero_inputs_is_rejected():
    with pytest.raises(ValueError, match="pathway map is required"):
        load_pathway_map()


def test_two_inputs_at_once_is_rejected():
    p = FIXTURES / "pathway-manifest-sample.md"
    with pytest.raises(ValueError, match="not both"):
        load_pathway_map(manifest_path=p, map_path=FIXTURES / "sample-verifier-map.json")
    with pytest.raises(ValueError, match="not both"):
        load_pathway_map(manifest_path=p, manifest_text=p.read_text(encoding="utf-8"))


def test_malformed_manifest_raises_instead_of_half_filled_dict():
    with pytest.raises(PathwayMapError, match="does not look like a pathway manifest"):
        parse_manifest_text("hello world\nnot a manifest at all\n")
    # Same refusal through the loader.
    with pytest.raises(ValueError, match="does not look like a pathway manifest"):
        load_pathway_map(manifest_text="hello world\n")


def test_malformed_verifier_json_raises():
    with pytest.raises(ValueError, match="not valid verifier JSON"):
        parse_verifier_json_text("{not json")
    with pytest.raises(ValueError, match="must be an object"):
        parse_verifier_json_text("[1, 2]")


def test_missing_file_names_input_not_path():
    with pytest.raises(ValueError, match="manifest_path") as exc:
        load_pathway_map(manifest_path="/nonexistent/manifest.md")
    # The refusal names the offending INPUT; it never echoes the host path.
    assert "/nonexistent" not in str(exc.value)
    with pytest.raises(ValueError, match="map_path") as exc:
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
