# Director rig

The Director-mode film crew: turns a shot-list JSON into a rendered demo video
via the Captur'd MCP engine (`CAPTURD_REPO`, default `/opt/sunsponge-capture`).

- `scout.py` — plans a shot list for a URL (AI-assisted, falls back to a
  generic scroll tour).
- `film.py` — executes a shot list: record → enrich → stylize → zoom/hold/
  spotlight/overlay → export.
- `paid_boot.py`, `finish.py`, `revoice.py`, `verify_frames.py` — support
  tooling for the same pipeline.

**Live location:** the service (`service/app/main.py`, `service/render_worker.py`)
shells these by path via `CAPTURD_RIG_DIR` (default `/opt/capturd-rig` on
rhobear-vps). On the box, `/opt/capturd-rig` is a symlink into this directory
so there is exactly one copy under version control — edit here, not there.

## 2026-07-22 fix: KeyError in film.py's adjusted()

`adjusted(act_i)` mapped a shot-list act index to a live engine step index via
`kept.index(act_to_engine[act_i])`. If an act's `demo.act` call never
completed (selector timeout under `skip_unresolved`) or its engine step got
trimmed as a stray SPA interaction, that act index has no live engine step —
and the old code raised `KeyError`/`ValueError`, crashing the whole render.
Content-dependent: reproduced on a rhobear.ai walkthrough (job `08a1b1ad8396`),
not on lab.html walkthroughs whose selectors always resolved.

Fixed: `adjusted()` now returns `None` and logs a warning instead of raising;
every call site (zoom/hold/spotlight/overlay) skips that one directive and
keeps rendering the rest of the shot instead of failing the whole job.
Verified against the exact original crash inputs (unit-level) and a fresh
live rhobear.ai walkthrough through the running service (end-to-end).
