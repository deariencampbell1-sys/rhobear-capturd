#!/usr/bin/env python3
"""Captur'd sqlite -> Postgres migrator (ADR-0001 Stage 5).

The hosted service's durable state (users, sessions, jobs, usage, mcp_tokens)
lives in a sqlite file at CAPTURD_DATA_DIR/capturd.sqlite3 — container-local
durable state, which the runtime/container architecture requires moving to
Postgres. This tool backfills it idempotently, so it can be re-run until the
switch is declared complete.

Idempotency model: every upsert is ``INSERT ... ON CONFLICT DO NOTHING`` (no
conflict target column). That is intentionally *insert-only* — it never updates
an already-present target row, so a partially populated destination never gets
silently mutated on re-run. If you need to resync from a stale destination,
start from a clean target (fresh schema) and run once. Conflicting rows (same
primary key, or any other unique constraint such as a user email already present
under a different id — which would otherwise hard-fail a targeted ``ON CONFLICT
(id)``) are skipped and reported under ``duplicates``, never raised.

Usage:
  python service/scripts/migrate_sqlite_to_postgres.py --dry-run
  python service/scripts/migrate_sqlite_to_postgres.py
  python service/scripts/migrate_sqlite_to_postgres.py --sqlite /path/to/capturd.sqlite3

Sink: CAPTURD_DATABASE_URL (psycopg). ``--dry-run`` reports counts against an
*empty* destination (it does not read existing Postgres rows and does not create
the schema); use a real run for true/repeatable numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402  (service package on the repo root path)

TABLES = ("users", "mcp_tokens", "sessions", "jobs", "usage")

# Column list per table, shared by SQL generation and param mapping so the two
# can never drift apart. usage's source rows have no _key — it is derived from
# the rowid in _usage_key() and injected into the row before upsert. Transaction
# handling (and the connection close) live on the sink, so the write path is
# identical for the real driver and the in-memory/test sink.
_COLS = {
    "users": "id, email, plan, created_at",
    "mcp_tokens": "token, user_id, created_at",
    "sessions": "token, user_id, created_at",
    "jobs": "id, user_id, kind, status, output, detail, created_at",
    "usage": "_key, user_id, kind, n, at",
}


def _usage_key(row: dict) -> str:
    """Idempotency key for a usage row (stable across processes).

    usage has no natural key in sqlite, so a rerun needs a stable one. Key it on
    the sqlite ``rowid`` (not just the second-resolution payload columns) so two
    distinct append-only events sharing ``(user_id, kind, n, at)`` — e.g. two
    events in the same second with the same ``n`` — are BOTH imported instead of
    being silently collapsed and undercounting usage/billing. ``rowid`` is stable
    for a given (unchanged) sqlite file, so reruns still stay idempotent. Uses a
    fixed SHA-256 (not Python's seeded ``hash()``) so keys are identical across
    processes regardless of ``PYTHONHASHSEED``.
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


def sql_for(table: str) -> str:
    """INSERT ... ON CONFLICT DO NOTHING for one table (pure — no DB needed).

    No conflict-target column is declared on purpose: ``ON CONFLICT DO NOTHING``
    skips a row that collides on ANY unique constraint — the primary key *or* a
    non-PK unique column such as ``users.email``. A targeted clause (e.g.
    ``ON CONFLICT (id)``) would only cover the PK and would hard-fail on a new-id
    row whose email is already present; the bare form makes an idempotent re-run
    after an email change a clean skip instead of a duplicate-key crash.
    """
    cols = _COLS[table]
    placeholders = ", ".join(["%s"] * len(cols.split(",")))
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )


def params_for(table: str, row: dict) -> tuple:
    """Parameter ordering for ``sql_for(table)`` (pure — no DB needed)."""
    return tuple(row.get(c.strip()) for c in _COLS[table].split(","))


class Sink(Protocol):
    """What the migrator needs from the destination (structural protocol)."""

    def upsert(self, table: str, row: dict) -> None: ...

    def count(self, table: str) -> int: ...

    def commit(self) -> None: ...

    def init_schema(self) -> None: ...

    def close(self) -> None: ...


class MemorySink:
    """In-memory sink for tests and ``--dry-run``.

    ``upsert`` is first-write-wins (it never overwrites an existing key), matching
    the destination's ``ON CONFLICT DO NOTHING`` so dry-run/unit counts don't
    drift from what the real sink would do.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict]] = {}

    def upsert(self, table: str, row: dict) -> None:
        key = str(row.get("_key") or row.get("id") or row.get("token") or "")
        self.rows.setdefault(table, {}).setdefault(key, row)

    def count(self, table: str) -> int:
        return len(self.rows.get(table, {}))

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def init_schema(self) -> None:
        return None

    def close(self) -> None:
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

    def rollback(self) -> None:
        self._conn.rollback()

    def init_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(PG_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - closing is best-effort cleanup
            pass


def read_rows(conn, table: str):
    """Yield each source row as a dict, in batches (never materializes the whole
    table — a production-sized sessions/usage table would otherwise OOM the
    migrator process). usage also selects the implicit ``rowid`` as ``_rowid``
    for its per-event idempotency key.
    """
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    columns = "rowid AS _rowid, *" if table == "usage" else "*"
    cur.execute(f"SELECT {columns} FROM {table}")
    try:
        while True:
            batch = cur.fetchmany(1000)
            if not batch:
                break
            for r in batch:
                yield dict(r)
    finally:
        cur.close()


def _source_count(conn, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT count(*) FROM {table}")
    try:
        return int(cur.fetchone()[0])
    finally:
        cur.close()


def migrate(sqlite_path: Path, sink, *, dry_run: bool = False) -> dict[str, dict]:
    conn = sqlite3.connect(sqlite_path)
    report = {}
    try:
        for table in TABLES:
            before = sink.count(table)
            processed = rejected = 0
            seen: set[str] = set()
            source = _source_count(conn, table)
            for row in read_rows(conn, table):
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
            # One commit per table (not per row): keeps the run atomicish — a
            # failure mid-table rolls back that table's work on the real sink
            # instead of leaving N/2 rows in.
            if not dry_run:
                sink.commit()
            after = sink.count(table) if not dry_run else before + processed
            imported = max(after - before, 0)
            report[table] = {
                "source": source,
                "imported": imported,
                "rejected": rejected,
                # Rows that shared a key with an earlier row of this run, or were
                # already present in the destination (or hit another unique
                # constraint such as a duplicate email under a different id) —
                # all idempotently skipped, never repeated or raised.
                "duplicates": source - imported - rejected,
                "resulting": after,
            }
    finally:
        conn.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sqlite", default=str(config.DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.is_file():
        print(f"ERROR: sqlite source not found: {sqlite_path}", file=sys.stderr)
        return 2

    url = os.environ.get("CAPTURD_DATABASE_URL", "").strip()
    if not url and not args.dry_run:
        print("ERROR: CAPTURD_DATABASE_URL not set (and not --dry-run)", file=sys.stderr)
        return 2

    if args.dry_run and url:
        print(
            "NOTE: --dry-run reports counts against an EMPTY destination (it "
            "does not read existing Postgres rows and does not create the "
            "schema); use a real run for true/repeatable numbers.",
            file=sys.stderr,
        )

    # Construct the sink inside the try so a missing optional dependency
    # (psycopg) is reported cleanly instead of leaking an uncaught traceback.
    sink: Sink | None = MemorySink() if args.dry_run else None
    try:
        if not args.dry_run:
            sink = PostgresSink(url)
            sink.init_schema()
        report = migrate(sqlite_path, sink, dry_run=args.dry_run)
    except ModuleNotFoundError as exc:
        print(
            f"ERROR: missing dependency: {exc} — Postgres migration needs "
            f"'pip install -r requirements-postgres.txt'",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - report any failure cleanly, don't leak a traceback
        if sink is not None and not args.dry_run:
            try:
                sink.rollback()
            except Exception:  # noqa: BLE001 - best-effort; we're already failing
                pass
        print(f"ERROR: migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if sink is not None:
            sink.close()

    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
