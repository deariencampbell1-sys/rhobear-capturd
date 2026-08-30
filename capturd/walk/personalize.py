"""Dynamic-variable personalization (Phase 9).

Template syntax (Supademo-flavored, deliberately minimal):

    See how {{business_name}} could handle incoming leads
    {{business_name | default:"your business"}}

Safety rules:

* Substitution is TEXT-ONLY. The viewer renders personalized fields with
  textContent (never innerHTML), so no variable can inject markup or script.
* Variable names are restricted to [A-Za-z_][A-Za-z0-9_]*; values are plain
  strings, length-bounded.
* Fallbacks are mandatory-by-convention: a variable without a match renders as
  its declared `default:` or as empty string — never as raw template syntax,
  never as an error.

Allowed fields (apply here): spec.name (title), step.annotation (narration),
choice step intro (annotation of a choice step — the "branch intro"),
choice.label, and spec.cta.label. Hotspot copy comes from the recorded product
itself and is NOT templated (it would lie about the product).

Server-side resolution: when attribution comes from Frontman, the hosted
service resolves variables from the opaque token server-side and renders the
spec before serving `/pub/d/{id}` — contact PII never rides in public URLs.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

_VAR_RE = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*default:\s*\"([^\"]*)\")?\s*\}\}")

_MAX_VALUE_LEN = 200
_MAX_VARS = 32


def sanitize_vars(vars: Mapping[str, Any] | None) -> dict[str, str]:
    """Coerce + bound a variable mapping. Unknown types stringify; keys must
    match the template grammar or they are dropped."""
    out: dict[str, str] = {}
    if not isinstance(vars, Mapping):
        return out
    for k, v in list(vars.items())[:_MAX_VARS]:
        if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            continue
        s = v if isinstance(v, str) else str(v)
        out[k] = s[:_MAX_VALUE_LEN]
    return out


def render_template(value: str, vars: Mapping[str, str] | None) -> str:
    """Substitute {{var}} / {{var | default:"x"}} tokens. Text-only."""
    if not isinstance(value, str) or "{{" not in value:
        return value
    safe = sanitize_vars(vars)

    def _sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in safe:
            return safe[name]
        return default if default is not None else ""

    return _VAR_RE.sub(_sub, value)


def has_variables(value: Any) -> bool:
    return isinstance(value, str) and bool(_VAR_RE.search(value))


def personalize_spec(spec: dict, vars: Mapping[str, str] | None) -> dict:
    """Return a COPY of the spec with variables applied to allowed fields.
    The input spec is never mutated (published versions are immutable)."""
    import copy

    out = copy.deepcopy(spec)
    if out.get("name"):
        out["name"] = render_template(out["name"], vars)
    for step in out.get("steps") or []:
        if step.get("annotation"):
            step["annotation"] = render_template(step["annotation"], vars)
        for ch in step.get("choices") or []:
            if ch.get("label"):
                ch["label"] = render_template(ch["label"], vars)
    cta = out.get("cta")
    if isinstance(cta, dict) and cta.get("label"):
        cta["label"] = render_template(cta["label"], vars)
    return out
