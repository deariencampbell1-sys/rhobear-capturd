"""Parse a pathway map into one unified dict shape for capture planning.

A "pathway map" is the audit artefact an agent produces for a product: which UI
pathways exist, whether each is WIRED / MOCKED / UNWIRED / INCOMPLETE / UNCLEAR,
plus the routes, plugins, suspicious calls and anti-patterns behind them. It
arrives in two flavours, both parsed here:

* a pathway manifest in markdown (headings + pipe tables), or
* verifier JSON (``{ summary, checks: { <check>: { findings[] } } }``).

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
_URL_RE = re.compile(r"https?://[^\s`'\"]+", re.IGNORECASE)

_VIEW_FROM_FILE: dict[str, str] = {
    "restedcaptureview": "capture",
    "hubview": "hub",
    "boardview": "board",
    "workbenchview": "workbench",
    "catalogview": "catalog",
    "skillsview": "skills",
    "settingsview": "settings",
    "vaultview": "vault",
    "cronview": "cron",
    "worldsview": "worlds",
    "cliview": "cli",
    "learnview": "learn",
    "app.jsx": "hub",
}


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
        if value
    ]
    if len(provided) > 1:
        raise PathwayMapError(
            "provide a manifest or a map, not both — got "
            + ", ".join(sorted(provided))
        )
    if manifest_text:
        return parse_manifest_text(manifest_text, source="<pasted manifest>")
    if map_text:
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
    except OSError as exc:
        raise PathwayMapError(
            "manifest_path could not be read as UTF-8 text"
        ) from exc
    return parse_manifest_text(raw, source=str(manifest_path))


def parse_manifest_text(raw: str, source: str = "<pasted manifest>") -> dict[str, Any]:
    if not (raw or "").strip():
        raise PathwayMapError("pathway manifest is empty")
    sections = _split_sections(raw)
    parsed = {
        "raw": raw,
        "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "file": source,
        "summary": _extract_summary(sections),
        "pathways": _extract_pathways(sections),
        "routes": _extract_routes(sections),
        "plugins": _extract_plugins(sections),
        "suspicious": _extract_suspicious(sections),
        "antiPatterns": _extract_anti_patterns(sections),
        "metadata": {"file": source},
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
    except OSError as exc:
        raise PathwayMapError(
            "map_path could not be read as UTF-8 text"
        ) from exc
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
        for finding in check.get("findings") or []:
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

    return {
        "raw": None,
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "file": source,
        "summary": data.get("manifest_summary") or data.get("summary") or {},
        "pathways": pathways,
        "routes": routes,
        "plugins": [],
        "suspicious": [],
        "antiPatterns": [],
        "metadata": {
            "file": source,
            "version": data.get("version"),
            "repo": data.get("repo"),
            "manifest_found": data.get("manifest_found"),
        },
    }


def _status_from_finding(check_name: str, finding: dict[str, Any]) -> str:
    if check_name == "ui-route-diff":
        return "UNWIRED"
    severity = str(finding.get("severity") or "").lower()
    if severity in {"critical", "high"}:
        return "INCOMPLETE"
    if severity == "medium":
        return "UNCLEAR"
    return "WIRED"


def _split_sections(raw: str) -> list[dict[str, Any]]:
    lines = raw.split("\n")
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            if current:
                sections.append(current)
            current = {"heading": match.group(2).strip(), "level": len(match.group(1)), "body": []}
        elif current is not None:
            current["body"].append(line)
        else:
            current = {"heading": None, "level": 0, "body": [line]}
    if current:
        sections.append(current)
    return sections


def _extract_summary(sections: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "pathwaysTotal": None,
        "wired": None,
        "mocked": None,
        "unwired": None,
        "incomplete": None,
        "unclear": None,
        "routesCount": None,
        "pluginsCount": None,
        "raw": None,
    }
    for sec in sections:
        text = "\n".join(sec["body"])
        for match in re.finditer(
            r"\|\s*\*{0,2}(WIRED|MOCKED|UNWIRED|INCOMPLETE|UNCLEAR|Wired|Mocked|Unwired|Incomplete|Unclear|Total pathways|TODO|FIXME)\*{0,2}\s*\|?\s*(\d+)\s*\|",
            text,
            re.IGNORECASE,
        ):
            key = match.group(1).lower()
            val = int(match.group(2))
            if "wire" in key and "un" not in key:
                summary["wired"] = val
            elif "mock" in key:
                summary["mocked"] = val
            elif "unwire" in key:
                summary["unwired"] = val
            elif "incomplete" in key:
                summary["incomplete"] = val
            elif "unclear" in key:
                summary["unclear"] = val
            elif "total" in key:
                summary["pathwaysTotal"] = val
        routes_match = re.search(r"(\d+)\s+(?:FastAPI\s+)?routes?", text, re.IGNORECASE)
        if routes_match and summary["routesCount"] is None:
            summary["routesCount"] = int(routes_match.group(1))
        plugins_match = re.search(r"(\d+)\+?\s+(?:bundled\s+)?plugins?", text, re.IGNORECASE)
        if plugins_match and summary["pluginsCount"] is None:
            summary["pluginsCount"] = int(plugins_match.group(1))
    return summary


def _extract_pathways(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pathways: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for sec in sections:
        heading = str(sec.get("heading") or "")
        if re.search(r"suspicious|risk|anti-?pattern|open questions|plugin", heading, re.I):
            continue
        for table in _extract_tables("\n".join(sec["body"])):
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
                status_raw = (
                    row_map.get("status")
                    or row_map.get("wired")
                    or row_map.get("unwired flag")
                    or ""
                ).upper().strip()
                location = (
                    row_map.get("location")
                    or row_map.get("file:line")
                    or f"{row_map.get('file', '')}:{row_map.get('line', '')}".strip(":")
                ).strip()
                if not pid or pid in {"id", "slug", "element"} or len(pid) > 200:
                    continue
                if pid in seen_ids:
                    continue
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
                    "status": _normalize_status(status_raw),
                    "rawStatus": status_raw,
                    "unwiredFlag": bool(re.search(r"\b(y|yes|⚠|true|⚠\s*yes)\b", row_map.get("unwired flag", ""), re.I)),
                })
    return pathways


def _normalize_status(status: str) -> str:
    """One spelling per status: everything funnels through here."""
    text = (status or "").upper()
    if "WIRED" in text and "UN" not in text:
        return "WIRED"
    if "MOCK" in text:
        return "MOCKED"
    if "UNWIRE" in text or "UNWIRED" in text:
        return "UNWIRED"
    if "INCOMPLETE" in text:
        return "INCOMPLETE"
    if "UNCLEAR" in text:
        return "UNCLEAR"
    if text in {"YES", "TRUE", "⚠ YES", "⚠YES"}:
        return "UNWIRED"
    return text or "UNKNOWN"


def _extract_routes(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for sec in sections:
        for table in _extract_tables("\n".join(sec["body"])):
            headers = [h.lower() for h in table["headers"]]
            if not any(re.search(r"path|route|endpoint", h) for h in headers):
                continue
            for row in table["rows"]:
                row_map: dict[str, str] = {}
                for i, header in enumerate(headers):
                    row_map[header] = row[i] if i < len(row) else ""
                method = (row_map.get("method") or row_map.get("http method") or "GET").upper().strip()
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
        for table in _extract_tables("\n".join(sec["body"])):
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
        for table in _extract_tables("\n".join(sec["body"])):
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
        for table in _extract_tables("\n".join(sec["body"])):
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
                patterns.append({
                    "category": cat,
                    "count": int(re.sub(r"\D", "", str(entry.get("count") or "")) or 0) or None,
                    "file": entry.get("file") or "",
                    "line": entry.get("line") or "",
                    "quote": entry.get("quote") or entry.get("snippet") or entry.get("code") or "",
                })
    return patterns


def _extract_tables(text: str) -> list[dict[str, list[str]]]:
    tables: list[dict[str, list[str]]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        header_line = lines[i]
        if "|" not in header_line:
            i += 1
            continue
        cells = _split_row(header_line)
        if len(cells) < 2:
            i += 1
            continue
        sep_line = lines[i + 1] if i + 1 < len(lines) else ""
        if not re.match(r"^\s*\|?[\s\-:|]+\|?\s*$", sep_line) or "-" not in sep_line:
            i += 1
            continue
        rows: list[list[str]] = []
        j = i + 2
        while j < len(lines) and "|" in lines[j]:
            row = _split_row(lines[j])
            if len(row) >= len(cells) - 1:
                rows.append(row)
            j += 1
        tables.append({"headers": [c.strip() for c in cells], "rows": rows})
        i = j
    return tables


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _infer_view_from_location(location: str) -> str | None:
    file_part = (location or "").split(":")[0].lower().replace("\\", "/")
    base = file_part.rsplit("/", 1)[-1]
    compact = base.replace("_", "").replace("-", "")
    for key, view in _VIEW_FROM_FILE.items():
        if key.replace(".", "") in compact or key in base:
            return view
    return None


def _extract_route_path(text: str) -> str | None:
    for match in _ROUTE_METHOD_RE.finditer(text or ""):
        return match.group(2).strip()
    for match in re.finditer(r"(/api/[^\s`'\"]+)", text or ""):
        return match.group(1).strip()
    return None


# resolve_pathway_url / plan_targets_from_map land in capturd/shots/capture.py (brief 01d)
