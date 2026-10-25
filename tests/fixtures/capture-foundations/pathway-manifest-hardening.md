# Pathway Manifest — hardening

Exercise the real manifest shapes the parser must survive: a lone `⚠`
unwired-flag cell, a pipe inside a code span, a fenced code block containing a
`#` line, a pathways table keyed by a `pathway` column, and a routes row with
an empty Method cell.

## Summary Counts

| Category | Count |
|---|---|
| **Total pathways** | 4 |
| **WIRED** | 2 |
| **UNWIRED** | 1 |
| **MOCKED** | 1 |

## 1. Pathways Table

| id | location | trigger | declared_intent | handler | downstream_call | evidence | status | unwired flag |
|---|---|---|---|---|---|---|---|---|
| `capture-start` | A.jsx:1 | `onClick` | Launch capture | `startCapture` | `POST /api/captures` | A.jsx:1 | WIRED |  |
| `catalog-search` | B.jsx:2 | `fetch(url)` | Fetch catalog | `fetch` | `GET /api/catalog` | B.jsx:2 |  | ⚠ |
| `export-rows` | C.jsx:3 | `onClick` | Export rows | `exportRows` | `a || b` | C.jsx:3 | MOCKED |  |

```text
# sketch of plugins considered for the capture run
run_capture()
```

| id | pathway | trigger | status |
|---|---|---|---|
| `settings-open` | `/settings` | `onClick` gear | WIRED |

## 2. Routes Table

| Method | Path | Handler file:line |
|---|---|---|
| GET | `/api/captures` | api.py:10 |
|  | `/api/catalog` | api.py:20 |
