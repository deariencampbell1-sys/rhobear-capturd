"""Demo audit (Phase 7) — deterministic structural checks + observed analytics.

Order of authority (per the V2 directive):

1. deterministic structural rules — always available, zero hallucination risk
2. observed engagement analytics — only when a real analytics payload is
   supplied; NEVER fabricated when absent
3. optional AI critique — a hook, not a dependency (the MCP layer may attach
   one; this module never invents behavioral conclusions itself)

Scores: each category starts at 100; findings subtract by severity
(high 25, warn 10, info 3), clamped to [0, 100]. Overall is the mean of
category scores. Findings carry concrete evidence so a human or agent can
verify every claim against the spec.
"""
from __future__ import annotations

from typing import Any, Optional

_SEVERITY_COST = {"high": 25, "warn": 10, "info": 3}

_CATEGORIES = (
    "pacing", "visual_framing", "narration", "interaction_clarity",
    "cta_structure", "mobile_experience", "branch_design", "engagement",
)

# Dwell / hold thresholds (ms) — mirrors the viewer's own pacing constants.
_MAX_STEP_DWELL_MS = 6000
_MAX_HOLD_KEYFRAME_MS = 3500
_MIN_TIMELINE_MS = 0


def _finding(category: str, severity: str, message: str, **evidence) -> dict:
    f = {"category": category, "severity": severity, "message": message}
    f.update({k: v for k, v in evidence.items() if v is not None})
    return f


def _step_dwell_ms(spec: dict, i: int) -> int:
    """Server-side mirror of the viewer's stepDwellMs floor logic."""
    step = (spec.get("steps") or [])[i]
    timeline_ms = 0
    tl = ((spec.get("aiAnnotations") or {}).get("animationTimeline") or [])
    for kf in tl:
        if kf.get("stepIndex") == i:
            timeline_ms += int(kf.get("duration") or 500)
    voice_ms = 0
    words = step.get("voiceoverWords")
    if words:
        voice_ms = int(words[-1].get("tEndMs") or 0) + 500
    elif step.get("voiceoverBase64") or step.get("voiceoverPath"):
        voice_ms = 3000
    return max(1900, timeline_ms + 300, voice_ms + 350)


def _hotspot_point(step: dict) -> Optional[tuple]:
    inter = step.get("interaction") or {}
    target = inter.get("target") or {}
    r = target.get("boundingRect")
    h = inter.get("hotspot") or {}
    if not r:
        return None
    xp = h.get("xPct", 50) if isinstance(h.get("xPct"), (int, float)) else 50
    yp = h.get("yPct", 50) if isinstance(h.get("yPct"), (int, float)) else 50
    try:
        return (r["x"] + r["width"] * xp / 100.0, r["y"] + r["height"] * yp / 100.0)
    except (KeyError, TypeError):
        return None


def audit_spec(spec: dict, *, analytics: Optional[dict] = None) -> dict:
    """Audit a DemoSpec (dict form). Deterministic; analytics optional."""
    findings: list[dict] = []
    steps = spec.get("steps") or []
    n = len(steps)
    tl = (spec.get("aiAnnotations") or {}).get("animationTimeline") or []

    # ---- pacing -----------------------------------------------------------
    if n < 3:
        findings.append(_finding(
            "pacing", "warn", f"Only {n} steps — demos under 3 steps rarely "
            "carry a narrative", steps=n))
    for i, step in enumerate(steps):
        dwell = _step_dwell_ms(spec, i)
        if dwell > _MAX_STEP_DWELL_MS:
            findings.append(_finding(
                "pacing", "warn",
                f"Step {i + 1} holds for {dwell / 1000:.1f}s — long static holds "
                "lose attention", step_id=f"step-{i}", dwell_ms=dwell))
        for kf in tl:
            if kf.get("stepIndex") == i and kf.get("action") == "hold" \
                    and int(kf.get("duration") or 0) > _MAX_HOLD_KEYFRAME_MS:
                findings.append(_finding(
                    "pacing", "info",
                    f"Step {i + 1} has a {int(kf['duration']) / 1000:.1f}s hold "
                    "keyframe — consider trimming", step_id=f"step-{i}"))
        if not any(kf.get("stepIndex") == i for kf in tl):
            findings.append(_finding(
                "pacing", "info",
                f"Step {i + 1} is fully static (no camera keyframes)",
                step_id=f"step-{i}"))

    # ---- narration / interaction clarity ----------------------------------
    for i, step in enumerate(steps):
        ann = (step.get("annotation") or "").strip()
        target_text = ((step.get("interaction") or {}).get("target") or {}).get("text")
        if not ann:
            findings.append(_finding(
                "narration", "warn",
                f"Step {i + 1} has no narration (annotation) — silent steps "
                "read as broken", step_id=f"step-{i}"))
        elif target_text and ann.strip().lower() == str(target_text).strip().lower():
            findings.append(_finding(
                "narration", "info",
                f"Step {i + 1} narration duplicates the visible hotspot copy "
                f"({target_text!r})", step_id=f"step-{i}"))
        inter = step.get("interaction") or {}
        if not inter.get("hotspot"):
            findings.append(_finding(
                "interaction_clarity", "warn",
                f"Step {i + 1} has no hotspot — viewers can't see where to look",
                step_id=f"step-{i}"))

    # ---- visual framing ----------------------------------------------------
    has_zoom = any(kf.get("action") in ("zoomTo", "zoomToFit") for kf in tl)
    has_spotlight = any(kf.get("action") == "spotlightOn" for kf in tl)
    if not has_zoom:
        findings.append(_finding(
            "visual_framing", "info",
            "No zoom keyframes anywhere — the whole demo stays wide"))
    if not has_spotlight:
        findings.append(_finding(
            "visual_framing", "info",
            "No spotlight keyframes — nothing directs the eye"))

    # ---- CTA structure -----------------------------------------------------
    has_cta = bool(spec.get("cta")) or any(
        c.get("ctaId") for s in steps for c in (s.get("choices") or []))
    if n and not has_cta:
        findings.append(_finding(
            "cta_structure", "warn",
            "No final CTA — the demo ends without a next step"))

    # ---- mobile experience --------------------------------------------------
    for i, step in enumerate(steps):
        pt = _hotspot_point(step)
        vp = spec.get("viewport") or {"width": 1440, "height": 900}
        if pt and vp.get("width"):
            frac = pt[0] / float(vp["width"])
            if frac < 0.10 or frac > 0.90:
                findings.append(_finding(
                    "mobile_experience", "warn",
                    f"Step {i + 1} interaction target sits at {frac:.0%} of the "
                    "viewport width — outside the mobile-safe crop (~390px "
                    "phones crop the edges)", step_id=f"step-{i}",
                    x_fraction=round(frac, 3)))

    # ---- branch design -------------------------------------------------------
    choice_steps = [i for i, s in enumerate(steps) if s.get("choices")]
    for i in choice_steps:
        for ch in steps[i].get("choices") or []:
            if ch.get("destination") == i:
                findings.append(_finding(
                    "branch_design", "high",
                    f"Choice {ch.get('id')!r} on step {i + 1} jumps to its own "
                    "step — dead-end loop", step_id=f"step-{i}",
                    choice_id=ch.get("id")))
    if not choice_steps and n >= 5:
        findings.append(_finding(
            "branch_design", "info",
            "Long demo with no viewer-facing choices — consider a path split"))

    # ---- engagement (ONLY from real analytics — never fabricated) ------------
    if analytics is not None:
        for s in analytics.get("steps") or []:
            if s.get("reach", 0) >= 3 and s.get("dropoff_pct", 0) >= 40:
                findings.append(_finding(
                    "engagement", "high",
                    f"{s['dropoff_pct']:.0f}% of viewers exit at {s['step_id']} "
                    f"({s['exits']} of {s['reach']} sessions)",
                    step_id=s["step_id"], dropoff_pct=s["dropoff_pct"],
                    exits=s["exits"], reach=s["reach"]))
        cr = analytics.get("completion_rate")
        if cr is not None and analytics.get("starts", 0) >= 5 and cr < 0.3:
            findings.append(_finding(
                "engagement", "warn",
                f"Completion rate is {cr:.0%} across {analytics['starts']} starts"))
    else:
        findings.append(_finding(
            "engagement", "info",
            "No engagement analytics supplied — behavioral findings unavailable "
            "(publish the demo and pass analytics to audit again)"))

    # ---- scores ---------------------------------------------------------------
    cat_findings: dict[str, list[dict]] = {c: [] for c in _CATEGORIES}
    for f in findings:
        cat_findings.setdefault(f["category"], []).append(f)
    scores = {}
    for c in _CATEGORIES:
        s = 100 - sum(_SEVERITY_COST.get(f["severity"], 5)
                      for f in cat_findings.get(c, []))
        scores[c] = max(0, min(100, s))
    overall = round(sum(scores.values()) / len(_CATEGORIES))

    # Highest-impact first: high severity, then largest category damage.
    ordered = sorted(
        findings,
        key=lambda f: ({"high": 0, "warn": 1, "info": 2}[f["severity"]],
                       -_SEVERITY_COST.get(f["severity"], 5)))

    return {
        "overall": overall,
        "scores": scores,
        "findings": ordered,
        "counts": {
            "high": sum(1 for f in findings if f["severity"] == "high"),
            "warn": sum(1 for f in findings if f["severity"] == "warn"),
            "info": sum(1 for f in findings if f["severity"] == "info"),
        },
        "behavioral_data": analytics is not None,
    }
