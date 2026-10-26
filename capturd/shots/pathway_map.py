"""Parse a pathway map into one unified dict shape for capture planning.

A "pathway map" is the audit artefact an agent produces for a product: which UI
pathways exist, whether each is WIRED / MOCKED / UNWIRED / INCOMPLETE / UNCLEAR,
plus the routes, plugins, suspicious calls and anti-patterns behind them. It
arrives in two flavours, both parsed here:

* a pathway manifest in markdown (headings + pipe tables), or
* verifier JSON (``{ summary, checks: { <check>: { findings[] } } }``).

Both flavours parse to the same top-level shape — ``flavour`` says which
produced it — and ``rawStatus`` keeps its per-flavour meaning: the raw status
cell for a manifest, the ``SEVERITY``/check name for a verifier finding.

Captur'd is a desktop tool: the map usually arrives as PASTED text, so
``load_pathway_map`` accepts exactly one of a file path, pasted manifest
markdown, pasted verifier JSON — and a pasted input parses to the same content
as the equivalent file. Output is deterministic: same input bytes, same dict
(no timestamps, no host paths).

The parser refuses rather than half-fill: a table is returned with every row
or the parse raises ``PathwayMapError`` — never part of one. Counts come from
count tables — tables whose header SHAPE is exactly two columns, a category
word first and a count word second (D24) — wherever they sit; a three-column
table sets no counts and raises nothing, and a two-column pathway table
(``| id | status |``) is not a count table because its second header is not
a count word. The refusal is decided once a line is KNOWN to be a
table, never during header detection: a prose line that carries a pipe
and a stray backtick is skipped like any other non-table line, not a
parse abort (D17).

Verifier JSON may declare the anti-patterns array itself: its well-formed
entries round-trip into the returned ``antiPatterns`` through the same
normaliser the manifest flavour uses (D23), while a non-list array is
refused naming the field and the type it got (D21), a non-mapping entry is
refused naming its index and type — never skipped (D22) — and a mapping
entry with no usable category is refused the same way (D27): the returned
array is never shorter than what the feed declared.

Refusals — every one of these constructs raises ``PathwayMapError``
rather than half-fill, and each is named here so the list cannot live
only in a PR body: a misaligned row (a row whose cell count differs
from its header's, named with its line number); pipe-bearing prose
immediately after a table, which the parser cannot tell from a
misaligned row — put a blank line between; a duplicate ``id`` or an
over-long ``id`` (over 200 characters); an unbalanced backtick in a confirmed table header or row; a verifier
``antiPatterns`` value that is not a list; an anti-pattern entry that is
not a mapping; an anti-pattern entry with no usable category; a two-column
table whose rows are count rows (a count label with a numeric count) but
whose header is not a recognised count-table header — the feed owner fixes
the header, the consumer never receives nulls (D38); and a count cell of
two bare integers (``2 5`` — nothing between the runs but whitespace),
which is ambiguous and is never glued into one number.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

__all__ = [
    "PathwayMapError",
    "load_pathway_map",
    "parse_manifest_md",
    "parse_manifest_text",
    "parse_verifier_json",
    "parse_verifier_json_text",
]

_ROUTE_METHOD_RE = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH|WS)\s+(/[^\s`'\"]+)",
    re.IGNORECASE,
)
# The capture-resolution half — URL resolution, target planning, view/route
# inference — lands in capturd/shots/capture.py (brief 01d).


class PathwayMapError(ValueError):
    """Raised when a pathway map is missing, ambiguous or unparseable."""


def load_pathway_map(
    *,
    manifest_path: str | Path | None = None,
    map_path: str | Path | None = None,
    manifest_text: str | None = None,
    map_text: str | None = None,
) -> dict[str, Any]:
    """Load a pathway map. PASTED text is the primary path (Captur'd is a desktop
    tool — the user pastes/uploads the manifest their agent produced); file paths
    are the CLI convenience. manifest_* = markdown pathway-manifest;
    map_* = verifier JSON. Exactly one input may be given."""
    provided = [
        name
        for name, value in (
            ("manifest_path", manifest_path),
            ("map_path", map_path),
            ("manifest_text", manifest_text),
            ("map_text", map_text),
        )
        if value is not None
    ]
    if len(provided) > 1:
        raise PathwayMapError(
            "provide a manifest or a map, not both — got "
            + ", ".join(sorted(provided))
        )
    # An empty path is a PROVIDED path too: refuse it explicitly by name
    # instead of letting it fall through to the "required" complaint.
    if manifest_path is not None and not manifest_path:
        raise PathwayMapError("manifest_path is empty")
    if map_path is not None and not map_path:
        raise PathwayMapError("map_path is empty")
    # ``is not None``, not truthiness: "" is a PROVIDED input — two inputs
    # must still collide, and a lone empty paste must reach the parser's
    # empty-input refusal, not the loader's "required" complaint.
    if manifest_text is not None:
        return parse_manifest_text(manifest_text, source="<pasted manifest>")
    if map_text is not None:
        return parse_verifier_json_text(map_text, source="<pasted map>")
    if manifest_path:
        path = Path(manifest_path)
        if not path.is_file():
            # Name the offending input but never echo the path itself.
            raise PathwayMapError(
                "manifest_path does not exist or is not a readable file"
            )
        return parse_manifest_md(path)
    if map_path:
        path = Path(map_path)
        if not path.is_file():
            raise PathwayMapError(
                "map_path does not exist or is not a readable file"
            )
        return parse_verifier_json(path)
    raise PathwayMapError(
        "a pathway map is required — paste the manifest markdown (or pass a file)"
    )


def parse_manifest_md(manifest_path: Path) -> dict[str, Any]:
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # ``from None``: the chained error would carry the host path in
        # ``__cause__.filename`` (and UnicodeDecodeError is no OSError), and
        # the parsed map promises never to leak host paths.
        raise PathwayMapError(
            "manifest_path could not be read as UTF-8 text"
        ) from None
    return parse_manifest_text(raw, source=str(manifest_path))


def parse_manifest_text(raw: str, source: str = "<pasted manifest>") -> dict[str, Any]:
    if not (raw or "").strip():
        raise PathwayMapError("pathway manifest is empty")
    sections = _split_sections(raw)
    parsed = {
        "raw": raw,
        "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        # Basename only: the parsed map must not carry host paths.
        "file": Path(source).name,
        "flavour": "manifest",
        "summary": _extract_summary(sections),
        "pathways": _extract_pathways(sections),
        "routes": _extract_routes(sections),
        "plugins": _extract_plugins(sections),
        "suspicious": _extract_suspicious(sections),
        "antiPatterns": _extract_anti_patterns(sections),
        # The manifest says nothing about these — None, not a borrowed value.
        "metadata": {
            "file": Path(source).name,
            "version": None,
            "repo": None,
            "manifest_found": None,
        },
    }
    if not parsed["pathways"] and not parsed["routes"]:
        # Refuse instead of returning a half-filled dict: text with none of the
        # manifest's tables is not a pathway manifest.
        raise PathwayMapError(
            "text does not look like a pathway manifest — no pathways or routes "
            "table found (expected markdown headings and pipe tables)"
        )
    return parsed


def parse_verifier_json(map_path: Path) -> dict[str, Any]:
    try:
        text = map_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise PathwayMapError(
            "map_path could not be read as UTF-8 text"
        ) from None
    return parse_verifier_json_text(text, source=str(map_path))


def parse_verifier_json_text(text: str, source: str = "<pasted map>") -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PathwayMapError(
            "pathway map is not valid verifier JSON"
        ) from exc
    if not isinstance(data, dict):
        raise PathwayMapError("verifier map JSON must be an object")

    pathways: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    anti_patterns: list[dict[str, Any]] = []
    seen_route_paths: set[str] = set()

    checks = data.get("checks")
    if checks is not None and not isinstance(checks, dict):
        raise PathwayMapError("verifier map 'checks' must be an object")
    for check_name, check in (checks or {}).items():
        if not isinstance(check, dict):
            continue
        findings = check.get("findings")
        if findings is None:
            continue
        if not isinstance(findings, list):
            # A dict yields its keys and a string its characters; everything
            # would be skipped below and the parse would "succeed" with zero
            # pathways. Refuse instead.
            raise PathwayMapError(
                f"verifier check '{check_name}' findings must be a list"
            )
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            fid = str(finding.get("id") or f"{check_name}-{len(pathways)}")
            file_ref = str(finding.get("file") or "")
            line_ref = str(finding.get("line") or "")
            location = f"{file_ref}:{line_ref}".strip(":")
            route_path = str(finding.get("path") or "").strip()
            pathways.append({
                "id": fid,
                "location": location,
                "trigger": str(finding.get("code") or finding.get("message") or "").strip(),
                "declaredIntent": str(finding.get("message") or "").strip(),
                "handler": str(check_name),
                "downstreamCall": route_path,
                "expectedSideEffect": str(finding.get("suggestion") or "").strip(),
                "evidence": str(finding.get("code") or "").strip(),
                "status": _status_from_finding(check_name, finding),
                "rawStatus": str(finding.get("severity") or check_name).upper(),
                "unwiredFlag": check_name == "ui-route-diff",
                "check": check_name,
            })
            if route_path and route_path not in seen_route_paths:
                seen_route_paths.add(route_path)
                # The verb comes from the finding's code — ``post("/api/x")`` or
                # ``POST /api/x`` — with call punctuation stripped so the one
                # _ROUTE_METHOD_RE matches both shapes; "" when undeterminable.
                probe = str(finding.get("code") or "")
                for punct in "()\"'`":
                    probe = probe.replace(punct, " ")
                method_match = _ROUTE_METHOD_RE.search(probe)
                routes.append({
                    "method": method_match.group(1).upper() if method_match else "",
                    "path": route_path,
                    "handler": location,
                    "raw": finding,
                })

    # D18: the verifier JSON may carry the anti-patterns array itself — the
    # rhobear-verifier manifest.js field semantics this module is compatible
    # with name { ..., antiPatterns[], ... } — and an entry's count arrives
    # as a JSON value. The entries ride the SAME normaliser as the manifest
    # flavour, so the two flavours agree on the same input value: an integer
    # 0 count is a real count (only `raw is None` is absent), never a silent
    # None. An anti-patterns-only map still parses to nothing a planner can
    # use: the no-pathways/no-routes refusal below is unchanged.
    declared_anti_patterns = data.get("antiPatterns")
    if declared_anti_patterns is not None and not isinstance(declared_anti_patterns, list):
        # D21: name the field AND the type it got — a dict yields its keys
        # and a string its characters, so a bare "must be a list" hides
        # what actually arrived and the caller cannot tell a shape bug
        # from a payload bug.
        raise PathwayMapError(
            "verifier map 'antiPatterns' must be a list, got "
            f"{type(declared_anti_patterns).__name__}"
        )
    for index, entry in enumerate(declared_anti_patterns or []):
        if not isinstance(entry, dict):
            # D22: refuse, never skip. A skipped entry returns a shorter
            # array than the feed declared with no signal — precisely the
            # silent half-fill this module exists to prevent. The refusal
            # names the offending entry's index and type.
            raise PathwayMapError(
                "verifier map 'antiPatterns' entry at index "
                f"{index} must be an object, got {type(entry).__name__} — refusing"
            )
        pattern = _normalise_anti_pattern(entry)
        if pattern is None:
            # D27: refuse, never skip — a mapping entry with no usable
            # category would return a shorter array than the feed declared
            # with no signal. Mirrors D22: the refusal names the entry's
            # index (a falsy-but-present category — 0, "" — is no category
            # at all).
            raise PathwayMapError(
                "verifier map 'antiPatterns' entry at index "
                f"{index} has no usable category (category/type/anti-pattern/"
                "pattern all missing or empty) — refusing"
            )
        anti_patterns.append(pattern)

    # Key presence decides, not truthiness: a present-but-empty
    # manifest_summary must not be swapped for a differently-shaped summary.
    manifest_summary = data.get("manifest_summary")
    if isinstance(manifest_summary, dict):
        source_summary: dict[str, Any] = manifest_summary
    elif isinstance(data.get("summary"), dict):
        source_summary = data["summary"]
    else:
        source_summary = {}

    if not pathways and not routes:
        # The same refusal the manifest flavour already makes: a verifier map
        # that yields zero pathways and zero routes is not a map.
        raise PathwayMapError(
            "verifier map yielded no pathways and no routes — refusing an empty map"
        )

    return {
        # Markdown parsing keeps the source text in ``raw``; JSON does too.
        "raw": text,
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        # Basename only: the parsed map must not carry host paths.
        "file": Path(source).name,
        "flavour": "verifier",
        "summary": _normalised_summary(source_summary),
        "pathways": pathways,
        "routes": routes,
        "plugins": [],
        "suspicious": [],
        "antiPatterns": anti_patterns,
        "metadata": {
            "file": Path(source).name,
            "version": data.get("version"),
            "repo": data.get("repo"),
            "manifest_found": data.get("manifest_found"),
        },
    }


_SUMMARY_COUNT_KEYS = (
    "pathwaysTotal",
    "wired",
    "mocked",
    "unwired",
    "incomplete",
    "unclear",
    "routesCount",
    "pluginsCount",
)

# D24: a count table is identified by its header SHAPE, not by the section
# heading — exactly two columns, the first a category word, the second a
# count word. Wherever such a table sits, its rows are count rows: there is
# no "wrong place" for a count table to be, so a real feed whose counts
# live under ## Status Overview or ## Breakdown reads them instead of
# raising. A three-column table is never a count table, and a two-column
# pathway table (| id | status |) is not one because `status` is not a
# count word. D38: the vocabulary is wider — a feed that names its count
# column Metric / Value / Number / Amount / Qty, or its category column
# Metric / Measure / Item, is still the count-table shape; before the
# widening those headers were not recognised and every count under them
# was silently returned as None.
_COUNT_WORDS = frozenset(
    {"count", "counts", "n", "total", "totals", "paths", "pathways", "#",
     "metric", "metrics", "value", "values", "number", "amount", "qty"}
)
_CATEGORY_WORDS = frozenset(
    {"category", "status", "kind", "label", "state", "type", "name",
     "metric", "metrics", "measure", "item"}
)


# D31: header cells are matched after removing markdown emphasis — `*`,
# `_`, backticks and whitespace are stripped from both header cells before
# the word-set comparison, so `| **Category** | **Count** |`,
# `| _Category_ | _Count_ |` and `` | `Count` | ... `` are all count
# tables. The old row-regex tolerated the emphasis in row labels; the
# shape check must tolerate it in headers too, or every summary count
# under an emphasised header is silently dropped.
_EMPHASIS_RE = re.compile(r"[\s*_`]+")


def _is_count_table(headers: list[str]) -> bool:
    """D24: the count-table header shape — exactly two columns, the first a
    category word, the second a count word. This decides WHERE counts may
    be read from; the section heading is irrelevant. D31: the comparison
    runs on the header cells with markdown emphasis removed (`*`, `_`,
    backticks and whitespace stripped), so a bold or italic header is
    still a count table — never a silent drop of every count under it."""
    if len(headers) != 2:
        return False
    first, second = (_EMPHASIS_RE.sub("", header).lower() for header in headers)
    return first in _CATEGORY_WORDS and second in _COUNT_WORDS


# D7: a count row's LABEL decides WHICH count the row sets — WIRED /
# MOCKED / UNWIRED / INCOMPLETE / UNCLEAR / TOTAL PATHWAYS / ROUTES /
# PLUGINS, optionally bold-wrapped. D25: the label is ALL the regex
# decides — the count VALUE is parsed by `_count_from_cell`, the one
# count parser for both flavours, so a summary cell is read exactly like
# an anti-pattern cell ("3.5" → 3 in both) and the two sites can never
# disagree. The old digit-run value group and its end-of-line anchor are
# gone with that job.
_COUNT_ROW_RE = re.compile(
    r"^\*{0,2}(WIRED|MOCKED|UNWIRED|INCOMPLETE|UNCLEAR"
    r"|TOTAL PATHWAYS|ROUTES|PLUGINS)\*{0,2}$",
    re.IGNORECASE,
)


def _normalised_summary(source: dict[str, Any]) -> dict[str, Any]:
    """The one summary shape for both flavours: the same 8 count keys, with
    None where the source does not say."""
    return {key: source.get(key) for key in _SUMMARY_COUNT_KEYS}


def _status_from_finding(check_name: str, finding: dict[str, Any]) -> str:
    if check_name == "ui-route-diff":
        return "UNWIRED"
    severity = str(finding.get("severity") or "").lower()
    if severity in {"critical", "high"}:
        return "INCOMPLETE"
    # A finding exists — it must never read as the strongest "nothing wrong
    # here" label: medium/low/info/absent severity all stay UNCLEAR.
    return "UNCLEAR"


def _split_sections(raw: str) -> list[dict[str, Any]]:
    """Split into heading sections. Heading matches inside a fenced code
    block (``` or ~~~) are ignored — a ``# comment`` in a fence must not
    carve out a phantom section and gut the table above it. Each section
    carries ``start``: the 0-based index of its first body line in ``raw``,
    so a refused table row can name its line number in the input."""
    lines = raw.split("\n")
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    open_fence: str | None = None  # the opening marker, e.g. "```" or "~~~"
    for idx, line in enumerate(lines):
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if open_fence is None:
            if fence_match:
                open_fence = fence_match.group(1)
            else:
                match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
                if match:
                    if current:
                        sections.append(current)
                    current = {"heading": match.group(2).strip(), "level": len(match.group(1)), "body": [], "start": idx + 1}
                    continue
        else:
            # A closing fence is a bare run of the same marker, at least as
            # long as the opening one; fenced lines stay in the body.
            stripped = line.strip()
            if stripped and set(stripped) == {open_fence[0]} and len(stripped) >= len(open_fence):
                open_fence = None
        if current is not None:
            current["body"].append(line)
        else:
            current = {"heading": None, "level": 0, "body": [line], "start": idx}
    if current:
        sections.append(current)
    return sections


def _extract_summary(sections: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {key: None for key in _SUMMARY_COUNT_KEYS}
    for sec in sections:
        # D24: the section heading no longer decides anything — every table
        # in the document is checked against the count-table header shape,
        # and a matching table's rows are read wherever the table sits. A
        # pathway row shaped `| p1 | WIRED | 3 |` lives in a three-column
        # table, which is never a count table, so it cannot set a count.
        for table in _extract_tables("\n".join(sec["body"]), sec["start"] + 1):
            if not _is_count_table(table["headers"]):
                continue
            # Counts come from the table's CELLS only, never from prose.
            # The row label decides WHICH count the row sets (D7).
            for row in table["rows"]:
                match = _COUNT_ROW_RE.match(row[0])
                if not match:
                    continue
                key = match.group(1).lower()
                # D25: one parser for every count — the cell itself goes
                # through `_count_from_cell`, exactly like an anti-pattern
                # entry's count, so the two sites cannot disagree.
                val = _count_from_cell(row[1])
                if val is None:
                    # An empty or digit-less cell sets nothing — a real
                    # zero is a real count (D4).
                    continue
                if "unwire" in key:
                    summary["unwired"] = val
                elif "wire" in key:
                    summary["wired"] = val
                elif "mock" in key:
                    summary["mocked"] = val
                elif "incomplete" in key:
                    summary["incomplete"] = val
                elif "unclear" in key:
                    summary["unclear"] = val
                elif "total" in key:
                    summary["pathwaysTotal"] = val
                elif "route" in key:
                    summary["routesCount"] = val
                elif "plugin" in key:
                    summary["pluginsCount"] = val
    return summary


def _extract_pathways(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pathways: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for sec in sections:
        heading = str(sec.get("heading") or "")
        if re.search(r"suspicious|risk|anti-?pattern|open questions|plugin", heading, re.I):
            continue
        for table in _extract_tables("\n".join(sec["body"]), sec["start"] + 1):
            headers = [h.lower() for h in table["headers"]]
            if "rank" in headers:
                continue
            has_id = any(re.search(r"^id$|slug|element|pathway", h) for h in headers)
            has_status = any(h in {"status", "wired", "unwired flag", "unwired"} for h in headers)
            if not has_id or not has_status:
                continue
            for row in table["rows"]:
                row_map: dict[str, str] = {}
                for i, header in enumerate(headers):
                    row_map[header] = row[i] if i < len(row) else ""
                pid = (
                    row_map.get("id")
                    or row_map.get("slug")
                    or row_map.get("element")
                    or row_map.get("name")
                    or row_map.get("pathway")
                    or ""
                ).strip().strip("`")
                status_cell = row_map.get("status")
                wired_cell = row_map.get("wired")
                flag_cell = row_map.get("unwired flag")
                unwired_cell = row_map.get("unwired")
                flag_value = _first_set(flag_cell, unwired_cell)
                # Polarity comes from the column, not from the word: each
                # status-bearing column is read with its own polarity, in the
                # priority status > wired > unwired flag/unwired.
                if status_cell is not None and status_cell.strip():
                    status_raw = status_cell.strip()
                    status = _normalize_status(status_raw)
                elif wired_cell is not None and wired_cell.strip():
                    status_raw = wired_cell.strip()
                    status = _status_from_wired_column(status_raw)
                elif flag_value is not None:
                    status_raw = flag_value.strip()
                    status = _status_from_unwired_column(status_raw)
                else:
                    status_raw, status = "", "UNKNOWN"
                location = (
                    row_map.get("location")
                    or row_map.get("file:line")
                    or f"{row_map.get('file', '')}:{row_map.get('line', '')}".strip(":")
                ).strip()
                # The placeholder-header skip stays a skip — `id`/`slug`/
                # `element` is a header row, not a pathway. But a real id that
                # is over-long or already seen is REFUSED, not dropped: a
                # half-filled map is the failure this module exists to prevent.
                if not pid or pid in {"id", "slug", "element"}:
                    continue
                if len(pid) > 200:
                    raise PathwayMapError(
                        f"pathway id is {len(pid)} characters (max 200), "
                        f"first 40: '{pid[:40]}' — refusing"
                    )
                if pid in seen_ids:
                    raise PathwayMapError(
                        f"duplicate pathway id '{pid}' — refusing"
                    )
                seen_ids.add(pid)
                pathways.append({
                    "id": pid,
                    "location": location,
                    "trigger": (row_map.get("trigger") or row_map.get("href") or "").strip(),
                    "selector": (row_map.get("selector") or row_map.get("css") or row_map.get("css selector") or "").strip().strip("`"),
                    "declaredIntent": (
                        row_map.get("declared intent")
                        or row_map.get("declared_intent")
                        or row_map.get("intent")
                        or ""
                    ).strip(),
                    "handler": (row_map.get("handler") or row_map.get("downstream call") or row_map.get("downstream_call") or "").strip(),
                    "downstreamCall": (
                        row_map.get("downstream call")
                        or row_map.get("downstream_call")
                        or row_map.get("network call")
                        or row_map.get("expected side effect")
                        or row_map.get("expected_side_effect")
                        or ""
                    ).strip(),
                    "expectedSideEffect": (
                        row_map.get("expected side effect")
                        or row_map.get("expected_side_effect")
                        or row_map.get("side effect")
                        or ""
                    ).strip(),
                    "evidence": (row_map.get("evidence") or "").strip(),
                    "status": status,
                    "rawStatus": status_raw,
                    # The flag lives in the `unwired flag` / `unwired` column
                    # and is set by that column's polarity (yes/true/⚠).
                    "unwiredFlag": flag_value is not None
                    and _status_from_unwired_column(flag_value) == "UNWIRED",
                })
    return pathways


def _first_set(*cells: str | None) -> str | None:
    """The first cell that exists and is not empty — one column at a time, so
    an empty cell falls through to the next status-bearing column."""
    for cell in cells:
        if cell is not None and cell.strip():
            return cell
    return None


def _status_from_wired_column(cell: str) -> str:
    """A `wired` column carries its own polarity: yes|true|✔|wired → WIRED,
    no|false|✗|unwired → UNWIRED, anything else → UNCLEAR."""
    token = " ".join(cell.split()).lower()
    if token in {"yes", "true", "✔", "wired"}:
        return "WIRED"
    if token in {"no", "false", "✗", "unwired"}:
        return "UNWIRED"
    return "UNCLEAR"


def _status_from_unwired_column(cell: str) -> str:
    """An `unwired flag` / `unwired` column carries the inverted polarity:
    yes|true|⚠ → UNWIRED, no|false → WIRED, anything else → UNCLEAR."""
    token = " ".join(cell.split()).lower()
    if token in {"yes", "true", "⚠"}:
        return "UNWIRED"
    if token in {"no", "false"}:
        return "WIRED"
    return "UNCLEAR"


_CANONICAL_STATUSES = ("WIRED", "MOCKED", "UNWIRED", "INCOMPLETE", "UNCLEAR")


def _normalize_status(status: str) -> str:
    """One spelling per status, matched on whole canonical tokens — never a
    substring scan. Anything carrying NOT, PARTIAL, a hyphen or a parenthesis
    (or any word beyond the canonical token) is UNCLEAR — INCOMPLETE for
    PARTIAL — and never WIRED; text outside the closed label set is UNCLEAR
    with the raw cell kept in rawStatus, and UNKNOWN is only for a missing
    cell."""
    text = " ".join(str(status or "").split())
    if not text:
        return "UNKNOWN"
    upper = text.upper()
    if "PARTIAL" in upper:
        return "INCOMPLETE"
    if re.search(r"\bNOT\b", upper) or any(ch in upper for ch in "-()"):
        return "UNCLEAR"
    # Whole tokens only: a bare ⚠ rides along (it means unwired on its own);
    # the neutral word FLAG — the `unwired flag` column's own name, echoed
    # back as a status — is dropped.
    words = [w for w in upper.replace("⚠", " ⚠ ").split() if w != "FLAG"]
    letters = [w for w in words if re.search(r"[A-Z]", w)]
    if len(set(letters)) == 1 and letters[0] in _CANONICAL_STATUSES:
        return letters[0]
    if letters and set(letters) <= {"YES", "TRUE"}:
        return "UNWIRED"
    if not letters and "⚠" in upper:
        return "UNWIRED"
    return "UNCLEAR"


def _extract_routes(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for sec in sections:
        for table in _extract_tables("\n".join(sec["body"]), sec["start"] + 1):
            headers = [h.lower() for h in table["headers"]]
            # Anchored: a `pathway` column in a pathways table must not pull
            # its rows into routes.
            if not any(re.search(r"^path$|^route$|^endpoint$", h) for h in headers):
                continue
            for row in table["rows"]:
                row_map: dict[str, str] = {}
                for i, header in enumerate(headers):
                    row_map[header] = row[i] if i < len(row) else ""
                # "" when the manifest gives no verb — never invent GET.
                method = (row_map.get("method") or row_map.get("http method") or "").upper().strip()
                path = (row_map.get("path") or row_map.get("route") or row_map.get("endpoint") or "").strip().strip("`")
                if not path:
                    continue
                routes.append({
                    "method": method,
                    "path": path,
                    "handler": (row_map.get("handler") or row_map.get("handler file:line") or "").strip(),
                    "raw": row_map,
                })
    return routes


def _extract_plugins(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plugins: list[dict[str, Any]] = []
    for sec in sections:
        if not re.search(r"plugin", sec.get("heading") or "", re.I):
            continue
        for table in _extract_tables("\n".join(sec["body"]), sec["start"] + 1):
            headers = [h.lower() for h in table["headers"]]
            if not any(re.search(r"^name$|plugin|skill", h) for h in headers):
                continue
            for row in table["rows"]:
                row_map: dict[str, str] = {}
                for i, header in enumerate(headers):
                    row_map[header] = row[i] if i < len(row) else ""
                name = (row_map.get("name") or row_map.get("plugin") or row_map.get("skill") or "").strip()
                if not name or name.lower() == "name":
                    continue
                plugins.append({"name": name, "raw": row_map})
    return plugins


def _extract_suspicious(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suspicious: list[dict[str, Any]] = []
    for sec in sections:
        if not re.search(r"suspicious|top\s+\d|risk", sec.get("heading") or "", re.I):
            continue
        for table in _extract_tables("\n".join(sec["body"]), sec["start"] + 1):
            headers = [h.lower() for h in table["headers"]]
            if not headers:
                continue
            for row in table["rows"]:
                entry: dict[str, str] = {}
                for i, header in enumerate(headers):
                    entry[header] = row[i] if i < len(row) else ""
                if all(not v or v == headers[0] for v in entry.values()):
                    continue
                suspicious.append(entry)
    return suspicious


def _count_from_cell(raw: Any) -> int | None:
    """The one count parser for both flavours — the grammar, written out
    (D32):

    * ``raw is None`` is the only "absent" (D18) — a JSON integer ``0``
      is a real count, never a silent None.
    * grouping separators sit INSIDE one number and are stripped first
      (D19): ``,` and the non-breaking space, so "1,234" is 1234, never
      the truncated first run 1 and never a silent None.
    * a decimal number is one run and reads its integer part (D4):
      "3.5" → 3.
    * otherwise the cell's count is its LEADING integer (D32): whatever
      follows the first integer run — a range ("3-5"), an annotation
      ("3 of 5", "3 (of 5)"), a trailing mark ("3+"), a tilde ("~12") —
      is not the count, exactly as the old row-regex read it.
    * two BARE integers — nothing between the two runs but whitespace
      ("2 5") — are REFUSED naming the cell (D26): two numbers with no
      separator is ambiguous, and guessing would glue them into one
      wrong count (never 25).
    * a cell with no integer at all is None — a count-table cell with no
      number is not a count.

    The verifier anti-patterns round trip (D23) parses each entry's
    ``count`` through this same parser."""
    if raw is None:
        return None
    text = str(raw)
    # Grouping separators live INSIDE one number: `,` and the non-breaking
    # space are stripped, so "1,234" reads whole (D19).
    joined = text.replace(",", "").replace("\u00a0", "")
    # A decimal point does not split: one decimal number is one run, so
    # "3.5" keeps its integer part (D4).
    matches = list(re.finditer(r"\d+(?:\.\d+)?", joined))
    if not matches:
        return None
    if len(matches) > 1:
        between = joined[matches[0].end():matches[1].start()]
        if not between.strip():
            # D26/D32: two bare integers separated by nothing but
            # whitespace are two numbers — refuse rather than glue.
            raise PathwayMapError(
                f"count cell '{text}' has two bare integers "
                f"('{matches[0].group(0).split('.')[0]}' and "
                f"'{matches[1].group(0).split('.')[0]}') with nothing between "
                "them but whitespace — refusing"
            )
        # D32: any other separator (a hyphen, "of", parentheses, a plus)
        # makes the later runs an annotation or range — the count is the
        # leading integer, never a refusal.
    return int(matches[0].group(0).split(".", 1)[0])


def _normalise_anti_pattern(entry: dict[str, Any]) -> dict[str, Any] | None:
    """One anti-pattern shape for both flavours: the locked 5 keys, with the
    same category aliases and count parser whether the entry came from a
    manifest table row or from verifier JSON (D18 — the flavours must agree
    on the same input value). Returns None for an entry with no category:
    the manifest caller skips such a table row, while the verifier caller
    REFUSES the entry by index instead (D27) so its returned array can
    never be shorter than what the feed declared. The verifier's own
    ``antiPatterns`` array round-trips through here (D23); a non-mapping
    entry never reaches this normaliser — the verifier caller refuses it
    by index and type (D22)."""
    category = str(
        entry.get("category")
        or entry.get("type")
        or entry.get("anti-pattern")
        or entry.get("pattern")
        or ""
    ).strip()
    if not category:
        return None
    return {
        "category": category,
        "count": _count_from_cell(entry.get("count")),
        "file": str(entry.get("file") or ""),
        "line": str(entry.get("line") or ""),
        "quote": str(entry.get("quote") or entry.get("snippet") or entry.get("code") or ""),
    }


def _extract_anti_patterns(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    for sec in sections:
        if not re.search(r"anti-?pattern|finding|grep|catch", sec.get("heading") or "", re.I):
            continue
        for table in _extract_tables("\n".join(sec["body"]), sec["start"] + 1):
            headers = [h.lower() for h in table["headers"]]
            if not any(re.search(r"category|type|anti-?pattern|pattern", h) for h in headers):
                continue
            for row in table["rows"]:
                entry: dict[str, str] = {}
                for i, header in enumerate(headers):
                    entry[header] = row[i] if i < len(row) else ""
                # D18: the entry normalisation is shared with the verifier
                # flavour (one shape, one count parser), so an integer 0
                # count is a real count in both flavours alike.
                pattern = _normalise_anti_pattern(entry)
                if pattern is not None:
                    patterns.append(pattern)
    return patterns


def _extract_tables(text: str, first_line: int = 1) -> list[dict[str, Any]]:
    """Extract markdown pipe tables. ``first_line`` is the 1-based line number
    of ``text``'s first line within the original input, so a refused row can
    name its line. A body row may omit the leading pipe (GFM). D5: the table
    ends only at a blank line, a line with no unescaped ``|``, or a new
    heading; a pipe-bearing line inside the body is a row — aligned it is
    returned, misaligned it raises ``PathwayMapError`` naming the line and
    both cell counts. The parser either returns every row of a table or
    raises — it never returns part of one. D17: the unbalanced-backtick
    refusal fires only where the line is already a confirmed header (the
    separator matched) or a confirmed body row — never during header
    detection, so pipe-bearing prose with a stray backtick does not abort
    the parse."""
    tables: list[dict[str, list[str]]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        header_line = lines[i]
        if "|" not in header_line:
            i += 1
            continue
        cells, backtick_refusal = _split_row(header_line, first_line + i)
        if len(cells) < 2:
            i += 1
            continue
        sep_line = lines[i + 1] if i + 1 < len(lines) else ""
        if not re.match(r"^\s*\|?[\s\-:|]+\|?\s*$", sep_line) or "-" not in sep_line:
            i += 1
            continue
        if backtick_refusal is not None:
            # D17: the separator matched, so this line IS a table header —
            # only now is the unbalanced-backtick refusal decided. A prose
            # line above a non-separator line was skipped untouched.
            raise PathwayMapError(backtick_refusal)
        rows: list[list[str]] = []
        j = i + 2
        # D5: the body ends only at a blank line, a line with no unescaped
        # pipe, or a new heading (headings never reach this loop — sections
        # are split first). A pipe-bearing line is a ROW: aligned it is
        # returned, misaligned it is REFUSED naming the line and both
        # counts. There is no "prose with a pipe" escape: the parser cannot
        # tell pipe-bearing prose from a misaligned row, and calling it
        # prose is what silently dropped every row after it. Pipe-bearing
        # prose after a table needs a blank line between.
        while j < len(lines):
            line = lines[j]
            if not line.strip():
                break
            if not re.search(r"(?<!\\)\|", line):
                break  # no unescaped pipe — not a row, ends the table
            row, backtick_refusal = _split_row(line, first_line + j)
            if backtick_refusal is not None:
                # D17: this line is a confirmed row (the table was
                # established by header + separator), so the refusal fires
                # — an unterminated code span swallows the row's remaining
                # pipes, which makes the cell count untrustworthy; the
                # backtick is the defect to name.
                raise PathwayMapError(backtick_refusal)
            if len(row) != len(cells):
                raise PathwayMapError(
                    f"table row at line {first_line + j} has {len(row)} cells but its "
                    f"header has {len(cells)} — refusing misaligned row, first cell: '{row[0]}'"
                )
            rows.append(row)
            j += 1
        headers = [c.strip() for c in cells]
        # D38: a two-column table whose rows are COUNT rows — a count label
        # in the first cell (D7's label set) and a parseable count in the
        # second — but whose header is not a recognised count-table header
        # must REFUSE naming the header row, never return every count as
        # None. The header vocabulary is fixed (D24), so the only
        # correction available is to the feed: it fixes the header, the
        # consumer never receives nulls. A row whose second cell has no
        # integer parses as None and is not a count row — arbitrary
        # two-column tables (| id | status |, | method | path |) stay
        # outside the refusal. A row whose cell is two bare integers
        # raises through `_count_from_cell` on its own — the ambiguity is
        # refused wherever the cell sits.
        if len(headers) == 2 and not _is_count_table(headers):
            for row in rows:
                if _COUNT_ROW_RE.match(row[0]) and _count_from_cell(row[1]) is not None:
                    raise PathwayMapError(
                        f"table header at line {first_line + i} is "
                        f"'{' | '.join(headers)}' — its rows read like count "
                        f"rows (count label '{row[0]}' with a numeric count) "
                        "but the header is not a recognised count-table "
                        "header (category word first, count word second) — "
                        "refusing rather than returning nulls"
                    )
        tables.append({
            "headers": [c.strip() for c in cells],
            "rows": rows,
            # 1-based input line of the first body row (rows are contiguous:
            # every pipe-bearing line is a row or the parse raises).
            "first_row": first_line + i + 2,
        })
        i = j
    return tables


def _split_row(line: str, lineno: int) -> tuple[list[str], str | None]:
    """Split a table row into cells. A ``|`` inside a backtick code span and
    an escaped ``\\|`` never split — otherwise a cell like `` `a || b` ``
    shifts every later column or gets the row silently dropped.

    D17: this function NEVER raises. A line is not yet known to be a table
    when header detection calls it, and a prose line can carry a pipe and
    an odd number of backticks — refusing there would abort the whole
    document on one typo in an appendix. A cell with an ODD backtick count
    is flagged instead: the second return value carries the ready-made
    refusal message (``None`` when every cell is balanced), and the caller
    raises it only once it has established the line is a table header or
    row."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    buf: list[str] = []
    in_code = False
    refusal: str | None = None

    def cell_done() -> None:
        nonlocal refusal
        cell = "".join(buf).strip()
        if refusal is None and cell.count("`") % 2 == 1:
            refusal = (
                f"table line {lineno} has an unbalanced backtick in cell "
                f"'{cell}' — refusing"
            )
        cells.append(cell)

    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "`":
            in_code = not in_code
            buf.append(ch)
            i += 1
        elif ch == "\\" and not in_code and i + 1 < len(text) and text[i + 1] == "|":
            buf.append("|")
            i += 2
        elif ch == "|" and not in_code:
            cell_done()
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    cell_done()
    return cells, refusal
