# Pathway Manifest — sample

## Summary Counts

| Category | Count |
|---|---|
| **Total pathways** | 3 |
| **WIRED** | 2 |
| **UNWIRED** | 1 |

## 1. Pathways Table

| id | location | trigger | declared_intent | handler | downstream_call | expected_side_effect | evidence | status |
|---|---|---|---|---|---|---|---|---|
| `capture-start` | RestedCaptureView.jsx:260 | `onClick` Start btn | Launch capture job | `startCapture` | `POST /api/rested-captures/jobs` | Job started | RestedCaptureView.jsx:260 | WIRED |
| `capture-job-poll` | RestedCaptureView.jsx:57 | `setInterval` 900ms | Poll capture job status | `restedCaptureJob` | `GET /api/rested-captures/jobs/<job_id>` | Progress UI updated | RestedCaptureView.jsx:57 | WIRED |
| `catalog-search` | CatalogView.jsx:243 | `fetch(url + "/catalog")` | Fetch live catalog | `fetch` | UNCLEAR — no `/catalog` in api.py | Catalog populated | CatalogView.jsx:243 | UNWIRED |

## 2. Routes Table

### FastAPI

| Method | Path | Handler file:line |
|---|---|---|
| GET | `/setup` | api.py:956 |
| POST | `/api/rested-captures/jobs` | api.py:2700 |