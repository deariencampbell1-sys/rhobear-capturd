# Pathway Manifest — named controls

A two-pathway map whose first pathway names a control (`#open-settings`) and
whose second is marked UNWIRED — planned and flagged, never dropped.

## Summary Counts

| Category | Count |
|---|---|
| **Total pathways** | 2 |
| **WIRED** | 1 |
| **UNWIRED** | 1 |

## 1. Pathways Table

| id | location | trigger | declared_intent | handler | downstream_call | expected_side_effect | evidence | status | selector |
|---|---|---|---|---|---|---|---|---|---|
| `open-settings` | App.jsx:12 | `click` Settings btn | Open the settings panel | `openSettings` | `GET /settings` | Settings panel shown | App.jsx:12 | WIRED | `#open-settings` |
| `open-missing` | App.jsx:20 | `click` Missing btn | Open a panel that is not wired | `openMissing` | `GET /missing` | Panel shown | App.jsx:20 | UNWIRED | `#missing-btn` |

## 2. Routes Table

### FastAPI

| Method | Path | Handler file:line |
|---|---|---|
| GET | `/settings` | api.py:10 |
