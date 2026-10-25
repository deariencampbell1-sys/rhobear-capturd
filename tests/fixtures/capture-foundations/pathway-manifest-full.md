# Pathway Manifest — full coverage fixture

## Summary Counts

| Category | Count |
|---|---|
| **Total pathways** | 1 |
| **WIRED** | 1 |

## 1. Pathways Table

| id | location | trigger | declared_intent | handler | downstream_call | expected_side_effect | evidence | status |
|---|---|---|---|---|---|---|---|---|
| `capture-start` | RestedCaptureView.jsx:260 | `onClick` Start btn | Launch capture job | `startCapture` | `POST /api/rested-captures/jobs` | Job started | RestedCaptureView.jsx:260 | WIRED |

## 2. Routes Table

| Method | Path | Handler file:line |
|---|---|---|
| POST | `/api/rested-captures/jobs` | api.py:2700 |

## 3. Plugins

| Name | Kind | Notes |
|---|---|---|
| rested-capture | core plugin | wires the capture surface |
| vault-sync | skill | syncs vault state |

## 4. Suspicious Calls

| Call | Location | Why it is risky |
|---|---|---|
| `eval(` | RestedCaptureView.jsx:101 | dynamic code execution |
| `fetch(BASE + "/catalog")` | CatalogView.jsx:243 | URL built from input |

## 5. Anti-patterns

| Category | Count | File | Line | Quote |
|---|---|---|---|---|
| duplicate-fetch | 3+ | CatalogView.jsx | 243 | `fetch(BASE + "/catalog")` |
| console-log-noise | ~12 | RestedCaptureView.jsx | 57 | `console.log(state)` |
| dead-export | n/a | hub.ts | 88 | `export const unusedHelper` |
