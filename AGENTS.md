# AGENTS.md — sunsponge-capture

<!-- lane-start -->
## Start here (RHOBEAR lanes, 2026-10-24)

This repo belongs to lane **L8c Captur'd**. Before you change anything, read in `rhobear-north-star` (`docs/north-star/lanes/`): `README.md`, `GATE.md`, then `L8c-capturd/BRIEF.md` and `L8c-capturd/SOURCES.md`.

- What wins when sources disagree: the owner's newest words, then the final iteration (the newest board and the newest
  code), then the docs, this file included.
- No worker starts here until the lane architect's gate is signed and a worker brief names your files.
- Old UX never comes back: a screen, style, font, colour or mark that is not on the current board is deleted, not
  restyled. Fonts: `docs/north-star/FONTS.md` in `rhobear-north-star` (Nacelle is banned).
- Workers run only on Core, Peak, Summit or Guest. No keys in prompts, files or commits.
<!-- /lane-start -->

<!-- RHOBEAR-CANON:OPERATING-DISCIPLINE v1 — source of truth: rhobear-app/docs/canon/OPERATING-DISCIPLINE.md -->
## OPERATING DISCIPLINE — RHOBEAR CANON v1 (every agent, every spawn)
Binds you whether spun up as worker, crew agent, Pi step, or courier. Read first.

1. CODEGRAPH FIRST. Before grep/glob/find/read-to-locate, query CodeGraph
   (code-graph-mcp) — it has the symbol/dependency/route answer at a fraction of the
   tokens. Grep/glob are the FALLBACK. If CodeGraph is unreachable, say so in one
   line, then fall back.
2. ONE AGENT. ONE THREAD. ONE STEP. Never spawn subagents. No Agent/Task fan-out, no
   parallel instances in-thread. Work like Pi: smallest next step toward the goal,
   finish it, then the next. No speculative scaffolding. If the job is bigger than
   one agent should hold, STOP and hand back a note — don't recruit.
3. LEAN TOKENS. Let RTK-G (rtk) compress noisy tool output. Speak caveman-terse by
   default; keep every path/command/error string exact. (Exception: reporting to the
   owner in a planning thread — stay readable.)
4. KNOW WHERE YOU ARE. You are one of many agents across local + 3 VMs (GCP
   chat-brain-1, Hetzner rhobear-vps, Azure rhobear-bld-01). Consult docs/canon/
   before assuming where anything lives. One project = one repo.
5. STAY IN YOUR LANE. Do only the lane you were given. Out-of-lane → flag, don't absorb.
<!-- /RHOBEAR-CANON -->

This repo = **Captur'd by Sun Sponge** — post-work product capture (one lane, two modes):

* **`capturd shots`** — rested-state bulk website screenshots (Playwright). Settled, animation-free full-page shots across viewports and color schemes.
* **`capturd walk`** — agent-made interactive product demos. Live DOM via rrweb (not stale screenshots), semantic zoom via panzoom, voice-synced camera (TTS word timestamps → keyframe alignment), content-mode auto-detection (DOM / video / hybrid) so canvas / Three.js / HTML5 games get a video-mode fallback instead of a silent break. **Push-to-talk voice input (Whisper)** so you TALK to the agent while it walks — "top left is X, pan in here before the click." **Workflow mode** — agent watches you click each step, asks between clicks what you're illustrating, extracts intent via voice dialog.

Both modes are exposed via one MCP surface (`capturd serve`) so any RHOBEAR agent (Plans, social manager, support, pi) drives them through `capture.*` / `demo.*` tools. Editing IS the API — the agent is the editor; there is no GUI editor as v1. See `README.md` for the pitch, `capturd/walk/coordinator.py` for the glue, `capturd/walk/schema.py` for the DemoSpec contract.

Do **not** silently expand the lane further. Analytics dashboards, user accounts, hosted CDN, demo hub CMS — all out of lane. Flag, don't absorb.