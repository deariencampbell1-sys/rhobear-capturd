"""Tests for capturd.shots.pathway_map — manifest/verifier map parsing.

Ports the meaningful tests of the sunsponge source (parse fixtures, paste-vs-
file equivalence) and adds the loader contract: refusals and deterministic
output. No network, no Playwright — this module is pure parsing.
"""

from __future__ import annotations

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
    with pytest.raises(ValueError, match="manifest_path"):
        load_pathway_map(manifest_path="/nonexistent/manifest.md")
    with pytest.raises(ValueError, match="map_path"):
        load_pathway_map(map_path="/nonexistent/map.json")


def test_single_pathway_manifest():
    parsed = parse_manifest_md(FIXTURES / "single-pathway-manifest.md")
    assert [p["id"] for p in parsed["pathways"]] == ["home"]
    assert parsed["pathways"][0]["status"] == "WIRED"
