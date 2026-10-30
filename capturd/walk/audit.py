"""Deterministic structural audit for Captur'd specs (W2-05c, neutral).

Ported from V2 with the prospect-CTA, engagement-analytics and personalization
layers removed: findings cite concrete spec evidence only (dwell ms, hold ms,
missing fields, geometry, choice destinations) and no behavioral conclusion is
ever invented — with no observed data the audit says so honestly instead of
guessing. Suggestions only; nothing here changes a spec or a revision.
"""

from __future__ import annotations

from typing import Any, Optional

_SEVERITY_COST = {"high": 25, "warn": 10, "info": 3}
_CATEGORIES = ("pacing", "visual_framing", "narration", "interaction_clarity", "mobile_experience",
               "branch_design", "engagement")

# Dwell / hold thresholds (ms) — mirrors the viewer's own pacing constants.
_MAX_STEP_DWELL_MS = 6000
_MAX_HOLD_KEYFRAME_MS = 3500
_DWELL_FLOOR_MS = 1900


def _int_ms(kf: dict) -> Optional[int]:
    """Integer duration in ms, else None (a bad value is reported, not raised)."""
    d = kf.get("duration")
    return d if isinstance(d, int) and not isinstance(d, bool) else None


def _finding(category: str, severity: str, message: str, **evidence: Any) -> dict:
    f = {"category": category, "severity": severity, "message": message}
    f.update({k: v for k, v in evidence.items() if v is not None})
    return f


def _timeline(spec: dict) -> list:
    tl = (spec.get("aiAnnotations") or {}).get("animationTimeline")
    return tl if isinstance(tl, list) else []


def _step_dwell_ms(spec: dict, i: int) -> int:
    """Server-side mirror of the viewer's step-dwell floor logic."""
    step = (spec.get("steps") or [])[i]
    timeline_ms = sum((d or 500) for kf in _timeline(spec)
                      if kf.get("stepIndex") == i
                      for d in (_int_ms(kf),) if d is not None)
    words = step.get("voiceoverWords")
    voice_ms = (int(words[-1].get("tEndMs") or 0) + 500 if words else
                3000 if (step.get("voiceoverBase64") or step.get("voiceoverPath")) else 0)
    return max(_DWELL_FLOOR_MS, timeline_ms + 300, voice_ms + 350)


def _hotspot_point(step: dict) -> Optional[tuple]:
    inter = step.get("interaction") or {}
    r = (inter.get("target") or {}).get("boundingRect")
    h = inter.get("hotspot") or {}
    if not isinstance(r, dict):
        return None
    xp = h.get("xPct", 50) if isinstance(h.get("xPct"), (int, float)) else 50
    yp = h.get("yPct", 50) if isinstance(h.get("yPct"), (int, float)) else 50
    try:
        return (r["x"] + r["width"] * xp / 100.0, r["y"] + r["height"] * yp / 100.0)
    except (KeyError, TypeError):
        return None


def audit_spec(spec: dict) -> dict:
    """Audit a DemoSpec (dict form). Deterministic; structure only."""
    findings: list[dict] = []
    steps = spec.get("steps") or []
    n = len(steps)
    tl = _timeline(spec)

    # ---- pacing --------------------------------------------------------
    for kf in tl:  # malformed durations are reported as findings, never a crash
        raw = kf.get("duration")
        if raw is None or _int_ms(kf) is not None:
            continue
        idx = kf.get("stepIndex")
        findings.append(_finding(
            "pacing", "warn",
            f"{kf.get('action')!r} keyframe has a non-integer duration ({raw!r}) — "
            "the viewer cannot schedule it; fix the timeline value",
            step_id=f"step-{idx}" if isinstance(idx, int) else "unknown",
            duration=raw))
    if 0 < n < 3:
        findings.append(_finding("pacing", "warn",
            f"Only {n} steps — demos under 3 steps rarely carry a narrative", steps=n))
    for i, step in enumerate(steps):
        dwell = _step_dwell_ms(spec, i)
        if dwell > _MAX_STEP_DWELL_MS:
            findings.append(_finding("pacing", "warn",
                f"Step {i + 1} holds for {dwell / 1000:.1f}s — long static holds lose attention",
                step_id=f"step-{i}", dwell_ms=dwell))
        for kf in tl:
            if kf.get("stepIndex") == i and kf.get("action") == "hold" \
                    and (d := _int_ms(kf)) and d > _MAX_HOLD_KEYFRAME_MS:
                findings.append(_finding("pacing", "info",
                    f"Step {i + 1} has a {d / 1000:.1f}s hold keyframe — consider trimming",
                    step_id=f"step-{i}", hold_ms=d))
        if not any(kf.get("stepIndex") == i for kf in tl):
            findings.append(_finding("pacing", "info",
                f"Step {i + 1} is fully static (no camera keyframes)", step_id=f"step-{i}"))

        # ---- narration / interaction clarity (same pass) ----------------
        ann = (step.get("annotation") or "").strip()
        target_text = ((step.get("interaction") or {}).get("target") or {}).get("text")
        if not ann:
            findings.append(_finding("narration", "warn",
                f"Step {i + 1} has no narration (annotation) — silent steps read as broken",
                step_id=f"step-{i}"))
        elif target_text and ann.lower() == str(target_text).strip().lower():
            findings.append(_finding("narration", "info",
                f"Step {i + 1} narration duplicates the visible hotspot copy ({target_text!r})",
                step_id=f"step-{i}"))
        if not (step.get("interaction") or {}).get("hotspot"):
            findings.append(_finding("interaction_clarity", "warn",
                f"Step {i + 1} has no hotspot — viewers can't see where to look", step_id=f"step-{i}"))

    if not any(kf.get("action") in ("zoomTo", "zoomToFit") for kf in tl):
        findings.append(_finding("visual_framing", "info",
            "No zoom keyframes anywhere — the whole demo stays wide"))
    if not any(kf.get("action") == "spotlightOn" for kf in tl):
        findings.append(_finding("visual_framing", "info",
            "No spotlight keyframes — nothing directs the eye"))

    # ---- mobile experience (geometry evidence) ---------------------------
    vp = spec.get("viewport") or {"width": 1440, "height": 900}
    for i, step in enumerate(steps):
        pt = _hotspot_point(step)
        if pt and vp.get("width"):
            frac = pt[0] / float(vp["width"])
            if frac < 0.10 or frac > 0.90:
                findings.append(_finding("mobile_experience", "warn",
                    f"Step {i + 1} interaction target sits at {frac:.0%} of the viewport width — "
                    "outside the mobile-safe crop (~390px phones crop the edges)",
                    step_id=f"step-{i}", x_fraction=round(frac, 3)))

    # ---- branch design ----------------------------------------------------
    choice_steps = [i for i, s in enumerate(steps) if s.get("choices")]
    for i in choice_steps:
        for ch in steps[i].get("choices") or []:
            if ch.get("destination") == i:
                findings.append(_finding("branch_design", "high",
                    f"Choice {ch.get('id')!r} on step {i + 1} jumps to its own step — dead-end loop",
                    step_id=f"step-{i}", choice_id=ch.get("id")))
    if not choice_steps and n >= 5:
        findings.append(_finding("branch_design", "info",
            "Long demo with no viewer-facing choices — consider a path split"))

    # ---- engagement: honest absence, never fabricated ----------------------
    findings.append(_finding("engagement", "info",
        "No observed engagement data — behavioral findings unavailable "
        "(this audit reports structure only)"))

    # ---- scores: high-impact findings first ----
    cat_findings: dict[str, list[dict]] = {c: [] for c in _CATEGORIES}
    for f in findings:
        cat_findings.setdefault(f["category"], []).append(f)
    scores = {c: max(0, min(100, 100 - sum(
        _SEVERITY_COST.get(f["severity"], 5) for f in cat_findings.get(c, []))))
        for c in _CATEGORIES}
    findings.sort(key=lambda f: {"high": 0, "warn": 1, "info": 2}[f["severity"]])

    return {"overall": round(sum(scores.values()) / len(_CATEGORIES)),
            "scores": scores, "findings": findings,
            "counts": {s: sum(1 for f in findings if f["severity"] == s)
                       for s in ("high", "warn", "info")},
            "behavioral_data": False}
