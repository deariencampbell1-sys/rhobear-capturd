#!/usr/bin/env python3
"""Captur'd sqlite -> Postgres migrator (ADR-0001 Stage 5).

The hosted service's durable state (users, sessions, jobs, usage, mcp_tokens)
lives in a sqlite file at CAPTURD_DATA_DIR/capturd.sqlite3 — container-local
durable state, which the runtime/container architecture requires moving to
Postgres. This tool backfills it idempotently (ON CONFLICT DO NOTHING), so it
can be re-run until the switch is declared complete.

Usage:
  python service/scripts/migrate_sqlite_to_postgres.py --dry-run
  python service/scripts/migrate_sqlite_to_postgres.py
  python service/scripts/migrate_sqlite_to_postgres.py --sqlite /path/to/capturd.sqlite3

Sink: CAPTURD_DATABASE_URL (psycopg). Tests pass their own in-memory sink.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402  (service package on the repo root path)

TABLES = ("users", "mcp_tokens", "sessions", "jobs", "usage")

# Column list per table, shared by SQL generation and param mapping so the two
# can never drift apart. usage's source rows have no _key — it is derived from
# the rowid in _usage_key() and injected into the row before upsert.
_COLS = {
    "users": "id, email, plan, created_at",
    "mcp_tokens": "token, user_id, created_at",
    "sessions": "token, user_id, created_at",
    "jobs": "id, user_id, kind, status, output, detail, created_at",
    "usage": "_key, user_id, kind, n, at",
}


def _usage_key(row: dict) -> str:
    """Idempotency key for a usage row.

    usage has no natural key in sqlite, so a rerun needs a stable one. Key it on
    the sqlite ``rowid`` (not just the second-resolution payload columns) so two
    distinct append-only events sharing ``(user_id, kind, n, at)`` — e.g. two
    events in the same second with the same ``n`` — are BOTH imported instead of
    being silently collapsed and undercounting usage/billing. ``rowid`` is stable
    for a given (unchanged) sqlite file, so reruns still stay idempotent.
    """
    return hashlib.sha256(
        f"{row['_rowid']}|{row['user_id']}|{row['kind']}|{row['n']}|{row['at']}".encode(
            "utf-8"
        )
    ).hexdigest()[:24]


PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free', created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS mcp_tokens (
  token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL, output TEXT DEFAULT '', detail TEXT DEFAULT '',
  created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
  _key TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 1, at BIGINT NOT NULL);
"""


def _conflict_col(table: str) -> str:
    """The column the ON CONFLICT target points at.

    usage keys on ``_key``; users/jobs on their primary key ``id``; token tables
    on ``token``. Notably users must conflict on ``id`` (the PK), NOT ``email``:
    an idempotent rerun after a user's email changed would otherwise try to
    re-insert a row with a duplicate ``id`` and hard-fail.
    """
    if table == "usage":
        return "_key"
    if table in ("users", "jobs"):
        return "id"
    return "token"


def sql_for(table: str) -> str:
    """INSERT ... ON CONFLICT DO NOTHING for one table (pure — no DB needed)."""
    cols = _COLS[table]
    placeholders = ", ".join(["%s"] * len(cols.split(",")))
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT ({_conflict_col(table)}) DO NOTHING"
    )


def params_for(table: str, row: dict) -> tuple:
    """Parameter ordering for ``sql_for(table)`` (pure — no DB needed)."""
    return tuple(row.get(c.strip()) for c in _COLS[table].split(","))


class Sink:  # pragma: no cover - protocol doc
    def upsert(self, table: str, row: dict) -> None: ...

    def count(self, table: str) -> int: ...

    def commit(self) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict]] = {}

    def upsert(self, table: str, row: dict) -> None:
        self.rows.setdefault(table, {})[str(row.get("_key") or row.get("id") or row.get("token"))] = row

    def count(self, table: str) -> int:
        return len(self.rows.get(table, {}))

    def commit(self) -> None:
        return None


def _resolve_key(table: str, row: dict) -> str:
    if table == "usage":
        return _usage_key(row)
    return str(row.get("id") or row.get("token") or "")


class PostgresSink:
    def __init__(self, url: str) -> None:
        import psycopg

        self._conn = psycopg.connect(url)

    def upsert(self, table: str, row: dict) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql_for(table), params_for(table, row))

    def count(self, table: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def commit(self) -> None:
        self._conn.commit()

    def init_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(PG_SCHEMA)
        self._conn.commit()


def read_rows(conn, table: str) -> list[dict]:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    # usage needs the implicit rowid for a per-event idempotency key; the other
    # tables are keyed on their own PK already.
    columns = "rowid AS _rowid, *" if table == "usage" else "*"
    rows = [dict(r) for r in cur.execute(f"SELECT {columns} FROM {table}").fetchall()]
    cur.close()
    return rows


def migrate(sqlite_path: Path, sink, *, dry_run: bool = False) -> dict[str, dict]:
    conn = sqlite3.connect(sqlite_path)
    report = {}
    for table in TABLES:
        rows = read_rows(conn, table)
        before = sink.count(table)
        processed = rejected = 0
        seen: set[str] = set()
        for row in rows:
            key = _resolve_key(table, row)
            if not key:
                rejected += 1
                continue
            if key in seen:
                continue
            seen.add(key)
            if table == "usage":
                row = {**row, "_key": key}
            if not dry_run:
                sink.upsert(table, row)
            processed += 1
        # One commit per table (not per row): keeps the run atomicish — a failure
        # mid-table rolls back that table's work instead of leaving N/2 rows in.
        if not dry_run:
            sink.commit()
        after = sink.count(table) if not dry_run else before + processed
        imported = max(after - before, 0)
        report[table] = {
            "source": len(rows),
            "imported": imported,
            "rejected": rejected,
            # Rows that shared an id with an earlier row of this run (or were
            # already present — both are idempotently skipped, never repeated).
            "duplicates": len(rows) - imported - rejected,
            "resulting": after,
        }
    conn.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", default=str(config.DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("CAPTURD_DATABASE_URL", "").strip()
    if not url and not args.dry_run:
        print("ERROR: CAPTURD_DATABASE_URL not set (and not --dry-run)", file=sys.stderr)
        return 2
    sink = MemorySink() if args.dry_run or not url else PostgresSink(url)
    if isinstance(sink, PostgresSink):
        sink.init_schema()
    report = migrate(Path(args.sqlite), sink, dry_run=args.dry_run)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
