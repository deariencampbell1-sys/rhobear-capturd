"""Tests for capturd.shots.pathway_map — manifest/verifier map parsing.

Ports the meaningful tests of the sunsponge source (parse fixtures, paste-vs-
file equivalence) and adds the loader contract: refusals and deterministic
output. No network, no Playwright — this module is pure parsing.
"""

from __future__ import annotations

import inspect
import json
import re
import traceback
from pathlib import Path

import pytest

from capturd.shots import pathway_map
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


def test_negated_and_qualified_statuses_are_not_wired():
    # Whole canonical tokens only: NOT, PARTIAL, a hyphen or a parenthesis —
    # or any word beyond the canonical token — can never read as WIRED.
    def status(raw_status: str) -> str:
        text = "\n".join([
            "# Pathways",
            "",
            "| id | status |",
            "|---|---|",
            f"| p | {raw_status} |",
        ])
        return parse_manifest_text(text)["pathways"][0]["status"]

    assert status("NOT WIRED") == "UNCLEAR"
    assert status("PARTIALLY WIRED") == "INCOMPLETE"
    assert status("WIRED (mock)") == "UNCLEAR"
    assert status("NOT UNWIRED") == "UNCLEAR"


def test_unknown_status_text_is_unclear_and_kept_raw():
    # The label set is closed: text outside it is UNCLEAR with the raw cell
    # kept in rawStatus — never a seventh label leaking the cell verbatim.
    text = "\n".join([
        "# Pathways",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | mostly fine |",
        "| p2 |  |",
    ])
    rows = {p["id"]: p for p in parse_manifest_text(text)["pathways"]}
    assert rows["p1"]["status"] == "UNCLEAR"
    assert rows["p1"]["rawStatus"] == "mostly fine"
    # UNKNOWN only for a missing cell.
    assert rows["p2"]["status"] == "UNKNOWN"
    assert rows["p2"]["rawStatus"] == ""


def test_short_table_row_is_refused_with_the_line():
    # A row shorter than its header is refused, not dropped: a half-filled
    # map is exactly the failure this module exists to prevent. The refusal
    # names the row's line number and its first cell.
    text = "\n".join([
        "# Pathway Manifest — short row",          # line 1
        "",                                        # line 2
        "## 1. Pathways Table",                    # line 3
        "",                                        # line 4
        "| id | trigger | status |",               # line 5
        "|---|---|---|",                           # line 6
        "| `ok-pathway` | `onClick` | WIRED |",    # line 7
        "| `short-row` | `onClick` |",             # line 8 — one cell short
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "line 8" in message
    assert "short-row" in message


def test_summary_counts_come_from_cells_not_prose():
    # routesCount/pluginsCount come from the summary CELLS (| Routes | 1 |)
    # only — never from prose, so a pathway row saying "2 routes" cannot
    # set the count.
    parsed = parse_manifest_md(FIXTURES / "pathway-manifest-boolean-columns.md")
    assert parsed["summary"]["routesCount"] == 1
    sample = parse_manifest_md(FIXTURES / "pathway-manifest-sample.md")
    assert sample["summary"]["routesCount"] == 2
    assert sample["summary"]["pluginsCount"] == 0


def test_summary_ignores_pathway_rows():
    # D24: counts come from count TABLES only (two columns, category word
    # then count word) — a pathway row shaped `| p1 | WIRED | 3 |` lives in
    # a three-column table, which is never a count table, so it must never
    # set summary["wired"] = 3. And with no count table anywhere, every
    # count stays None.
    text = "\n".join([
        "# Pathway Manifest — row shape",
        "",
        "## Summary Counts",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | 2 |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status | wired |",
        "|---|---|---|",
        "| p1 | WIRED | 3 |",
    ])
    parsed = parse_manifest_text(text)
    assert parsed["summary"]["wired"] == 2
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]
    no_summary = "\n".join([
        "# Pathway Manifest — no summary section",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    bare = parse_manifest_text(no_summary)
    assert all(v is None for v in bare["summary"].values())


def test_accounting_heading_is_not_a_summary_section():
    # D24: the heading never mattered — this stray two-cell pipe table
    # (`| WIRED | 3 |` over a separator, no body rows) is not a count table
    # under ANY heading: its headers ("WIRED", "3") are not a category word
    # followed by a count word. No counts, no error.
    text = "\n".join([
        "# Pathway Manifest — accounting heading",
        "",
        "## Accounting",
        "",
        "| WIRED | 3 |",
        "|---|---|",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    parsed = parse_manifest_text(text)
    assert parsed["summary"]["wired"] is None
    assert all(v is None for v in parsed["summary"].values())
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]


def test_totals_heading_is_a_summary_section():
    # D24: `## Totals` yields its counts because its table has the count-
    # table header shape (`| Category | Count |`) — not because of the
    # heading. The heading name is decorative.
    text = "\n".join([
        "# Pathway Manifest — totals heading",
        "",
        "## Totals",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | 2 |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    parsed = parse_manifest_text(text)
    assert parsed["summary"]["wired"] == 2
    assert parsed["summary"]["pathwaysTotal"] is None


def test_three_column_table_under_summary_heading_sets_no_counts():
    # D7: the summary heading gate alone is not enough — a count row must be
    # exactly two cells (`| <LABEL> | <N> |`). A three-column pathway table
    # under ## Summary has `| p1 | WIRED | 3 |` rows: WIRED sits in the
    # middle cell, so it must not set summary["wired"] — and the pathways
    # must still parse.
    text = "\n".join([
        "# Pathway Manifest — three columns under summary",
        "",
        "## Summary",
        "",
        "| id | status | count |",
        "|---|---|---|",
        "| p1 | WIRED | 3 |",
        "| p2 | UNWIRED | 1 |",
    ])
    parsed = parse_manifest_text(text)
    assert parsed["summary"]["wired"] is None
    assert parsed["summary"]["unwired"] is None
    assert all(v is None for v in parsed["summary"].values())
    assert [p["id"] for p in parsed["pathways"]] == ["p1", "p2"]


def test_count_table_under_any_heading_is_read():
    # D24: a count table is its header SHAPE — exactly two columns, a
    # category word first and a count word second — wherever it sits. A
    # manifest whose counts live under ## Status Overview or ## Breakdown
    # must READ its counts, not raise: the section heading no longer
    # decides anything, so a real feed that parsed yesterday cannot fail
    # today because its count table sits under a heading outside the old
    # summary allow-list.
    for heading in ("Status Overview", "Breakdown", "Distribution", "Summary Counts"):
        text = "\n".join([
            "# Pathway Manifest — counts under any heading",
            "",
            f"## {heading}",
            "",
            "| Category | Count |",
            "|---|---|",
            "| **WIRED** | 5 |",
            "",
            "## 1. Pathways Table",
            "",
            "| id | status |",
            "|---|---|",
            "| p1 | WIRED |",
        ])
        parsed = parse_manifest_text(text)
        assert parsed["summary"]["wired"] == 5, heading
        assert [p["id"] for p in parsed["pathways"]] == ["p1"], heading


def test_bold_header_is_still_a_count_table():
    # D31: header cells are matched after removing markdown emphasis —
    # `*`, `_`, backticks and whitespace are stripped from both header
    # cells before the word-set comparison, so a bold, italic or code-span
    # header is still the count-table shape. This is a REGRESSION guard:
    # the old row-regex read these counts; a verbatim header comparison
    # silently drops every one of them — and a silent drop is the one
    # thing this module must never do.
    for header_row, label in (
        ("| **Category** | **Count** |", "bold"),
        ("| _Category_ | _Count_ |", "italic"),
        ("| `Category` | `Count` |", "code span"),
    ):
        text = "\n".join([
            "# Pathway Manifest — emphasised count header",
            "",
            "## Status Overview",
            "",
            header_row,
            "|---|---|",
            "| **WIRED** | 2 |",
            "| **TOTAL PATHWAYS** | 3 |",
            "",
            "## 1. Pathways Table",
            "",
            "| id | status |",
            "|---|---|",
            "| p1 | WIRED |",
        ])
        parsed = parse_manifest_text(text)
        assert parsed["summary"]["wired"] == 2, label
        assert parsed["summary"]["pathwaysTotal"] == 3, label
        assert [p["id"] for p in parsed["pathways"]] == ["p1"], label


def test_bold_italic_count_row_is_counted():
    # D41: `_COUNT_ROW_RE`'s hand-rolled `\*{0,2}` cannot match a
    # bold-ITALIC label — `***WIRED***` is three asterisks per side — so
    # that count was silently dropped inside the very path that refuses
    # silent drops. Emphasis is stripped from the row label before the
    # label set is matched (the D31 approach), so one, two or three
    # asterisks — and underscores and code spans — all read as the label.
    for row_label, label in (
        ("***WIRED***", "bold-italic"),
        ("*WIRED*", "italic"),
        ("**WIRED**", "bold"),
        ("_WIRED_", "underscore"),
        ("`WIRED`", "code span"),
        ("WIRED", "bare"),
    ):
        text = "\n".join([
            "# Pathway Manifest — emphasised count row",
            "",
            "## Status Overview",
            "",
            "| Category | Count |",
            "|---|---|",
            f"| {row_label} | 4 |",
            "",
            "## 1. Pathways Table",
            "",
            "| id | status |",
            "|---|---|",
            "| p1 | WIRED |",
        ])
        parsed = parse_manifest_text(text)
        assert parsed["summary"]["wired"] == 4, label
        assert [p["id"] for p in parsed["pathways"]] == ["p1"], label


def test_three_column_table_is_not_a_count_table():
    # D24: a count table has EXACTLY two columns. A three-column table —
    # `| Category | Count | Notes |`, wherever it sits — sets no counts and
    # raises nothing.
    for heading in ("Notes", "Summary"):
        text = "\n".join([
            "# Pathway Manifest — three columns",
            "",
            f"## {heading}",
            "",
            "| Category | Count | Notes |",
            "|---|---|---|",
            "| WIRED | 5 | headline figure |",
            "",
            "## 1. Pathways Table",
            "",
            "| id | status |",
            "|---|---|",
            "| p1 | WIRED |",
        ])
        parsed = parse_manifest_text(text)
        assert all(v is None for v in parsed["summary"].values()), heading
        assert [p["id"] for p in parsed["pathways"]] == ["p1"], heading


def test_two_column_pathway_table_is_not_a_count_table():
    # D24: `| id | status |` is two columns but NOT a count table — its
    # second header is not a count word. Its pathway rows set no counts,
    # wherever the table sits; beside it, a table whose header shape says
    # "counts" (category word first, count word second) does feed the
    # summary — the contrast pins the shape rule, not the heading.
    text = "\n".join([
        "# Pathway Manifest — two-column pathway table",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
        "| p2 | UNWIRED |",
        "",
        "## Status Overview",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **TOTAL PATHWAYS** | 2 |",
        "| **WIRED** | 1 |",
    ])
    parsed = parse_manifest_text(text)
    assert parsed["summary"]["pathwaysTotal"] == 2
    assert parsed["summary"]["wired"] == 1
    assert parsed["summary"]["unwired"] is None
    assert [p["id"] for p in parsed["pathways"]] == ["p1", "p2"]


def test_unrecognised_count_header_refuses_and_wider_vocabulary_reads():
    # D38: an unrecognised count-table header REFUSES — it never returns
    # nulls. Three pinned behaviours:
    #
    # 1. The header vocabulary is wider: `metric(s)`, `value(s)`,
    #    `number`, `amount`, `qty` count as count words and `metric(s)`,
    #    `measure`, `item` as category words, so `| Metric | Value |`
    #    READS its counts instead of silently dropping every row.
    # 2. A two-column table whose rows are count rows (a count label in
    #    the first cell, a parseable count in the second) but whose
    #    header is not a recognised count-table header raises
    #    PathwayMapError naming the header — the feed owner fixes the
    #    header; the consumer never receives nulls.
    # 3. A two-column table whose rows are NOT count labels raises
    #    nothing — the refusal stays inside D38's rule and does not
    #    widen to arbitrary two-column tables.
    # Case 1: the wider vocabulary reads its counts.
    metric_text = "\n".join([
        "# Pathway Manifest — metric header",        # line 1
        "",                                          # line 2
        "## Status Overview",                        # line 3
        "",                                          # line 4
        "| Metric | Value |",                        # line 5
        "|---|---|",                                 # line 6
        "| WIRED | 5 |",                             # line 7
        "",                                          # line 8
        "## 1. Pathways Table",                      # line 9
        "",                                          # line 10
        "| id | status |",                           # line 11
        "|---|---|",                                 # line 12
        "| p1 | WIRED |",                            # line 13
    ])
    parsed = parse_manifest_text(metric_text)
    assert parsed["summary"]["wired"] == 5
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]

    # Case 2: an unrecognised header over count rows refuses, naming the
    # header row — never a silent all-None summary.
    thing_text = "\n".join([
        "# Pathway Manifest — unrecognised count header",  # line 1
        "",                                                # line 2
        "## Status Overview",                              # line 3
        "",                                                # line 4
        "| Thing | Blah |",                                # line 5
        "|---|---|",                                       # line 6
        "| WIRED | 5 |",                                   # line 7
        "",                                                # line 8
        "## 1. Pathways Table",                            # line 9
        "",                                                # line 10
        "| id | status |",                                 # line 11
        "|---|---|",                                       # line 12
        "| p1 | WIRED |",                                  # line 13
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(thing_text)
    message = str(excinfo.value)
    assert "Thing" in message
    assert "Blah" in message

    # Case 3: non-count-label rows raise nothing — the refusal needs BOTH
    # a count label and a parseable count, not just a numeric second cell.
    plain_text = "\n".join([
        "# Pathway Manifest — plain two-column table",  # line 1
        "",                                             # line 2
        "## Notes",                                     # line 3
        "",                                             # line 4
        "| Thing | Blah |",                             # line 5
        "|---|---|",                                    # line 6
        "| alpha | 7 |",                                # line 7 — numeric, not a label
        "| beta | gamma |",                             # line 8
        "",                                             # line 9
        "## 1. Pathways Table",                         # line 10
        "",                                             # line 11
        "| id | status |",                              # line 12
        "|---|---|",                                    # line 13
        "| p1 | WIRED |",                               # line 14
    ])
    parsed = parse_manifest_text(plain_text)
    assert all(v is None for v in parsed["summary"].values())
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]


def test_unrecognised_header_error_names_the_vocabulary():
    # D48: the D38 refusal names the vocabulary it accepts, so a feed
    # owner fixes the header in one read instead of guessing. The message
    # must state the accepted count words and category words (asserted
    # against the module's own word sets — at least two of each, matched
    # on word boundaries so prose like "count label" cannot fake a hit)
    # and name the offending header.
    text = "\n".join([
        "# Pathway Manifest — unrecognised count header",  # line 1
        "",                                                # line 2
        "## Status Overview",                              # line 3
        "",                                                # line 4
        "| Thing | Blah |",                                # line 5
        "|---|---|",                                       # line 6
        "| WIRED | 5 |",                                   # line 7
        "",                                                # line 8
        "## 1. Pathways Table",                            # line 9
        "",                                                # line 10
        "| id | status |",                                 # line 11
        "|---|---|",                                       # line 12
        "| p1 | WIRED |",                                  # line 13
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "Thing" in message
    assert "Blah" in message

    def named(words: frozenset[str]) -> list[str]:
        return [w for w in words if re.search(rf"\b{re.escape(w)}\b", message)]

    assert len(named(pathway_map._COUNT_WORDS)) >= 2, message
    assert len(named(pathway_map._CATEGORY_WORDS)) >= 2, message


def test_tables_have_no_first_row_field():
    # D40: the table dict carries no `first_row` key. It was added to every
    # table dict and read by nobody — dead data whose line-number contract
    # invites a future consumer to rely on something unmaintained. The
    # table dict is internal to `_extract_tables` (never returned to
    # callers), so this test reaches into the private extractor to pin the
    # exact shape: `headers` and `rows`, nothing else.
    text = "\n".join([
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    tables = pathway_map._extract_tables(text)
    assert len(tables) == 1
    assert set(tables[0]) == {"headers", "rows"}
    assert "first_row" not in tables[0]
    assert tables[0]["headers"] == ["id", "status"]
    assert tables[0]["rows"] == [["p1", "WIRED"]]


def test_tables_are_aligned_and_retyped():
    # D51: two structural claims, no behaviour change.
    # 1. Rows are refused unless aligned with their header, so the row-map
    #    fill that guards a short row can never take its false branch —
    #    dead defensive code, gone from the module.
    # 2. `_extract_tables`' local accumulator is annotated against its
    #    return (`list[dict[str, Any]]`), not `list[dict[str, list[str]]]`
    #    — the dict's values include `rows: list[list[str]]`.
    source = inspect.getsource(pathway_map)
    assert "if i < len(row)" not in source
    assert "list[dict[str, list[str]]]" not in source
    assert re.search(r"tables: list\[dict\[str, Any\]\] = \[\]", source)
    # Aligned rows survive intact end to end: dropping the dead fill
    # changes nothing for a well-formed table.
    text = "\n".join([
        "# Pathway Manifest — aligned rows",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status | trigger |",
        "|---|---|---|",
        "| p1 | WIRED | onClick |",
    ])
    parsed = parse_manifest_text(text)
    assert parsed["pathways"][0]["trigger"] == "onClick"


def test_both_flavours_return_the_same_key_set():
    # One shape for both flavours: identical top-level keys, with `flavour`
    # saying which produced it; summary and metadata carry the same keys too.
    manifest = parse_manifest_md(FIXTURES / "pathway-manifest-sample.md")
    verifier = parse_verifier_json(FIXTURES / "sample-verifier-map.json")
    assert set(manifest) == set(verifier)
    assert manifest["flavour"] == "manifest"
    assert verifier["flavour"] == "verifier"
    counts = {
        "pathwaysTotal", "wired", "mocked", "unwired",
        "incomplete", "unclear", "routesCount", "pluginsCount",
    }
    assert set(manifest["summary"]) == counts
    assert set(verifier["summary"]) == counts
    metadata = {"file", "version", "repo", "manifest_found"}
    assert set(manifest["metadata"]) == metadata
    assert set(verifier["metadata"]) == metadata
    # None where the source does not say.
    assert manifest["metadata"]["version"] is None
    assert manifest["metadata"]["repo"] is None
    assert manifest["metadata"]["manifest_found"] is None


def test_prose_with_pipe_after_a_table_is_refused():
    # D5 supersedes the D2 prose escape: a pipe-bearing line inside a table
    # body must align with the header or be refused. The parser cannot tell
    # pipe-bearing prose from a misaligned row, and calling it prose is what
    # silently dropped every row after it — here a 2-cell line follows a
    # nine-column table. The table ends only at a line with no unescaped
    # pipe, a blank line, or a new heading; pipe-bearing prose after a
    # table needs a blank line between.
    text = "\n".join([
        "# Pathway Manifest — prose after table",
        "",
        "## 1. Pathways Table",
        "",
        "| id | location | trigger | declared_intent | handler | downstream_call | evidence | status | unwired flag |",
        "|---|---|---|---|---|---|---|---|---|",
        "| p1 | A.jsx:1 | onClick | Launch capture | startCapture | POST /api/captures | A.jsx:1 | WIRED |  |",
        "see a | b above for the wiring notes",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "line 8" in message
    assert "2 cells" in message
    assert "header has 9" in message


def test_table_without_leading_pipes_parses():
    # D2': GFM lets a table row omit the leading pipe. Such a row is
    # parsed, never silently dropped — a table parsing to zero rows beside
    # a pipe-bearing line is exactly the silent drop this module prevents.
    text = "\n".join([
        "# Pathway Manifest — no leading pipes",
        "",
        "## 1. Pathways Table",
        "",
        "id | status | trigger",
        "---|---|---",
        "p1 | WIRED | ok",
    ])
    parsed = parse_manifest_text(text)
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]
    assert parsed["pathways"][0]["status"] == "WIRED"
    assert parsed["pathways"][0]["trigger"] == "ok"


def test_misaligned_row_is_refused():
    # D2': a pipe-bearing row that cannot be aligned with its header is
    # REFUSED naming the line — never dropped, never a zero-row table.
    text = "\n".join([
        "# Pathway Manifest — misaligned row",      # line 1
        "",                                         # line 2
        "## 1. Pathways Table",                     # line 3
        "",                                         # line 4
        "| id | status | trigger |",                # line 5
        "|---|---|---|",                            # line 6
        "p1 | WIRED",                               # line 7 — 2 cells vs 3
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "line 7" in message
    assert "p1" in message


def test_misaligned_row_mid_table_is_refused():
    # D5: a pipe-bearing row in the MIDDLE of a table that cannot be aligned
    # is refused, naming the line and both cell counts. The old parser broke
    # at such a row and silently skipped every row after it — a truncation,
    # not a parse: the parser returns every row of a table or raises.
    text = "\n".join([
        "# Pathway Manifest — mid-table misalignment",  # line 1
        "",                                             # line 2
        "## 1. Pathways Table",                         # line 3
        "",                                             # line 4
        "id | status",                                  # line 5
        "---|---",                                      # line 6
        "p1 | WIRED",                                   # line 7
        "p2 | WIRED | extra",                           # line 8 — 3 cells vs 2
        "p3 | WIRED",                                   # line 9
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "line 8" in message
    assert "3 cells" in message
    assert "header has 2" in message
    # The same refusal when the row does carry its leading pipe.
    piped = "\n".join([
        "# Pathway Manifest — mid-table misalignment",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
        "| p2 | WIRED | extra |",
        "| p3 | WIRED |",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(piped)
    assert "line 8" in str(excinfo.value)


def test_misaligned_row_mid_table_does_not_lose_trailing_rows():
    # The same table as above: the old parser returned a SUCCESSFUL parse of
    # [p1] — nothing refused, and p3 silently absent. D5: every row of a
    # table or a refusal, so a misaligned mid-table row can never leave a
    # successful partial parse behind; and when the row is repaired, p3 must
    # be present in the successful parse.
    text = "\n".join([
        "# Pathway Manifest — trailing row",  # line 1
        "",                                   # line 2
        "## 1. Pathways Table",               # line 3
        "",                                   # line 4
        "id | status",                        # line 5
        "---|---",                            # line 6
        "p1 | WIRED",                         # line 7
        "p2 | WIRED | extra",                 # line 8 — 3 cells vs 2
        "p3 | WIRED",                         # line 9
    ])
    with pytest.raises(PathwayMapError):
        parse_manifest_text(text)
    repaired = "\n".join([
        "# Pathway Manifest — trailing row",
        "",
        "## 1. Pathways Table",
        "",
        "id | status",
        "---|---",
        "p1 | WIRED",
        "p3 | WIRED",
    ])
    assert [p["id"] for p in parse_manifest_text(repaired)["pathways"]] == ["p1", "p3"]


def test_duplicate_pathway_id_is_refused():
    # D3: a repeated id is REFUSED, not silently dropped — a map that says
    # the same pathway twice is ambiguous and must not half-parse.
    text = "\n".join([
        "# Pathway Manifest — duplicate id",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| dup-pathway | WIRED |",
        "| dup-pathway | MOCKED |",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    assert "dup-pathway" in str(excinfo.value)


def test_overlong_pathway_id_is_refused():
    # D3: an id longer than 200 characters is REFUSED, naming the length and
    # the first 40 characters — not silently dropped.
    long_id = "long-" + "x" * 196  # 201 characters
    text = "\n".join([
        "# Pathway Manifest — overlong id",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        f"| {long_id} | WIRED |",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "201" in message
    assert long_id[:40] in message


def test_every_manifest_fixture_parses():
    # D3': the duplicate/over-long id refusals must be proven safe for the
    # inputs we ship — every manifest fixture in the repo parses without
    # raising, so the refusal cannot be blamed for breaking a real input.
    # D6: the test asserts counts, not just non-emptiness — a total, silent
    # loss of a fixture's summary block must fail the suite. EVERY *.md
    # fixture here carries a `## Summary Counts` table (none lacks one); the
    # map lists the counts each fixture's table actually declares, and all
    # of them must come out non-None. A fixture added later must join the
    # map — an unlisted fixture fails below instead of silently skipping
    # the count assertion.
    manifests = sorted(FIXTURES.glob("*.md"))
    assert manifests, "no manifest fixtures found under capture-foundations"
    declared_counts = {
        "pathway-manifest-boolean-columns.md": (
            "pathwaysTotal", "wired", "unwired", "routesCount", "pluginsCount",
        ),
        "pathway-manifest-full.md": ("pathwaysTotal", "wired"),
        "pathway-manifest-hardening.md": ("pathwaysTotal", "wired", "unwired", "mocked"),
        "pathway-manifest-sample.md": (
            "pathwaysTotal", "wired", "unwired", "routesCount", "pluginsCount",
        ),
        "single-pathway-manifest.md": ("pathwaysTotal", "wired"),
    }
    for path in manifests:
        assert path.name in declared_counts, (
            f"{path.name} is not in the D6 count map — say in this test's "
            "comment whether it carries a summary table"
        )
        parsed = parse_manifest_md(path)
        assert parsed["flavour"] == "manifest"
        assert parsed["pathways"], f"{path.name} parsed to zero pathways"
        for key in declared_counts[path.name]:
            assert parsed["summary"][key] is not None, (
                f"{path.name} lost its summary count '{key}' — a summary "
                "block must never be silently dropped"
            )


def test_zero_count_is_kept():
    # D4: a count cell of "0" is 0 — not None. An empty count cell stays
    # None; a real zero is a real count.
    text = "\n".join([
        "# Pathway Manifest — zero count",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
        "",
        "## 2. Anti-patterns",
        "",
        "| Category | Count | File | Line | Quote |",
        "|---|---|---|---|---|",
        "| zero-hits | 0 | hub.ts | 88 | `export const unusedHelper` |",
        "| no-count |  | hub.ts | 89 | `export const otherHelper` |",
    ])
    parsed = parse_manifest_text(text)
    counts = {a["category"]: a["count"] for a in parsed["antiPatterns"]}
    assert counts["zero-hits"] == 0
    # A missing/empty count stays None.
    assert counts["no-count"] is None


def test_decimal_count_reads_the_integer_part():
    # D4: "3.5" reads as 3 — the first integer run, not a digit-strip that
    # would glue "3" and "5" into 35.
    text = "\n".join([
        "# Pathway Manifest — decimal count",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
        "",
        "## 2. Anti-patterns",
        "",
        "| Category | Count | File | Line | Quote |",
        "|---|---|---|---|---|",
        "| decimal-hits | 3.5 | hub.ts | 88 | `export const unusedHelper` |",
    ])
    parsed = parse_manifest_text(text)
    assert parsed["antiPatterns"][0]["count"] == 3


def test_summary_decimal_count_reads_the_integer_part():
    # D25: one parser for every count — the summary path parses its count
    # cell through `_count_from_cell`, exactly like an anti-pattern entry,
    # so `| WIRED | 3.5 |` yields 3 in the summary as it does in an
    # anti-pattern entry (D4): never 35, never None, never a refused row.
    text = "\n".join([
        "# Pathway Manifest — decimal summary count",
        "",
        "## Summary Counts",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | 3.5 |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
        "",
        "## 2. Anti-patterns",
        "",
        "| Category | Count | File | Line | Quote |",
        "|---|---|---|---|---|",
        "| decimal-hits | 3.5 | hub.ts | 88 | `export const unusedHelper` |",
    ])
    parsed = parse_manifest_text(text)
    assert parsed["summary"]["wired"] == 3
    assert parsed["antiPatterns"][0]["count"] == 3


def test_range_and_annotation_read_the_leading_integer():
    # D32: a cell's count is its LEADING integer, unless the cell is two
    # bare integers. A range ("3-5") or an annotation ("3 of 5",
    # "3 (of 5)", "3+") in a count-shaped table must read its leading
    # integer, exactly as the old row-regex did — refusing the whole
    # document over one annotated cell is an abort, not a parse. D26 is
    # not weakened: two BARE integers ("2 5") are still refused, and a
    # cell with no integer at all is still None.
    for cell, expected in (
        ("3-5", 3),
        ("3 of 5", 3),
        ("3 (of 5)", 3),
        ("3+", 3),
    ):
        text = "\n".join([
            "# Pathway Manifest — annotated summary count",
            "",
            "## Summary Counts",
            "",
            "| Category | Count |",
            "|---|---|",
            f"| **WIRED** | {cell} |",
            "",
            "## 1. Pathways Table",
            "",
            "| id | status |",
            "|---|---|",
            "| p1 | WIRED |",
        ])
        parsed = parse_manifest_text(text)
        assert parsed["summary"]["wired"] == expected, cell
        # One parser for every count: the same grammar at the anti-pattern
        # count site.
        anti_text = "\n".join([
            "# Pathway Manifest — annotated anti-pattern count",
            "",
            "## 1. Pathways Table",
            "",
            "| id | status |",
            "|---|---|",
            "| p1 | WIRED |",
            "",
            "## 2. Anti-patterns",
            "",
            "| Category | Count | File | Line | Quote |",
            "|---|---|---|---|---|",
            f"| annotation-hits | {cell} | hub.ts | 88 | `export const x` |",
        ])
        anti = parse_manifest_text(anti_text)
        assert anti["antiPatterns"][0]["count"] == expected, cell

    # The verifier flavour rides the same parser too.
    verifier = parse_verifier_json_text(json.dumps({
        "antiPatterns": [{"category": "r", "count": "3 of 5"}],
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    }))
    assert verifier["antiPatterns"][0]["count"] == 3

    # A cell with no integer at all is None — a count-table cell with no
    # number is not a count (unchanged).
    none_text = "\n".join([
        "# Pathway Manifest — digit-less count cell",
        "",
        "## Summary Counts",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | n/a |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    assert parse_manifest_text(none_text)["summary"]["wired"] is None

    # Two bare integers are still refused — D26 stands (also asserted
    # unchanged in test_cell_with_two_integer_runs_is_refused).
    bare_text = "\n".join([
        "# Pathway Manifest — two bare integers",
        "",
        "## Summary Counts",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | 2 5 |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(bare_text)
    assert "2 5" in str(excinfo.value)


def test_verifier_integer_zero_count_is_kept():
    # D18: an integer zero is a real count. The verifier flavour carries the
    # same anti-pattern shape as the manifest flavour, and a count that
    # arrives as a JSON value is parsed with `raw is None` as the ONLY
    # "absent" test — `0 or ""` is the bug that swallowed a JSON `0` into
    # None while the manifest flavour kept the string cell "0" as 0. The two
    # flavours must agree on the same input value.
    text = json.dumps({
        "antiPatterns": [{"category": "x", "count": 0}],
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    })
    parsed = parse_verifier_json_text(text)
    assert parsed["flavour"] == "verifier"
    assert parsed["antiPatterns"] == [
        {"category": "x", "count": 0, "file": "", "line": "", "quote": ""},
    ]


def test_verifier_anti_patterns_must_be_a_list():
    # D21: a non-list `antiPatterns` is refused by name. A dict yields its
    # keys and a string its characters, so either shape would silently
    # parse to garbage (or zero entries) instead of refusing. The refusal
    # must name the field AND the type it got.
    for payload, type_name in (({}, "dict"), ("x", "str")):
        text = json.dumps({
            "antiPatterns": payload,
            "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
        })
        with pytest.raises(PathwayMapError) as excinfo:
            parse_verifier_json_text(text)
        message = str(excinfo.value)
        assert "antiPatterns" in message, f"refusal must name the field: {message}"
        assert type_name in message, (
            f"refusal must name the type it got ({type_name}): {message}"
        )


def test_verifier_anti_pattern_entry_must_be_a_mapping():
    # D22: a non-dict entry in `antiPatterns` is REFUSED, not skipped.
    # Skipping returns a shorter array than the feed declared with no
    # signal — the silent drop this module exists to prevent. The refusal
    # names the offending entry's index and type.
    for entries, index, type_name in (
        (["fine", 42], 0, "str"),
        ([{"category": "ok"}, 42], 1, "int"),
    ):
        text = json.dumps({
            "antiPatterns": entries,
            "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
        })
        with pytest.raises(PathwayMapError) as excinfo:
            parse_verifier_json_text(text)
        message = str(excinfo.value)
        assert f"index {index}" in message, (
            f"refusal must name the entry's index ({index}): {message}"
        )
        assert type_name in message, (
            f"refusal must name the entry's type ({type_name}): {message}"
        )


def test_anti_pattern_entry_without_category_is_refused():
    # D27: a mapping entry with no usable category is REFUSED, not skipped
    # — mirroring D22 — so the returned array can never be shorter than
    # what the feed declared. The refusal names the entry's index. A
    # falsy-but-present category (0, "") is no category at all.
    for entries, index in (
        ([{"category": 0}], 0),
        ([{"type": ""}], 0),
        ([{"category": "ok"}, {"count": 3}], 1),
    ):
        text = json.dumps({
            "antiPatterns": entries,
            "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
        })
        with pytest.raises(PathwayMapError) as excinfo:
            parse_verifier_json_text(text)
        message = str(excinfo.value)
        assert f"index {index}" in message, (
            f"refusal must name the entry's index ({index}): {message}"
        )
        assert "category" in message, (
            f"refusal must name the missing thing (category): {message}"
        )


def test_verifier_anti_patterns_round_trip():
    # D23: the verifier's own `antiPatterns` array is parsed into the
    # returned map. The key is part of the returned shape and the feed
    # declares the data, so returning [] would be the drop. A well-formed
    # entry lands in `antiPatterns` with its fields intact, through the
    # same normaliser the manifest flavour uses.
    text = json.dumps({
        "antiPatterns": [{
            "category": "mocked-net",
            "count": 3,
            "file": "hub.ts",
            "line": 88,
            "quote": "fetch('/api/x').then(mock)",
        }],
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    })
    parsed = parse_verifier_json_text(text)
    assert parsed["flavour"] == "verifier"
    assert parsed["antiPatterns"] == [{
        "category": "mocked-net",
        "count": 3,
        "file": "hub.ts",
        "line": "88",
        "quote": "fetch('/api/x').then(mock)",
    }]


def test_zero_line_and_file_survive_normalisation():
    # D39: `line` and `file` keep a real zero. `str(entry.get(key) or "")`
    # collapses an integer 0 into "" — the same falsy collapse D18 fixed
    # for `count` — so a feed that says line 0 or file 0 (a synthetic
    # "no real location" marker) silently loses the value. Absence is
    # decided by `is None`, never truthiness: a present zero round-trips
    # as "0", and an absent key stays "".
    text = json.dumps({
        "antiPatterns": [
            {"category": "zeroed", "line": 0, "file": 0},
            {"category": "absent"},
            {"category": "real", "line": 88, "file": "hub.ts"},
        ],
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    })
    parsed = parse_verifier_json_text(text)
    entries = {a["category"]: a for a in parsed["antiPatterns"]}
    assert entries["zeroed"]["line"] == "0"
    assert entries["zeroed"]["file"] == "0"
    assert entries["absent"]["line"] == ""
    assert entries["absent"]["file"] == ""
    assert entries["real"]["line"] == "88"
    assert entries["real"]["file"] == "hub.ts"


def test_comma_formatted_count_is_read_whole_or_refused():
    # D19, decided: READ WHOLE — `,` and spaces are stripped before taking
    # the first integer run, so "1,234" is 1234: never the truncated first
    # run 1, and never a silent None. Both count sites agree: the summary
    # count row and the anti-pattern count cell (whose "1,234" used to read
    # 1, understating the count returned to callers).
    summary_text = "\n".join([
        "# Pathway Manifest — grouped count",
        "",
        "## Summary Counts",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | 1,234 |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    parsed = parse_manifest_text(summary_text)
    assert parsed["summary"]["wired"] == 1234
    anti_text = "\n".join([
        "# Pathway Manifest — grouped anti-pattern count",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
        "",
        "## 2. Anti-patterns",
        "",
        "| Category | Count | File | Line | Quote |",
        "|---|---|---|---|---|",
        "| comma-hits | 1,234 | hub.ts | 88 | `export const x` |",
    ])
    anti = parse_manifest_text(anti_text)
    assert anti["antiPatterns"][0]["count"] == 1234
    # The verifier flavour rides the same count parser: a grouped STRING
    # count in the JSON entry is read whole too.
    verifier_text = json.dumps({
        "antiPatterns": [{"category": "y", "count": "2,468"}],
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    })
    verifier = parse_verifier_json_text(verifier_text)
    assert verifier["antiPatterns"][0]["count"] == 2468


def test_cell_with_two_integer_runs_is_refused():
    # D26: `,` and the non-breaking space are grouping separators INSIDE
    # one number; a plain space between digits is NOT grouping — two
    # independent integer runs are two numbers and must be REFUSED naming
    # the cell, never glued into one ("2 5" is not 25). Grouping is
    # intact: "1,234" is still 1234 at every count site (D19).
    summary_text = "\n".join([
        "# Pathway Manifest — two integer runs",
        "",
        "## Summary Counts",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | 2 5 |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(summary_text)
    assert "2 5" in str(excinfo.value)

    anti_text = "\n".join([
        "# Pathway Manifest — two integer runs in an anti-pattern",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
        "",
        "## 2. Anti-patterns",
        "",
        "| Category | Count | File | Line | Quote |",
        "|---|---|---|---|---|",
        "| two-runs | 2 5 | hub.ts | 88 | `export const x` |",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(anti_text)
    assert "2 5" in str(excinfo.value)

    # Grouping is intact: "1,234" reads whole at both count sites.
    assert parse_manifest_text(
        summary_text.replace("2 5", "1,234")
    )["summary"]["wired"] == 1234
    assert parse_manifest_text(
        anti_text.replace("2 5", "1,234")
    )["antiPatterns"][0]["count"] == 1234


def test_nbsp_grouping_and_verifier_two_run_refusal():
    # D50: the two count paths the suite never exercised.
    # 1. The non-breaking space is a grouping separator INSIDE one number
    #    (named in the D19 grammar) — "1\u00a0234" reads 1234, never the
    #    truncated first run and never a silent None.
    # 2. Two bare integers refused through the VERIFIER path —
    #    parse_verifier_json_text with {"count": "2 5"} — naming the
    #    cell, not only through the manifest summary (D26).
    nbsp_text = "\n".join([
        "# Pathway Manifest — nbsp-grouped count",
        "",
        "## Summary Counts",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | 1\u00a0234 |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    parsed = parse_manifest_text(nbsp_text)
    assert parsed["summary"]["wired"] == 1234

    verifier_text = json.dumps({
        "antiPatterns": [{"category": "z", "count": "2 5"}],
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    })
    with pytest.raises(PathwayMapError) as excinfo:
        parse_verifier_json_text(verifier_text)
    message = str(excinfo.value)
    assert "2 5" in message
    assert "two bare integers" in message


def test_prose_line_with_pipe_and_one_backtick_does_not_abort():
    # D17: a refusal is decided once the line is KNOWN to be a table, never
    # during header detection. A prose line in a Notes section that carries a
    # pipe and an odd number of backticks is not a table — it must be skipped
    # like any other non-table line, not abort the parse. One typo in an
    # appendix must never refuse the whole document.
    text = "\n".join([
        "# Pathway Manifest — stray backtick in prose",   # line 1
        "",                                               # line 2
        "## 1. Pathways Table",                           # line 3
        "",                                               # line 4
        "| id | trigger | status |",                      # line 5
        "|---|---|---|",                                  # line 6
        "| p1 | onClick | WIRED |",                       # line 7
        "",                                               # line 8
        "## 2. Notes",                                    # line 9
        "",                                               # line 10
        "Trigger: click | handler: `startCapture",        # line 11
    ])
    parsed = parse_manifest_text(text)
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]


def test_table_row_with_unbalanced_backtick_is_still_refused():
    # D17's other half, committed as the guard: moving the refusal out of
    # header detection must not re-open the silent swallow. A real table ROW
    # with one stray backtick is still refused, naming the line and the cell.
    text = "\n".join([
        "# Pathway Manifest — stray backtick in a row",   # line 1
        "",                                               # line 2
        "## 1. Pathways Table",                           # line 3
        "",                                               # line 4
        "| id | trigger | status |",                      # line 5
        "|---|---|---|",                                  # line 6
        "| p1 | onClick | WIRED |",                       # line 7
        "| p2 | `onClick | WIRED |",                      # line 8 — stray backtick
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "line 8" in message
    assert "onClick" in message


def test_unbalanced_backtick_is_refused():
    # D5: a cell with an opening backtick and no closer must REFUSE — an
    # unterminated code span would otherwise swallow the rest of the row's
    # pipes, silently shifting or dropping every later column. The refusal
    # names the line and the cell.
    text = "\n".join([
        "# Pathway Manifest — unbalanced backtick",
        "",
        "## 1. Pathways Table",
        "",
        "| id | trigger | status |",
        "|---|---|---|",
        "| p1 | `onClick | WIRED |",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "line 7" in message
    assert "onClick" in message


def test_unbalanced_backtick_in_header_is_refused():
    # D37: the header-level unbalanced-backtick refusal gets a test of its
    # own. A header cell with an odd backtick count opens a code span that
    # swallows the row's remaining pipes — the header collapses to two
    # cells, but the separator line still matches, so the line IS a
    # confirmed table header and the refusal must fire, naming the line.
    # Every previous backtick test used a body row; this pins the header
    # half of the same refusal.
    text = "\n".join([
        "# Pathway Manifest — unbalanced backtick in a header",  # line 1
        "",                                                     # line 2
        "## 1. Pathways Table",                                 # line 3
        "",                                                     # line 4
        "| id | `trigger | status |",                           # line 5 — odd backtick
        "|---|---|---|",                                        # line 6
        "| p1 | onClick | WIRED |",                             # line 7
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "line 5" in message
    assert "trigger" in message


def test_collapsed_header_with_a_separator_refuses():
    # D47: a header whose stray backtick collapses it to fewer than two
    # cells short-circuits at `len(cells) < 2` BEFORE the backtick refusal
    # is inspected — the whole table and every row under it vanish with no
    # error, the exact silent drop this PR exists to close. The separator
    # following proves the line IS a table header, so the same refusal as
    # the >=2-cell case fires, naming the line.
    text = "\n".join([
        "# Pathway Manifest — collapsed header",    # line 1
        "",                                        # line 2
        "## 1. Pathways Table",                    # line 3
        "",                                        # line 4
        "| id | status |",                         # line 5
        "|---|---|",                               # line 6
        "| p1 | WIRED |",                          # line 7
        "",                                        # line 8
        "## 2. Second Table",                      # line 9
        "",                                        # line 10
        "| `a | b |",                              # line 11 — collapses to one cell
        "|---|---|",                               # line 12
        "| r1 | 5 |",                              # line 13
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(text)
    message = str(excinfo.value)
    assert "line 11" in message
    assert "unbalanced backtick" in message


def test_collapsed_header_without_a_separator_is_not_a_table():
    # D47's other half: the refusal fires only where the line is KNOWN to
    # be a table header — a separator follows. `| `a | b |` with no
    # separator after it is not a table: it is skipped like any other
    # non-table line, with no refusal, and the document still parses.
    text = "\n".join([
        "# Pathway Manifest — stray backtick, no separator",  # line 1
        "",                                                   # line 2
        "## Notes",                                           # line 3
        "",                                                   # line 4
        "| `a | b |",                                         # line 5 — no separator below
        "",                                                   # line 6
        "## 1. Pathways Table",                               # line 7
        "",                                                   # line 8
        "| id | status |",                                    # line 9
        "|---|---|",                                          # line 10
        "| p1 | WIRED |",                                     # line 11
    ])
    parsed = parse_manifest_text(text)
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]


def test_unknown_wired_token_is_unclear():
    # D6: the wired column's own polarity — a token outside
    # yes|true|✔|wired / no|false|✗|unwired is UNCLEAR, with the raw cell
    # kept in rawStatus.
    text = "\n".join([
        "# Pathway Manifest — wired maybe",
        "",
        "## 1. Pathways Table",
        "",
        "| id | trigger | wired |",
        "|---|---|---|",
        "| p1 | onClick | maybe |",
    ])
    row = parse_manifest_text(text)["pathways"][0]
    assert row["status"] == "UNCLEAR"
    assert row["rawStatus"] == "maybe"


def test_unknown_unwired_token_is_unclear():
    # D6: the unwired column's inverted polarity — a token outside
    # yes|true|⚠ / no|false is UNCLEAR and does not set the flag.
    text = "\n".join([
        "# Pathway Manifest — unwired maybe",
        "",
        "## 1. Pathways Table",
        "",
        "| id | trigger | unwired |",
        "|---|---|---|",
        "| p1 | onClick | maybe |",
    ])
    row = parse_manifest_text(text)["pathways"][0]
    assert row["status"] == "UNCLEAR"
    assert row["rawStatus"] == "maybe"
    assert row["unwiredFlag"] is False


def test_permission_error_becomes_pathway_map_error(tmp_path, monkeypatch):
    # D6: an OSError on read (PermissionError) becomes PathwayMapError with
    # no host path anywhere in the rendered exception chain — the branch
    # raises `from None`, so the OS error (which names the path) is gone.
    manifest = tmp_path / "manifest.md"
    manifest.write_text("# Pathway Manifest\n", encoding="utf-8")

    def deny(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_md(manifest)
    rendered = "".join(traceback.format_exception(
        type(excinfo.value), excinfo.value, excinfo.value.__traceback__,
    ))
    assert str(tmp_path) not in rendered
    assert excinfo.value.__cause__ is None


def test_verifier_with_no_checks_is_refused():
    # Zero pathways AND zero routes is refused, exactly like the manifest
    # flavour — an empty verifier map must not parse to a success.
    with pytest.raises(PathwayMapError, match="no pathways"):
        parse_verifier_json_text('{"summary": {}, "checks": {}}')
    with pytest.raises(PathwayMapError, match="no pathways"):
        parse_verifier_json_text("{}")
    with pytest.raises(PathwayMapError, match="no pathways"):
        load_pathway_map(map_text='{"checks": {}}')


def test_empty_path_is_refused_explicitly():
    # "" is a PROVIDED path: an explicit refusal naming the input, not a
    # silent fall-through to the "required" complaint.
    with pytest.raises(PathwayMapError, match="manifest_path is empty"):
        load_pathway_map(manifest_path="")
    with pytest.raises(PathwayMapError, match="map_path is empty"):
        load_pathway_map(map_path="")


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
    # The verifier's no-pathways/no-routes refusal keeps an empty-checks
    # map out, so the raw-text property is codified on the smallest map
    # that still parses (one finding, one route).
    text = '{"checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}}}'
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
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    })
    parsed = parse_verifier_json_text(text)
    # A present-but-empty manifest_summary wins over a differently-shaped
    # summary — no borrowing of pathwaysTotal — and the summary carries the
    # same 8 count keys as every other map, None where the source does not
    # say.
    assert set(parsed["summary"]) == {
        "pathwaysTotal", "wired", "mocked", "unwired",
        "incomplete", "unclear", "routesCount", "pluginsCount",
    }
    assert parsed["summary"]["pathwaysTotal"] is None


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


def test_refusals_are_a_structured_list():
    # D49: the refusals are data, not prose. The module defines REFUSALS —
    # a non-empty tuple of short names — and renders it into the module
    # docstring; this test asserts structure (every name appears in the
    # docstring, the list is non-empty), never the wording of any entry,
    # so rewording cannot go red and dropping a refusal cannot slip
    # through unnoticed. Replaces the D33 substring test, which pinned
    # ten literal phrases and went red on any rewording without
    # protecting anything.
    refusals = pathway_map.REFUSALS
    assert isinstance(refusals, tuple)
    assert refusals, "REFUSALS must be non-empty"
    doc = pathway_map.__doc__ or ""
    for name in refusals:
        assert name in doc, f"module docstring must still name refusal: {name}"


def test_count_table_recognised_in_either_column_order():
    # D59: a count table is recognised in either column order — `| Count |
    # Category |` over `| 5 | WIRED |` rows is the same header shape as
    # `| Category | Count |` and yields the same counts; the unrecognised-
    # header refusal lists BOTH accepted orders, because the shape check
    # now recognises both.
    normal_text = "\n".join([
        "# Pathway Manifest — either column order",  # line 1
        "",                                         # line 2
        "## Status Overview",                       # line 3
        "",                                         # line 4
        "| Category | Count |",                     # line 5
        "|---|---|",                                # line 6
        "| **WIRED** | 5 |",                        # line 7
        "| **TOTAL PATHWAYS** | 7 |",               # line 8
        "",                                         # line 9
        "## 1. Pathways Table",                     # line 10
        "",                                         # line 11
        "| id | status |",                          # line 12
        "|---|---|",                                # line 13
        "| p1 | WIRED |",                           # line 14
    ])
    reversed_text = "\n".join([
        "# Pathway Manifest — either column order",
        "",
        "## Status Overview",
        "",
        "| Count | Category |",
        "|---|---|",
        "| 5 | **WIRED** |",
        "| 7 | **TOTAL PATHWAYS** |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    expected = {
        "pathwaysTotal": 7, "wired": 5, "mocked": None, "unwired": None,
        "incomplete": None, "unclear": None, "routesCount": None,
        "pluginsCount": None,
    }
    assert parse_manifest_text(normal_text)["summary"] == expected
    # Same data, columns reversed: the same counts — never an all-None
    # summary with no error.
    assert parse_manifest_text(reversed_text)["summary"] == expected

    # The unrecognised-header refusal names both accepted orders...
    thing_text = "\n".join([
        "# Pathway Manifest — unrecognised count header",  # line 1
        "",                                               # line 2
        "## Status Overview",                             # line 3
        "",                                               # line 4
        "| Thing | Blah |",                               # line 5
        "|---|---|",                                      # line 6
        "| WIRED | 5 |",                                  # line 7
        "",                                               # line 8
        "## 1. Pathways Table",                           # line 9
        "",                                               # line 10
        "| id | status |",                                # line 11
        "|---|---|",                                      # line 12
        "| p1 | WIRED |",                                 # line 13
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(thing_text)
    message = str(excinfo.value)
    assert "category word first" in message
    assert "count word first" in message


def test_detection_never_aborts_on_an_unrelated_cell():
    # D60: during DETECTION, a cell that refuses means "this is not a count
    # row" — the refusal is caught and the row skipped, so an unrelated
    # two-column table can never abort the whole document. The refusal
    # itself still fires where a count is genuinely being read (a real
    # count table's row, an anti-pattern count cell).
    unrelated_text = "\n".join([
        "# Pathway Manifest — unrelated two-column table",  # line 1
        "",                                               # line 2
        "## Notes",                                       # line 3
        "",                                               # line 4
        "| Thing | Blah |",                               # line 5
        "|---|---|",                                      # line 6
        "| foo | 2 5 |",                                  # line 7
        "",                                               # line 8
        "## 1. Pathways Table",                           # line 9
        "",                                               # line 10
        "| id | status |",                                # line 11
        "|---|---|",                                      # line 12
        "| p1 | WIRED |",                                 # line 13
    ])
    parsed = parse_manifest_text(unrelated_text)
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]
    assert all(v is None for v in parsed["summary"].values())

    # The refusing cell beside a count LABEL under an unrecognised header:
    # the detection probe reaches `_count_from_cell` there, and the
    # two-bare-integers refusal escaping detection kills the whole parse.
    # A cell that refuses is not a count — the document must still parse.
    labelled_text = unrelated_text.replace("| foo | 2 5 |", "| WIRED | 2 5 |")
    parsed = parse_manifest_text(labelled_text)
    assert [p["id"] for p in parsed["pathways"]] == ["p1"]
    assert all(v is None for v in parsed["summary"].values())

    # The same refusing cell inside a REAL count table still refuses —
    # there the count is genuinely being read (D26 stands).
    real_count_text = "\n".join([
        "# Pathway Manifest — refusing cell in a real count table",
        "",
        "## Summary Counts",
        "",
        "| Category | Count |",
        "|---|---|",
        "| **WIRED** | 2 5 |",
        "",
        "## 1. Pathways Table",
        "",
        "| id | status |",
        "|---|---|",
        "| p1 | WIRED |",
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(real_count_text)
    assert "2 5" in str(excinfo.value)


def test_duplicate_count_rows_refuse():
    # D61: two rows setting the same summary key refuse, whatever the
    # values say — the ambiguity is the duplicate, not the value (same key
    # and same value is refused too). The refusal names the key and both
    # line numbers, mirroring the duplicate-pathway-id refusal; keeping
    # the last row silently is exactly the half-fill this module refuses.
    dup_same_text = "\n".join([
        "# Pathway Manifest — duplicate count rows",  # line 1
        "",                                         # line 2
        "## Summary Counts",                        # line 3
        "",                                         # line 4
        "| Category | Count |",                     # line 5
        "|---|---|",                                # line 6
        "| **WIRED** | 5 |",                        # line 7
        "| **WIRED** | 5 |",                        # line 8 — same value
        "",                                         # line 9
        "## 1. Pathways Table",                     # line 10
        "",                                         # line 11
        "| id | status |",                          # line 12
        "|---|---|",                                # line 13
        "| p1 | WIRED |",                           # line 14
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(dup_same_text)
    message = str(excinfo.value)
    assert "wired" in message
    assert "line 7" in message
    assert "line 8" in message

    # Conflicting values refuse the same way.
    dup_conflict_text = dup_same_text.replace("| **WIRED** | 5 |\n| **WIRED** | 5 |",
                                              "| **WIRED** | 5 |\n| **WIRED** | 7 |")
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(dup_conflict_text)
    message = str(excinfo.value)
    assert "wired" in message
    assert "line 7" in message
    assert "line 8" in message


def test_normalise_anti_pattern_docstring_matches_behaviour():
    # D62: the documentation says "only an absent key becomes ''", but the
    # behaviour is wider: a PRESENT None also becomes "" — None is not a
    # value (D39's `is None` check treats it as absent). The docs must
    # state the None case, not hide it.
    # Behaviour first, through the public parse: a JSON null in the feed
    # round-trips as "".
    text = json.dumps({
        "antiPatterns": [{"category": "nulled", "file": None, "line": None}],
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    })
    parsed = parse_verifier_json_text(text)
    assert parsed["antiPatterns"] == [
        {"category": "nulled", "count": None, "file": "", "line": "", "quote": ""},
    ]
    # The docstring itself must name the None case — None becoming the
    # empty string, not only the absent key.
    doc = pathway_map._normalise_anti_pattern.__doc__ or ""
    assert re.search(r"None[^.]*becomes[^.]*''", doc), (
        "_normalise_anti_pattern's docstring must state that a present None "
        "becomes '' (like an absent key) — it currently claims only the "
        "absent-key case"
    )


def test_section_body_is_table_parsed_once(monkeypatch):
    # D63: each section body is table-parsed ONCE and the parsed tables are
    # shared by every extractor — `_extract_summary` alone reparsed each
    # section body, so a section was parsed up to six times. Proven with a
    # counter, not a clock: `_extract_tables` is called at most once per
    # section for a fixture document.
    raw = (FIXTURES / "pathway-manifest-sample.md").read_text(encoding="utf-8")
    sections = pathway_map._split_sections(raw)
    assert len(sections) > 1, "the fixture must have several sections"
    calls = {"n": 0}
    real = pathway_map._extract_tables

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(pathway_map, "_extract_tables", counting)
    parsed = parse_manifest_text(raw)
    assert calls["n"] <= len(sections), (
        f"_extract_tables called {calls['n']} times for {len(sections)} "
        "sections — each section body must be table-parsed once and the "
        "parsed tables shared by every extractor"
    )
    # The parse itself is unchanged.
    assert [p["id"] for p in parsed["pathways"]] == [
        "capture-start", "capture-job-poll", "catalog-search",
    ]


def test_manifest_anti_pattern_row_without_category_refuses():
    # D64: the flavours must agree — a declared anti-pattern with no usable
    # category is REFUSED in both. The verifier entry refuses by index
    # (D27); the manifest row refuses by its line. A silently skipped row
    # returns a shorter array than the feed declared with no signal.
    manifest_text = "\n".join([
        "# Pathway Manifest — anti-pattern row without category",  # line 1
        "",                                                      # line 2
        "## 1. Pathways Table",                                  # line 3
        "",                                                      # line 4
        "| id | status |",                                       # line 5
        "|---|---|",                                             # line 6
        "| p1 | WIRED |",                                        # line 7
        "",                                                      # line 8
        "## 2. Anti-patterns",                                   # line 9
        "",                                                      # line 10
        "| Category | Count | File | Line | Quote |",             # line 11
        "|---|---|---|---|---|",                                 # line 12
        "| real-find | 3 | hub.ts | 88 | `export const x` |",     # line 13
        "|  | 5 | hub.ts | 89 | `export const y` |",              # line 14 — no category
    ])
    with pytest.raises(PathwayMapError) as excinfo:
        parse_manifest_text(manifest_text)
    message = str(excinfo.value)
    assert "line 14" in message
    assert "category" in message

    # The verifier flavour already refuses such an entry by index (D27) —
    # both flavours agree on the same input value.
    verifier_text = json.dumps({
        "antiPatterns": [{"category": "ok"}, {"count": 3}],
        "checks": {"api-wiring": {"findings": [{"id": "f1", "path": "/api/x"}]}},
    })
    with pytest.raises(PathwayMapError) as excinfo:
        parse_verifier_json_text(verifier_text)
    assert "index 1" in str(excinfo.value)
