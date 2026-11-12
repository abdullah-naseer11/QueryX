"""Uses real Docker. Skipped automatically if Docker is not running."""
import os

import docker
import pytest

from app.sandbox.runner import LABEL, dry_run


def docker_is_running() -> bool:
    if os.getenv("SKIP_DOCKER_TESTS"):
        return False
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not docker_is_running(), reason="Docker is not running")


def leftover_sandboxes():
    return docker.from_env().containers.list(all=True, filters={"label": "dbarch=sandbox"})


def test_dry_run_in_a_real_sandbox():
    r = dry_run("ALTER TABLE users ADD COLUMN tier text;", "ALTER TABLE users DROP COLUMN tier;")
    assert r.status == "ok"
    assert [c.kind for c in r.migration_diff.changes] == ["column_added"]


def test_bad_migration_is_reported_and_sandbox_is_deleted():
    r = dry_run("ALTER TABLE no_such_table ADD COLUMN x text;", "SELECT 1;")
    assert r.status == "error" and r.error_at == "migration"
    assert leftover_sandboxes() == []


def test_sandbox_is_deleted_after_success():
    dry_run("ALTER TABLE users ADD COLUMN tier text;", "ALTER TABLE users DROP COLUMN tier;")
    assert leftover_sandboxes() == []
