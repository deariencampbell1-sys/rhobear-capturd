"""Safe demo optimization (Phase 8).

`optimize_spec` proposes deterministic, REVERSIBLE improvements and — only when
``apply=True`` — applies the SAFE subset. Every mutation is returned as a
before/after plan entry so it stays reviewable (the MCP tool defaults to
``apply=False``; the caller commits the result like any other edit).

SAFE (auto-applicable):
- copy shortening: annotation over the length budget, cut at a sentence
  boundary (never mid-word)
- caption cleanup: collapse whitespace runs, strip duplicated trailing
  punctuation, trim stray quotes
- pause trimming: HOLD keyframes longer than the budget are shortened to it
  (data-only change; versioning/publish makes it reversible in practice)

UNSAFE — never performed here, listed as approval-required when detected:
- deleting steps or branches, replacing media, changing product claims or
  pricing, publishing. Those need a human.
"""
from __future__ import annotations

import re
from typing import Any

_ANNOTATION_BUDGET = 140
_HOLD_BUDGET_MS = 3500

_WS_RUN = re.compile(r"\s{2,}")
_DUP_PUNCT = re.compile(r"([.!?])\1{2,}")
_TRAIL_QUOTE = re.compile(r"\s+[\"']+")


def _shorten(text: str) -> str:
    """Cut at the last sentence end inside the budget (never mid-word)."""
    budget = _ANNOTATION_BUDGET
    if len(text) <= budget:
        return text
    window = text[:budget]
    m = None
    for m in re.finditer(r"[.!?](?=\s|$)", window):
        pass
    if m and m.end() >= budget * 0.5:
        return text[: m.end()].rstrip()
    cut = window.rfind(" ")
    return (text[: cut if cut > 0 else budget]).rstrip() + "…"


def _clean_caption(text: str) -> str:
    t = _WS_RUN.sub(" ", text).strip()
    t = _DUP_PUNCT.sub(r"\1", t)
    t = _TRAIL_QUOTE.sub("", t)
    return t


def optimize_spec(spec: dict, *, apply: bool = False) -> dict:
    """Plan (and optionally apply) safe optimizations. Returns the plan."""
    plan: list[dict] = []
    steps = spec.get("steps") or []

    for i, step in enumerate(steps):
        ann = step.get("annotation")
        if isinstance(ann, str) and ann.strip():
            cleaned = _clean_caption(ann)
            if cleaned != ann:
                plan.append({"step_id": f"step-{i}", "action": "caption_cleanup",
                             "before": ann, "after": cleaned, "safe": True})
                if apply:
                    step["annotation"] = cleaned
                ann = cleaned
            if len(cleaned) > _ANNOTATION_BUDGET:
                shortened = _shorten(cleaned)
                if shortened != cleaned:
                    plan.append({"step_id": f"step-{i}", "action": "copy_shorten",
                                 "before": cleaned, "after": shortened, "safe": True})
                    if apply:
                        step["annotation"] = shortened
        elif ann is not None and not isinstance(ann, str):
            plan.append({"step_id": f"step-{i}", "action": "flag_only",
                         "before": ann, "after": ann, "safe": False,
                         "note": "annotation is not a string — needs human review"})

    tl = ((spec.get("aiAnnotations") or {}).get("animationTimeline"))
    if isinstance(tl, list):
        for kf in tl:
            if kf.get("action") == "hold" and isinstance(kf.get("duration"), int) \
                    and kf["duration"] > _HOLD_BUDGET_MS:
                plan.append({
                    "step_id": f"step-{kf.get('stepIndex')}",
                    "action": "pause_trim",
                    "before": kf["duration"],
                    "after": _HOLD_BUDGET_MS,
                    "safe": True,
                })
                if apply:
                    kf["duration"] = _HOLD_BUDGET_MS

    # Unsafe observations — reported, never touched.
    if len(steps) > 24:
        plan.append({"action": "approval_required", "safe": False,
                     "note": f"{len(steps)} steps — consider deleting weak steps "
                             "(requires approval)"})
    for i, step in enumerate(steps):
        for ch in step.get("choices") or []:
            if ch.get("destination") == i:
                plan.append({"action": "approval_required", "safe": False,
                             "note": f"choice {ch.get('id')!r} dead-ends at its own "
                                     f"step {i} — deleting/retargeting needs approval",
                             "step_id": f"step-{i}", "choice_id": ch.get("id")})

    safe = [p for p in plan if p.get("safe")]
    return {
        "plan": plan,
        "safe_count": len(safe),
        "applied": bool(apply),
        "spec": spec if apply else None,
    }
