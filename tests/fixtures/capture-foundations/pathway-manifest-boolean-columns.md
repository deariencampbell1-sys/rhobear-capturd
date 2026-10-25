# Pathway Manifest — boolean columns

Polarity lives in the column, not in the word: a `wired` column whose cell says
`yes` means WIRED and `no` means UNWIRED, and an `unwired` header is read
exactly like `unwired flag`. The pathway rows below mention routes in prose —
only the summary cells may set routesCount.

## Summary Counts

| Category | Count |
|---|---|
| **Total pathways** | 3 |
| **WIRED** | 1 |
| **UNWIRED** | 2 |
| Routes | 1 |
| Plugins | 0 |

## 1. Pathways Table

| id | trigger | downstream_call | wired | unwired |
|---|---|---|---|---|
| `boolean-yes` | `onClick` | fans out to 2 routes | yes |  |
| `boolean-no` | `onClick` | fans out to 3 routes | no |  |
| `unwired-flagged` | `onClick` | calls 2 routes |  | ⚠ |
