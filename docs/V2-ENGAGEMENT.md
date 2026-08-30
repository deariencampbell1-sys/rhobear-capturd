# Captur'd V2 — Engagement Layer Architecture (implemented 2026-08-29)

This document records the V2 engagement layer as BUILT. Baseline: the 2026-08-29
canon survey (`origin/main @ 14a23ff`). Branch: `feat/v2-engagement-layer`.

## Lane boundaries (hard rules, unchanged)

- **Captur'd Core** (`capturd/`): capture, record, direct, edit, narrate, branch,
  render, export, interactive playback. Emits local events; never knows Frontman.
- **Hosted service** (`service/`): engagement — publication, sessions, telemetry,
  analytics, sharing. Owns the ONLY seam to Frontman (`service/app/frontman.py`).
- **Frontman** (`rhobear-sales-chat`): prospect identity + tracked-send
  attribution. Owns the opaque token. Captur'd never mints a second one.
- No CRM, no second contact DB, no demo-hub CMS, no buyer chatbot in this pass.

## Viewer event contract (Core → anywhere)

`window.dispatchEvent(new CustomEvent("capturd:event", {detail}))` plus the
bounded `window.__capturdEvents` ring buffer. **Core never sends network
traffic** — the self-contained viewer works fully offline (proven by a
network-aborting Playwright test). Suppressed entirely in export mode.

Vocabulary (13): `demo_open, demo_start, step_view, step_complete, step_replay,
branch_view, branch_select, cta_view, cta_click, demo_complete, demo_exit,
viewer_pause, viewer_resume`.

Payload: `{event, demoId, versionId, sessionId, stepId, branchId, choiceId?,
ctaId?, url?, elapsedMs, timestamp, device:{deviceClass, viewportW, viewportH,
reducedMotion}}`.

Suppression rules: `demo_start`/`demo_complete`/`demo_open` once per session;
re-entering a visited step = `step_replay` (not a second `step_view`); leaving a
visited step = `step_complete`; final step completion is expressed as
`demo_complete`.

## Hosted store (service SQLite, idempotent IF-NOT-EXISTS schema)

- `demos` — stable identity (`dm_<token_urlsafe(16)>`, globally URL-safe),
  owner, mutable `draft_spec`, `published_version_id` pointer.
- `demo_versions` — immutable frozen specs, per-demo monotonic
  `version_number`, UNIQUE(demo_id, version_number). Publishing NEVER mutates
  history; restoring copies INTO the draft.
- `viewer_sessions` — opaque client UUID key, device class, bounded
  UTM/source JSON, `attribution_token` (Frontman opaque token; bound ONCE per
  session; never returned to any public caller).
- `demo_events` — server-validated event rows (vocabulary + field bounds),
  indexed by (demo, event, at) and (session).

## Public surface (existence-hiding by construction)

- `POST /api/pub/events` — tolerant ingest: invalid events dropped, unknown
  demo ids answered with the identical `{"ok": true}`, per-session hourly
  volume cap. Analytics can never be in the prospect's way.
- `GET /api/pub/demos/{id}/viewer` + `GET /pub/d/{id}` — published playback.
  Unknown demo / unpublished / any error → identical 404. Token validity never
  changes a response; no contact data is derivable from a token.

## Frontman attribution flow (the adapter)

```
Frontman contact/send
  → (dashboard or bridge) Frontman mints opaque token        [Frontman's DB]
  → Captur'd share URL  (share.trackable = facade over the bridge)
  → viewer loads ?fm=… → event batches carry attributionToken
  → hosted ingest binds token to session (once) and stores detailed events
  → sales-significant signals return to Frontman:
      demo-open | demo-return | demo-complete | demo-cta | demo-branch
```

Detailed step behavior (views, replays, dropoff, engaged time) STAYS in
Captur'd. The adapter is fail-open (telemetry never blocks viewing), the
bridge origin is server-side config only (`FRONTMAN_BASE_URL`,
`FRONTMAN_BRIDGE_KEY`), and Frontman-side additions are minimal and separate
(`internal/capturd/mint`, `internal/capturd/signal` — implemented in the
Frontman repo's own commit).

## Analytics formulas (service/app/analytics.py)

sessions = anonymous viewer sessions (= unique viewers) · starts = sessions
with `demo_start` · completions = sessions with `demo_complete` ·
completion_rate = completions/starts · completion_pct per session =
(furthest step + 1)/steps_count · engaged_ms = viewer uptime at last event
(avg + median) · per-step reach = distinct sessions viewing the step · exits =
sessions whose furthest step is N without completion · dropoff% = exits/reach ·
replays = `step_replay` count · CTA conversion = clicks/views · branch
distribution + completion-by-branch · device-class completion · return_rate =
tokens with >1 session / tokens with ≥1 (attribution-based; None without).

## Branching V2 (upgrades the existing primitive — no second system)

`step.choices = [{id, label, destination, analyticsName?, variable?, ctaId?}]`
validated by `schema.validate_choices` (stable ids, bounded lengths, in-range
destinations). `demo.branch` MCP tool now takes exactly one of legacy
`altPath` / new `choices`. Viewer renders a keyboard-accessible choice screen,
emits `branch_view`/`branch_select`, applies `variable` to `STATE.vars`
(Phase 9 consumer), jumps to destination (nested choice steps compose; the
flow rejoins the linear sequence), and auto-advance waits at choice steps.
Old specs (no choices) render unchanged.

## Personalization (Phase 9)

`{{var | default:"x"}}` — text-only, sanitized keys/values, mandatory
fallbacks. Allowed fields only: title, narration (annotation), branch intro,
choice labels, CTA label. Recorded hotspot/product copy is NEVER templated.
Viewer applies via textContent (no injection surface); server-side resolution
happens in `/pub/d/{id}` for Frontman-attributed views — PII never rides in
public URLs.

## Voice layer (Vertex legacy removed)

`capturd/walk/voice_provider.py`: `VoiceProvider` boundary with
**PollyProvider** (AWS Polly neural; native word speech-marks →
`voiceoverWords`; both time and duration are ms) and a **NovaSonicProvider**
seam (honest refusal: streaming output lacks word timings — slots in later
without caller changes). Legacy Vertex voice names alias to Polly so existing
demos regenerate. Edge TTS stays the zero-config fallback. Switcher: rebuilt
studio + mobile voice pickers (Ruth/Matthew/Danielle/Olivia/Kevin, HD badge),
`voice.list` / `voice.preview` / `voice.synthesize` MCP tools. One voice per
demo.

## MCP surface

- Core (local, 29): the 23 original + `demo.audit` + `demo.optimize` +
  `demo.personalize` + `voice.list` + `voice.preview` + `voice.synthesize`.
- Hosted (39): core + mounted engagement sub-server — `demo.publish`,
  `demo.version.list`, `demo.version.restore`, `demo.audit.live` (deterministic
  + REAL analytics), `analytics.demo/session/compare/dropoff`, `share.create`,
  `share.trackable`. Ownership via the MCP proxy's `x-capturd-user` →
  contextvar; no context = refusal. Zero name collisions.

## Skills (workflows, not tools)

`skills/capturd-{demo-qa, brand-voice-editor, pacing-optimizer,
sales-demo-builder, prospect-personalizer, trackable-link-builder,
dropoff-analyzer, performance-digest, mobile-auditor, product-change-mapper,
stale-demo-detector, top-performer-extractor, voiceover-producer}` (+ the
existing `capturd-autopilot`).

## Test status (2026-08-29)

297 passed / 2 justified skips / 13 e2e (E2E fixture repaired: runs cleanly,
skips with reason when `RHOBEAR_GW_API_KEY` is unconfigured, full assertions
when it is). New suites: `test_viewer_events.py` (8), `test_service_engagement.py`
(12), `test_branching_v2.py` (17), `test_audit_optimize_personalize.py` (15),
`test_voice_provider.py` (10), `test_mcp_v2.py` (6).
