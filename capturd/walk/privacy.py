"""The automated pre-publication privacy scan (GAPS-NOW M10, brief 09 item 6).

`scan_demo(spec, texts)` reads every text a demo carries (each step's line,
page URL, typed value, target text and voice direction, plus whole files such
as captions and transcript) and returns findings `{where, kind}`. A finding
never carries the matched value. Pixels are not scanned here (no OCR), so a
person still checks the frames before approval. This is an aid, not a
guarantee.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlparse

_PATTERNS = {
    "aws": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    "stripe": re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    "slack": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{8,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "openai_style": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
}
_SECRET_PARAMS = re.compile(
    r"^(?:api[_-]?key|key|token|access[_-]?token|secret|password|passwd|pwd|sig|signature|auth)$",
    re.IGNORECASE)


def _scan_text(text: Any, where: str) -> list[dict[str, str]]:
    if not isinstance(text, str) or not text:
        return []
    return [{"where": where, "kind": kind} for kind, pattern in _PATTERNS.items()
            if pattern.search(text)]


def _scan_url(url: Any, where: str) -> list[dict[str, str]]:
    if not isinstance(url, str) or not url:
        return []
    findings = _scan_text(url, where)
    try:
        parts = urlparse(url)
    except ValueError:
        return findings
    if parts.username or parts.password:
        findings.append({"where": where, "kind": "url_userinfo"})
    if any(_SECRET_PARAMS.match(name) for name, _value in parse_qsl(parts.query)):
        findings.append({"where": where, "kind": "url_secret_parameter"})
    return findings


def scan_demo(spec: dict[str, Any], texts: dict[str, str]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for i, step in enumerate(spec.get("steps") or []):
        interaction = step.get("interaction") or {}
        target = interaction.get("target") or {}
        findings += _scan_text(step.get("annotation"), f"step {i} line")
        findings += _scan_url(step.get("pageUrl"), f"step {i} page URL")
        findings += _scan_text(step.get("userDirection"), f"step {i} voice")
        findings += _scan_text(step.get("userIntent"), f"step {i} voice")
        findings += _scan_text(target.get("text"), f"step {i} target text")
        value = interaction.get("value")
        if value and (target.get("type") == "password"
                      or "password" in str(target.get("selector", "")).lower()):
            findings.append({"where": f"step {i} typed value", "kind": "password_field"})
        else:
            findings += _scan_text(value, f"step {i} typed value")
    for name, text in sorted(texts.items()):
        findings += _scan_text(text, name)
    return findings
