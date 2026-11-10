"""Runs a backfill in small batches. After every batch the progress is saved in the SAME
transaction as the data, so after a stop or a crash it continues exactly where it left off."""
import time
from typing import Callable

import psycopg
from pydantic import BaseModel

from app.backfill.spec import BackfillSpec, render, validate_spec
from app.db.models import SchemaSnapshot

ADVISORY_BASE = 5_000_000  # one lock per backfill, so the same backfill never runs twice at once


class Backfill(BaseModel):
    id: int
    request: str
    summary: str
    table_name: str
    key_column: str
    set_clause: str
    where_clause: str
    batch_size: int
    status: str
    min_key: int | None = None
    max_key: int | None = None
    last_key: int | None = None
    rows_to_update: int = 0
    rows_updated: int = 0
    error: str | None = None

    def spec(self) -> BackfillSpec:
        return BackfillSpec(table=self.table_name, key=self.key_column,
                            set_clause=self.set_clause, where_clause=self.where_clause)


def get(conn, backfill_id: int) -> Backfill:
    row = conn.execute("SELECT * FROM dbarch.backfills WHERE id = %s", (backfill_id,)).fetchone()
    if not row:
        raise KeyError(f"No backfill with id {backfill_id}")
    return Backfill(**row)


def list_all(conn) -> list[Backfill]:
    return [Backfill(**r) for r in conn.execute("SELECT * FROM dbarch.backfills ORDER BY id DESC")]


def create(conn, request: str, spec: BackfillSpec, snapshot: SchemaSnapshot, actor: str, batch_size: int = 5000) -> Backfill:
    """Check the spec, count the rows it would change, and store it. Nothing is changed yet."""
    problem = validate_spec(spec, snapshot)
    if problem:
        raise ValueError(problem)
    if not 1 <= batch_size <= 100_000:
        raise ValueError("batch_size must be between 1 and 100000")
    t, k = f'"{spec.table}"', f'"{spec.key}"'
    low_high = conn.execute(f"SELECT min({k}) AS lo, max({k}) AS hi FROM {t}").fetchone()
    count = conn.execute(f"SELECT count(*) AS n FROM {t} WHERE ({spec.where_clause})").fetchone()["n"]
    row = conn.execute(
        """INSERT INTO dbarch.backfills (request, summary, table_name, key_column, set_clause, where_clause,
                                         batch_size, status, min_key, max_key, rows_to_update, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, %s) RETURNING id""",
        (request, spec.summary, spec.table, spec.key, spec.set_clause, spec.where_clause, batch_size,
         low_high["lo"], low_high["hi"], count, actor)).fetchone()
    return get(conn, row["id"])


def _status(conn, backfill_id: int, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE dbarch.backfills SET status = %s, error = %s, updated_at = now() WHERE id = %s",
                 (status, error, backfill_id))


def _one_batch(conn, b: Backfill, low: int, high: int, retries: int, retry_wait: float) -> int:
    for attempt in range(1, retries + 1):
        try:
            with conn.transaction():  # the data change and the saved progress succeed or fail together
                n = conn.execute(render(b.spec(), low, high)).rowcount
                conn.execute("UPDATE dbarch.backfills SET last_key = %s, rows_updated = rows_updated + %s, "
                             "updated_at = now() WHERE id = %s", (high, n, b.id))
            return n
        except psycopg.errors.LockNotAvailable:
            if attempt == retries:
                raise
            time.sleep(retry_wait)
    return 0


def run(conn, backfill_id: int, sleep: float = 0.2, max_batches: int | None = None, retries: int = 3,
        retry_wait: float = 2.0, lock_timeout: str = "3s",
        progress: Callable[[Backfill], None] | None = None) -> Backfill:
    """Run (or continue) a backfill. `conn` is an admin connection with autocommit on."""
    b = get(conn, backfill_id)
    if b.status not in ("PENDING", "PAUSED", "FAILED"):
        raise ValueError(f"Backfill {backfill_id} is {b.status}. This step needs: PENDING or PAUSED or FAILED.")
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (ADVISORY_BASE + backfill_id,)).fetchone()["got"]:
        raise RuntimeError(f"Backfill {backfill_id} is already running somewhere else.")
    try:
        _status(conn, backfill_id, "RUNNING")
        conn.execute(f"SET lock_timeout = '{lock_timeout}'")
        conn.execute("SET statement_timeout = '60s'")
        b = get(conn, backfill_id)
        last = b.last_key if b.last_key is not None else (b.min_key - 1 if b.min_key is not None else 0)
        batches = 0
        try:
            while b.max_key is not None and last < b.max_key:
                if max_batches is not None and batches >= max_batches:
                    _status(conn, backfill_id, "PAUSED")
                    return get(conn, backfill_id)
                high = last + b.batch_size
                _one_batch(conn, b, last, high, retries, retry_wait)
                last, batches = high, batches + 1
                if progress:
                    progress(get(conn, backfill_id))
                time.sleep(sleep)  # let the database breathe
            _status(conn, backfill_id, "DONE")
        except psycopg.Error as e:
            _status(conn, backfill_id, "FAILED", f"{type(e).__name__}: {e}".strip())
        except KeyboardInterrupt:  # Ctrl+C = pause; run it again to continue
            _status(conn, backfill_id, "PAUSED")
            raise
    finally:
        conn.execute("RESET lock_timeout")
        conn.execute("RESET statement_timeout")
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_BASE + backfill_id,))
    return get(conn, backfill_id)
