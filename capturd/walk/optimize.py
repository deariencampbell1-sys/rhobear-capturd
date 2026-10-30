"""Safe demo optimization for Captur'd specs (W2-05c, neutral) — dry-run by default.

``optimize_spec`` proposes deterministic, REVERSIBLE improvements and returns
every mutation as a before/after plan entry. The input spec is NEVER mutated:
``apply=True`` returns an optimized DEEP COPY under ``"spec"`` for the caller
to persist — the MCP adapter writes that copy as a NEW draft through the 05b
revision store, so approved and published revisions stay byte-identical,
nothing is published, and the draft awaits review. SAFE: caption cleanup,
sentence-boundary copy shortening, bounded HOLD trims (never below the hold
floor — clamps reported). UNSAFE — never performed, reported as
``approval_required``: deleting steps/branches, replacing media, changing
claims/pricing, publishing.
"""

from __future__ import annotations

import copy
import re

_ANNOTATION_BUDGET = 140
_HOLD_BUDGET_MS = 3500
#: A trim never leaves a hold shorter than this — the plan reports the clamp.
_HOLD_FLOOR_MS = 1000

_WS_RUN = re.compile(r"\s{2,}")
_DUP_PUNCT = re.compile(r"([.!?])\1{2,}")
_TRAIL_QUOTE = re.compile(r"\s+[\"']+")


def _shorten(text: str) -> str:
    """Cut at the last sentence end inside the budget (never mid-word)."""
    if len(text) <= _ANNOTATION_BUDGET:
        return text
    window = text[:_ANNOTATION_BUDGET]
    m = None
    for m in re.finditer(r"[.!?](?=\s|$)", window):
        pass
    if m and m.end() >= _ANNOTATION_BUDGET * 0.5:
        return text[: m.end()].rstrip()
    cut = window.rfind(" ")
    return (text[: cut if cut > 0 else _ANNOTATION_BUDGET]).rstrip() + "…"


def _clean_caption(text: str) -> str:
    t = _WS_RUN.sub(" ", text).strip()
    t = _DUP_PUNCT.sub(r"\1", t)
    return _TRAIL_QUOTE.sub("", t)


def optimize_spec(spec: dict, *, apply: bool = False,
                  hold_budget_ms: int = _HOLD_BUDGET_MS) -> dict:
    """Plan (and optionally produce) safe optimizations. Returns the plan.

    ``spec`` is treated as immutable: with ``apply=True`` the result carries an
    optimized deep copy under ``"spec"``; the caller persists it (as a new draft)
    if it chooses. Nothing is published or approved here.
    """
    work = copy.deepcopy(spec) if apply else spec
    plan: list[dict] = []
    steps = work.get("steps") or []

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

    tl = (work.get("aiAnnotations") or {}).get("animationTimeline")
    if isinstance(tl, list):
        for kf in tl:
            if kf.get("action") != "hold" or not isinstance(kf.get("duration"), int):
                continue
            raw_target = min(kf["duration"], hold_budget_ms)
            target = max(raw_target, _HOLD_FLOOR_MS)
            if target >= kf["duration"]:
                continue  # nothing safe to trim
            plan.append({"step_id": f"step-{kf.get('stepIndex')}",
                         "action": "pause_trim", "before": kf["duration"],
                         "after": target, "safe": True,
                         "clamped": raw_target < _HOLD_FLOOR_MS})
            if apply:
                kf["duration"] = target

    # Unsafe observations — reported, never touched.
    for i, step in enumerate(steps):
        for ch in step.get("choices") or []:
            if ch.get("destination") == i:
                plan.append({"action": "approval_required", "safe": False,
                             "note": f"choice {ch.get('id')!r} dead-ends at its own "
                                     f"step {i} — deleting/retargeting needs approval",
                             "step_id": f"step-{i}", "choice_id": ch.get("id")})

    return {"plan": plan, "safe_count": sum(1 for p in plan if p.get("safe")),
            "applied": bool(apply), "spec": work if apply else None}
