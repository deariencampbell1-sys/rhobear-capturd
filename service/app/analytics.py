"""Engagement analytics — computed from raw demo_events. API-first.

No dashboard is built here; this module exists so the numbers exist and are
testable. Formulas (documented so callers can cite them):

- sessions            count of viewer_sessions for the demo (version-filtered)
- unique_viewers      == sessions (viewers are anonymous; a session IS a viewer)
- starts              sessions that emitted demo_start
- completions         sessions that emitted demo_complete
- completion_rate     completions / starts               (0.0 when starts == 0)
- completion_pct      per session: (furthest step index seen + 1) / steps_count
                      averaged over sessions that reached >= 1 step
- engaged_ms          per session: viewer uptime at its last event (max elapsed_ms);
                      average + median over sessions with >= 1 event
- per-step reach      distinct sessions with step_view/step_replay at that step
- per-step exits      sessions whose furthest step was that step without
                      demo_complete; dropoff% = exits / reach
- replays             count of step_replay per step
- cta                 cta_view / cta_click counts + conversion = clicks/views
- branches            branch_select distribution by choice_id + completion by branch
- device completion    completion_rate by device_class
- return_rate         attributed tokens with >1 session / tokens with >=1
                      (None when there are no attributed sessions)

All queries are bounded to the demo's own rows. No PII exists in this data.
"""
from __future__ import annotations

import statistics
import sqlite3
import time
from typing import Optional

from . import config, store


def _rows(sql: str, args: tuple) -> list[dict]:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _step_num(step_id: Optional[str]) -> Optional[int]:
    if step_id and step_id.startswith("step-"):
        tail = step_id[5:]
        if tail.isdigit():
            return int(tail)
    return None


def demo_analytics(demo_id: str, version_id: Optional[str] = None) -> dict:
    session_filter = "WHERE s.demo_id = ?" + (" AND s.version_id = ?" if version_id else "")
    sargs = (demo_id, version_id) if version_id else (demo_id,)

    event_filter = "WHERE e.demo_id = ?" + (" AND e.version_id = ?" if version_id else "")
    eargs = (demo_id, version_id) if version_id else (demo_id,)

    sessions = _rows(f"SELECT id, device_class, attribution_token FROM viewer_sessions s "
                     f"{session_filter}", sargs)
    session_ids = [s["id"] for s in sessions]
    n_sessions = len(session_ids)

    events = _rows(
        f"SELECT session_id, event, step_id, choice_id, cta_id, elapsed_ms "
        f"FROM demo_events e {event_filter}", eargs)

    started = {e["session_id"] for e in events if e["event"] == "demo_start"}
    completed = {e["session_id"] for e in events if e["event"] == "demo_complete"}

    # --- per-session furthest step + engaged time ---------------------------
    furthest: dict[str, int] = {}
    engaged: dict[str, int] = {}
    for e in events:
        n = _step_num(e["step_id"])
        if n is not None and e["event"] in ("step_view", "step_complete", "step_replay"):
            furthest[e["session_id"]] = max(furthest.get(e["session_id"], -1), n)
        el = e.get("elapsed_ms")
        if isinstance(el, int) and el >= 0:
            engaged[e["session_id"]] = max(engaged.get(e["session_id"], 0), el)

    steps_count = 0
    ver = store.get_version(version_id) if version_id else store.get_published(demo_id)
    if ver:
        steps_count = int(ver.get("steps_count") or 0)

    reached = [s for s in session_ids if s in furthest]
    completion_pcts = []
    if steps_count > 0:
        for s in reached:
            completion_pcts.append(min(100.0, (furthest[s] + 1) / steps_count * 100.0))

    avg_engaged = (sum(engaged.values()) / len(engaged)) if engaged else 0.0
    median_engaged = float(statistics.median(engaged.values())) if engaged else 0.0

    # --- per-step reach / exits / dropoff / replays -------------------------
    reach: dict[int, set] = {}
    replays: dict[int, int] = {}
    for e in events:
        n = _step_num(e["step_id"])
        if n is None:
            continue
        if e["event"] in ("step_view", "step_replay"):
            reach.setdefault(n, set()).add(e["session_id"])
        if e["event"] == "step_replay":
            replays[n] = replays.get(n, 0) + 1

    last_step: dict[str, int] = {}
    for s, n in furthest.items():
        last_step[s] = n

    steps_stats = []
    max_step = max(furthest.values()) if furthest else -1
    for n in range(0, max_step + 1):
        r = len(reach.get(n, set()))
        exits = sum(1 for s, last in last_step.items()
                    if last == n and s not in completed)
        steps_stats.append({
            "step_id": f"step-{n}",
            "reach": r,
            "exits": exits,
            "dropoff_pct": round(exits / r * 100, 1) if r else 0.0,
            "replays": replays.get(n, 0),
        })

    # --- CTA + branches -----------------------------------------------------
    cta_views = sum(1 for e in events if e["event"] == "cta_view")
    cta_clicks = sum(1 for e in events if e["event"] == "cta_click")

    branch_dist: dict[str, int] = {}
    branch_sessions: dict[str, set] = {}
    for e in events:
        if e["event"] == "branch_select" and e.get("choice_id"):
            cid = e["choice_id"]
            branch_dist[cid] = branch_dist.get(cid, 0) + 1
            branch_sessions.setdefault(cid, set()).add(e["session_id"])
    branches = [{
        "choice_id": cid,
        "selections": n,
        "completed": len(branch_sessions[cid] & completed),
    } for cid, n in sorted(branch_dist.items(), key=lambda kv: -kv[1])]

    # --- device-class completion --------------------------------------------
    by_device = {}
    for dc in {s["device_class"] for s in sessions}:
        ids = {s["id"] for s in sessions if s["device_class"] == dc}
        started_d = len(ids & started)
        by_device[dc] = {
            "sessions": len(ids),
            "completion_rate": (round(len(ids & completed) / started_d, 3)
                                if started_d else 0.0),
        }

    # --- return sessions (attribution-based; anonymous returns are unknowable)
    tokens: dict[str, set] = {}
    for s in sessions:
        if s.get("attribution_token"):
            tokens.setdefault(s["attribution_token"], set()).add(s["id"])
    return_rate = None
    if tokens:
        returns = sum(1 for t in tokens.values() if len(t) > 1)
        return_rate = round(returns / len(tokens), 3)

    return {
        "demo_id": demo_id,
        "version_id": version_id,
        "generated_at": int(time.time()),
        "sessions": n_sessions,
        "unique_viewers": n_sessions,
        "starts": len(started),
        "completions": len(completed),
        "completion_rate": (round(len(completed) / len(started), 3) if started else 0.0),
        "avg_completion_pct": (round(sum(completion_pcts) / len(completion_pcts), 1)
                               if completion_pcts else 0.0),
        "avg_engaged_ms": int(avg_engaged),
        "median_engaged_ms": int(median_engaged),
        "steps": steps_stats,
        "cta": {
            "views": cta_views,
            "clicks": cta_clicks,
            "conversion_rate": (round(cta_clicks / cta_views, 3) if cta_views else 0.0),
        },
        "branches": branches,
        "devices": by_device,
        "return_rate": return_rate,
    }
