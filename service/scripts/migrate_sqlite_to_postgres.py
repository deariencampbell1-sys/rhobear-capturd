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

More than one container-local source may be backfilled into the same Postgres, so
``usage`` keys are namespaced per source file (``--source-id``, defaulted from the
source path — see ``_usage_key``); pass a distinct one per source, and the same
one when re-running a file whose path changed.

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


def _default_source_id(sqlite_path: Path) -> str:
    """Per-source identity for usage keys — 8 hex of the resolved source path.

    Must be distinct across *different* source files and stable for the *same*
    one: a rerun has to reproduce its keys (or it would duplicate every usage
    row), while two files must not (or the second file's events are skipped as
    duplicates). The resolved absolute path gives both — it is identical on a
    rerun of one file, and container-local files live at different paths. When
    it cannot be the identity (the same DB mounted at a different path, a copied
    file), pass ``--source-id`` explicitly.
    """
    return hashlib.sha256(str(sqlite_path.resolve()).encode("utf-8")).hexdigest()[:8]


def _usage_key(row: dict, source_id: str) -> str:
    """Idempotency key for a usage row (stable across processes AND re-runs).

    ``usage`` has no natural key, so a rerun needs a stable one. Key it on the
    row's explicit ``id`` primary key (selected as ``_rowid`` — see
    ``read_rows``), not just the second-resolution payload columns, so two
    distinct append-only events sharing ``(user_id, kind, n, at)`` — e.g. two
    events in the same second with the same ``n`` — are BOTH imported instead of
    being silently collapsed and undercounting usage/billing.

    The source column is an explicit ``INTEGER PRIMARY KEY AUTOINCREMENT``
    (see ``store._SCHEMA``), so it is monotonic, never recycled after a delete,
    and — unlike an implicit rowid — not renumbered by ``VACUUM``. That is what
    makes the key stable across migration re-runs rather than merely
    "stable until someone vacuums". Legacy source files whose ``usage`` table
    predates that column fall back to ``rowid`` (see ``read_rows``); ids are
    carried over from rowids by the store's one-shot rebuild, so a key computed
    either way is identical.

    ``source_id`` namespaces the key by *source file* (``_default_source_id``,
    or ``--source-id``). A sqlite file's ids restart at 1, so keying on the id
    alone makes two sources collide whenever they hold the same
    ``(user_id, kind, n, at)`` under the same id — e.g. one user hitting two
    containers in the same second with the same ``kind`` and ``n``. The second
    row would then be skipped as a duplicate (exit code 0, only visible as a
    ``duplicates`` count) and usage/billing silently undercounted. Re-runs of
    one file keep the same id, so they stay idempotent.

    Uses a fixed SHA-256 (not Python's seeded ``hash()``) so keys are identical
    across processes regardless of ``PYTHONHASHSEED``.
    """
    return hashlib.sha256(
        f"{source_id}|{row['_rowid']}|{row['user_id']}|{row['kind']}|{row['n']}|"
        f"{row['at']}".encode("utf-8")
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


def _resolve_key(table: str, row: dict, source_id: str) -> str:
    if table == "usage":
        return _usage_key(row, source_id)
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


def _usage_pk_column(conn) -> str:
    """The column that identifies a ``usage`` row in *this* source file.

    ``id`` (the explicit ``INTEGER PRIMARY KEY AUTOINCREMENT``) on a current
    store; ``rowid`` on a legacy file that predates the column — the rebuild in
    ``store._migrate_usage_pk()`` carries rowids over as ids, so both choices
    yield the same key for the same row.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage)")}
    return "id" if "id" in cols else "rowid"


def read_rows(conn, table: str, *, batch_size: int = 1000):
    """Yield the source rows as batches of dicts (never materializes the whole
    table — a production-sized sessions/usage table would otherwise OOM the
    migrator process). ``usage`` also selects its per-event identity column
    (explicit ``id``, or ``rowid`` on a pre-migration file) as ``_rowid`` for its
    idempotency key.
    """
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    columns = "*" if table != "usage" else f"{_usage_pk_column(conn)} AS _rowid, *"
    cur.execute(f"SELECT {columns} FROM {table}")
    try:
        while True:
            batch = cur.fetchmany(batch_size)
            if not batch:
                break
            yield [dict(r) for r in batch]
    finally:
        cur.close()


def _source_count(conn, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT count(*) FROM {table}")
    try:
        return int(cur.fetchone()[0])
    finally:
        cur.close()


def _open_source(sqlite_path: Path) -> sqlite3.Connection:
    """Open the sqlite source **read-only**, on one consistent snapshot.

    The source is usually the live service database, so the migrator opens it
    through a ``mode=ro`` URI: it has no business writing to the source (and a
    typo'd path must fail loudly, not be created empty and migrated from). One
    deferred read transaction is then held across every table, so the counts and
    the rows come from the same snapshot instead of a writer committing between
    ``_source_count`` and ``read_rows``.
    """
    conn = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    conn.execute("BEGIN")
    return conn


def migrate(
    sqlite_path: Path, sink, *, dry_run: bool = False, source_id: str | None = None
) -> dict[str, dict]:
    source_id = source_id or _default_source_id(sqlite_path)
    conn = _open_source(sqlite_path)
    report = {}
    try:
        for table in TABLES:
            before = sink.count(table)
            processed = rejected = 0
            source = _source_count(conn, table)
            for batch in read_rows(conn, table):
                # De-dupe WITHIN a batch only. A run-wide ``seen`` set grows with
                # the table and would reintroduce the OOM this batching exists to
                # avoid (reviewer finding). It is also redundant across batches:
                # every table but ``usage`` has a PRIMARY KEY, and usage's key is
                # its own unique id, so the destination's unique constraints (and
                # MemorySink's first-write-wins) catch any repeat — counted under
                # ``duplicates`` via source - imported - rejected.
                seen: set[str] = set()
                for row in batch:
                    key = _resolve_key(table, row, source_id)
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
    if not dry_run and report["usage"]["duplicates"] > 0:
        # A skipped usage row is silent undercounting unless the operator hears
        # about it: rerunning one file reports this too (expected — everything is
        # already there), so name the other cause explicitly.
        print(
            f"[capturd] usage: {report['usage']['duplicates']} duplicate usage row(s) "
            "skipped — if this was not a rerun of an already-migrated source, two "
            "sources are sharing one identity; re-run with a distinct --source-id",
            file=sys.stderr,
        )
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sqlite", default=str(config.DB_PATH))
    ap.add_argument(
        "--source-id",
        default=None,
        help="namespaces this source's usage keys; defaults to a hash of the "
             "source's resolved path — distinct per source file, stable per "
             "rerun (set it explicitly if the file moves)",
    )
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
        report = migrate(
            sqlite_path, sink, dry_run=args.dry_run, source_id=args.source_id
        )
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
