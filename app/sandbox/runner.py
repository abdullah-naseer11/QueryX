"""Sandbox dry-run: copy the database STRUCTURE into a throw-away Docker database,
run the migration and the rollback there, and report what happened.
The real database is only read (structure only). The sandbox is always deleted."""
import shlex
import time
from typing import Literal
from urllib.parse import urlparse

import docker
import psycopg
from docker.errors import DockerException
from pglast import parse_sql
from pglast.parser import ParseError
from psycopg.rows import dict_row
from pydantic import BaseModel

from app.config import DATABASE_URL, SANDBOX_IMAGE
from app.db.schema_diff import SchemaDiff, diff_schemas
from app.db.schema_reader import read_schema

LABEL = {"dbarch": "sandbox"}


class SandboxReport(BaseModel):
    status: Literal["ok", "mismatch", "error"]
    message: str = ""
    error_at: Literal["migration", "rollback"] | None = None
    up_seconds: float = 0.0
    down_seconds: float = 0.0
    migration_diff: SchemaDiff = SchemaDiff()   # what the migration changed
    leftover_diff: SchemaDiff = SchemaDiff()    # what the rollback failed to undo
    note: str = "Structure only: the sandbox has no table data, so timings are not production timings."


def split_statements(sql: str) -> list[str]:
    """Cut a script into single statements (CREATE INDEX CONCURRENTLY must run alone)."""
    try:
        raw = parse_sql(sql)
    except ParseError:
        return [sql]  # let the database report the syntax error
    return [sql[r.stmt_location: r.stmt_location + (r.stmt_len or len(sql))].strip() for r in raw]


def _run(conn: psycopg.Connection, sql: str) -> float:
    start = time.perf_counter()
    for statement in split_statements(sql):
        conn.execute(statement)
    return round(time.perf_counter() - start, 3)


def execute_dry_run(conn: psycopg.Connection, up_sql: str, down_sql: str) -> SandboxReport:
    """The core test. `conn` must be autocommit with dict rows, on a throw-away database."""
    conn.execute("SET statement_timeout = '60s'")
    before = read_schema(conn)
    try:
        up_seconds = _run(conn, up_sql)
    except psycopg.Error as e:
        return SandboxReport(status="error", error_at="migration", message=f"{type(e).__name__}: {e}".strip())
    after_up = read_schema(conn)
    try:
        down_seconds = _run(conn, down_sql)
    except psycopg.Error as e:
        return SandboxReport(status="error", error_at="rollback", up_seconds=up_seconds,
                             migration_diff=diff_schemas(before, after_up), message=f"{type(e).__name__}: {e}".strip())
    after_down = read_schema(conn)

    migration_diff, leftover = diff_schemas(before, after_up), diff_schemas(before, after_down)
    common = dict(up_seconds=up_seconds, down_seconds=down_seconds, migration_diff=migration_diff, leftover_diff=leftover)
    if migration_diff.is_empty:
        return SandboxReport(status="mismatch", message="The migration changed nothing.", **common)
    if not leftover.is_empty:
        return SandboxReport(status="mismatch", message="The rollback did not restore the original schema.", **common)
    return SandboxReport(status="ok", message="Migration and rollback both worked in the sandbox.", **common)


class Sandbox:
    """A throw-away PostgreSQL in Docker. Use with `with`; it is deleted at the end, whatever happens."""
    USER = PASSWORD = DB = "sandbox"

    def __enter__(self) -> "Sandbox":
        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerException as e:
            raise RuntimeError("Docker is not running. Open Docker Desktop and try again.") from e
        self.container = self.client.containers.run(
            SANDBOX_IMAGE, detach=True, labels=LABEL,
            environment={"POSTGRES_USER": self.USER, "POSTGRES_PASSWORD": self.PASSWORD, "POSTGRES_DB": self.DB},
            ports={"5432/tcp": None},                              # random free port on this computer
            mem_limit="512m", nano_cpus=1_000_000_000,             # at most 512 MB and 1 CPU
            extra_hosts={"host.docker.internal": "host-gateway"},  # lets the sandbox reach your real database
        )
        try:
            self._wait_until_ready()
        except Exception:
            self.container.remove(force=True, v=True)
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.container.remove(force=True, v=True)

    @property
    def url(self) -> str:
        self.container.reload()
        port = self.container.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        return f"postgresql://{self.USER}:{self.PASSWORD}@127.0.0.1:{port}/{self.DB}"

    def _wait_until_ready(self, seconds: int = 90) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                psycopg.connect(self.url, connect_timeout=2).close()
                return
            except (psycopg.Error, KeyError, TypeError):
                time.sleep(1)
        raise RuntimeError("The sandbox database did not start in time.")

    def copy_structure_from(self, source_url: str) -> None:
        """pg_dump --schema-only runs INSIDE the sandbox and reads the structure of the real database."""
        u = urlparse(source_url)
        host = "host.docker.internal" if u.hostname in ("localhost", "127.0.0.1") else u.hostname
        cmd = (f"set -o pipefail; pg_dump --schema-only --no-owner --no-privileges "
               f"-h {shlex.quote(host)} -p {u.port or 5432} -U {shlex.quote(u.username or '')} "
               f"-d {shlex.quote(u.path.lstrip('/'))} | psql -v ON_ERROR_STOP=1 -q -U {self.USER} -d {self.DB}")
        code, output = self.container.exec_run(["bash", "-c", cmd], environment={"PGPASSWORD": u.password or ""})
        if code != 0:
            raise RuntimeError(f"Could not copy the structure: {output.decode(errors='replace')[-500:]}")


def dry_run(up_sql: str, down_sql: str, source_url: str = DATABASE_URL) -> SandboxReport:
    with Sandbox() as sandbox:
        sandbox.copy_structure_from(source_url)
        with psycopg.connect(sandbox.url, autocommit=True, row_factory=dict_row) as conn:
            return execute_dry_run(conn, up_sql, down_sql)
