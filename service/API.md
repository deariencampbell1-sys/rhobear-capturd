# Captur'd Hosted Service — API contract (L5)

The HTTP surface the dashboard calls. `render_worker.py` already implements the render +
cost-cap core; this is the thin transport + gating that wraps it. There is no separate
Captur'd checkout: the plan comes from central auth, the monthly plan and bank are L2's.

## Endpoints

### `POST /api/generate`
Start a generation. **Auth required** (session cookie from central auth). Every plan gets the
same features; the rate limit, concurrency cap and cost cap still apply.
```json
{ "url": "https://app.example.com", "template": "saas-walkthrough",
  "aspect": "9:16", "brand": "#4f8cff", "intro": "My App", "voice": "en-US-GuyNeural" }
```
→ `202 { "job_id": "ab12cd34ef56", "status": "queued" }`
Server maps `{url, template}` → a shot list (Director brain), builds a `JobSpec` with the
caller's plan cap, enqueues `run_job`.

### `GET /api/jobs/:id`
Poll status. → `{ "job_id", "status": "queued|running|done|failed|capped", "detail" }`.
`capped` = hit the cost cap (over-budget or ran too long) — surfaced honestly, not a silent fail.

### `GET /api/jobs/:id/video`
Stream/redirect to the finished MP4 (CDN URL once storage is wired). 404 until `status==done`.

### `GET /api/me`
Current user + plan + usage (generations used / remaining). Powers the dashboard meters.

## Gating order (per request)
1. **Auth** — valid session, else 401.
2. **Load protection** — hourly rate limit and per-account concurrency cap → 429 + Retry-After.
3. **Cost cap** — `enforce_precheck` + wall-time timeout in `run_job` (always on).
4. **Render** — `run_job` → film.py rig → finish.py → store → return.

## What exists vs pending
- ✅ **Render + cost cap** — `render_worker.py`, tested.
- ⏳ **Transport** — FastAPI app wrapping the above (small).
- ⏳ **Auth** — Google OAuth (needs G1 client).
- ⏳ **Metering** — L2 reserve/settle per job (blocked on L2; GAPS-NOW B-L2).
- ⏳ **Storage/CDN** — push finished MP4s, return signed URLs.
- ⏳ **Auto shot-list** — Director brain maps `{url, template}` → shot (plugs into `POST /generate`).
