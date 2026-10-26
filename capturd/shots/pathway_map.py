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
        "antiPatterns": [],
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
        # Counts come from the summary section ONLY: the body of the section
        # whose heading carries a summary word — word-anchored against the
        # allow-list summary/count(s)/total(s)/metric(s), so "Accounting",
        # "Discount" or "Counter" is not a summary section while "Totals"
        # is. A pathway row shaped `| p1 | WIRED | 3 |` sits in a pathways
        # table and must never set summary["wired"] — if no such section
        # exists, counts stay None.
        if not re.search(
            r"\b(summary|counts?|totals?|metrics?)\b",
            str(sec.get("heading") or ""),
            re.I,
        ):
            continue
        text = "\n".join(sec["body"])
        # IGNORECASE makes the uppercase spellings cover every case variant;
        # the two upstream "unfinished" status words map to no downstream
        # branch, so they are gone. ROUTES and PLUGINS ride the same cell
        # shape — counts come from summary cells only, never from prose or a
        # pathway row's text.
        for match in re.finditer(
            r"\|\s*\*{0,2}(WIRED|MOCKED|UNWIRED|INCOMPLETE|UNCLEAR|TOTAL PATHWAYS|ROUTES|PLUGINS)\*{0,2}\s*\|?\s*(\d+)\s*\|",
            text,
            re.IGNORECASE,
        ):
            key = match.group(1).lower()
            val = int(match.group(2))
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
                cat = (
                    entry.get("category")
                    or entry.get("type")
                    or entry.get("anti-pattern")
                    or entry.get("pattern")
                    or ""
                ).strip()
                if not cat:
                    continue
                # The first integer run only: "0" stays 0 (not None),
                # "3.5" reads 3 (not 35), and a missing/empty cell stays
                # None. The old digit-strip glued "3.5" into 35 and then
                # swallowed a real zero into `or None`.
                count_match = re.search(r"\d+", str(entry.get("count") or ""))
                patterns.append({
                    "category": cat,
                    "count": int(count_match.group(0)) if count_match else None,
                    "file": entry.get("file") or "",
                    "line": entry.get("line") or "",
                    "quote": entry.get("quote") or entry.get("snippet") or entry.get("code") or "",
                })
    return patterns


def _extract_tables(text: str, first_line: int = 1) -> list[dict[str, list[str]]]:
    """Extract markdown pipe tables. ``first_line`` is the 1-based line number
    of ``text``'s first line within the original input, so a refused row can
    name its line. A body row may omit the leading pipe (GFM) as long as it
    carries an unescaped pipe and aligns with the header's cell count; a row
    that cannot be aligned is REFUSED, not dropped — a half-filled map, or a
    table parsing to zero rows beside a skipped pipe-bearing line, is the
    failure this module exists to prevent."""
    tables: list[dict[str, list[str]]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        header_line = lines[i]
        if "|" not in header_line:
            i += 1
            continue
        cells = _split_row(header_line, first_line + i)
        if len(cells) < 2:
            i += 1
            continue
        sep_line = lines[i + 1] if i + 1 < len(lines) else ""
        if not re.match(r"^\s*\|?[\s\-:|]+\|?\s*$", sep_line) or "-" not in sep_line:
            i += 1
            continue
        rows: list[list[str]] = []
        j = i + 2
        # The body continues while a line starts with a pipe — or, GFM-style,
        # omits the leading pipe but carries an unescaped one and aligns with
        # the header's cell count. A line that matches neither ends the
        # table. A row that cannot be aligned is REFUSED, never dropped: a
        # table parsing to zero rows beside a skipped pipe-bearing line is
        # exactly the silent drop this module exists to prevent.
        while j < len(lines):
            line = lines[j]
            if not line.strip():
                break
            leading_pipe = re.match(r"^\s*\|", line) is not None
            has_unescaped_pipe = re.search(r"(?<!\\)\|", line) is not None
            if not leading_pipe and not has_unescaped_pipe:
                break  # plain prose — not a row, ends the table
            row = _split_row(line, first_line + j)
            if len(row) == len(cells):
                rows.append(row)
                j += 1
                continue
            if not leading_pipe and rows:
                # A pipe-bearing line AFTER parsed rows reads as prose (the
                # trailing-notes case): it ends the table, no refusal.
                break
            raise PathwayMapError(
                f"table row at line {first_line + j} has {len(row)} cells but its "
                f"header has {len(cells)} — refusing misaligned row, first cell: '{row[0]}'"
            )
        tables.append({"headers": [c.strip() for c in cells], "rows": rows})
        i = j
    return tables


def _split_row(line: str, lineno: int) -> list[str]:
    """Split a table row into cells. A ``|`` inside a backtick code span and
    an escaped ``\\|`` never split — otherwise a cell like `` `a || b` ``
    shifts every later column or gets the row silently dropped. A cell with
    an ODD backtick count is REFUSED, not swallowed: an unterminated code
    span would otherwise eat the rest of the row's pipes."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    buf: list[str] = []
    in_code = False

    def cell_done() -> str:
        cell = "".join(buf).strip()
        if cell.count("`") % 2 == 1:
            raise PathwayMapError(
                f"table line {lineno} has an unbalanced backtick in cell "
                f"'{cell}' — refusing"
            )
        return cell

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
            cells.append(cell_done())
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    cells.append(cell_done())
    return cells
