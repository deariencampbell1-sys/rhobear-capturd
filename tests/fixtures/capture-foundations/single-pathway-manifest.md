# Pathway Manifest — single

A one-pathway map whose pathway resolves to the built HTML root, so the capture
matrix is exactly viewports × schemes for one page. Used by the agent-API flow
tests to keep the shot matrix deterministic.

## Summary Counts

| Category | Count |
|---|---|
| **Total pathways** | 1 |
| **WIRED** | 1 |

## 1. Pathways Table

| id | location | trigger | declared_intent | handler | downstream_call | expected_side_effect | evidence | status |
|---|---|---|---|---|---|---|---|---|
| `home` | App.jsx:1 | `load` | Render the home view | `render` |  | Home painted | App.jsx:1 | WIRED |
