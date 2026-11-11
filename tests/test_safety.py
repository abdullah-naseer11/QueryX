import pytest

from app.safety.checker import check_sql

LOCK = "SET lock_timeout = '3s'; "


def rules(sql: str) -> set[str]:
    return {r.rule for r in check_sql(sql).results}


# (SQL, rule that must be reported, severity)
CATCH = [
    ("DROP TABLE users", "drop_table", "BLOCK"),
    ("drop table if exists users cascade", "drop_table", "BLOCK"),
    ("DROP   TABLE\n  users;", "drop_table", "BLOCK"),
    ("UPDATE users SET name = '' WHERE true", "update_without_where", "BLOCK"),
    ("DELETE FROM users WHERE 1 = 1", "delete_without_where", "BLOCK"),
    ("DROP SCHEMA public CASCADE", "drop_schema", "BLOCK"),
    ("DROP DATABASE shop", "drop_database", "BLOCK"),
    ("TRUNCATE users", "truncate", "BLOCK"),
    ("TRUNCATE TABLE users, orders", "truncate", "BLOCK"),
    ("DELETE FROM users", "delete_without_where", "BLOCK"),
    ("UPDATE users SET name = 'x'", "update_without_where", "BLOCK"),
    (LOCK + "ALTER TABLE users DROP COLUMN name", "drop_column", "WARN"),
    (LOCK + "ALTER TABLE users ALTER COLUMN name TYPE varchar(10)", "alter_column_type", "WARN"),
    (LOCK + "CREATE INDEX idx_a ON users (email)", "index_not_concurrent", "WARN"),
    (LOCK + "CREATE UNIQUE INDEX idx_b ON users (email)", "index_not_concurrent", "WARN"),
    (LOCK + "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (user_id) REFERENCES users (id)",
     "constraint_without_not_valid", "WARN"),
    (LOCK + "ALTER TABLE orders ADD CONSTRAINT ck CHECK (total >= 0)",
     "constraint_without_not_valid", "WARN"),
    (LOCK + "ALTER TABLE users ADD COLUMN tier text NOT NULL", "not_null_without_default", "WARN"),
    ("ALTER TABLE users ADD COLUMN tier text", "missing_lock_timeout", "WARN"),
    ("CREATE INDEX CONCURRENTLY idx_c ON users (email)", "missing_lock_timeout", "WARN"),
    ("SELECT 1 FROM", "parse_error", "BLOCK"),
    ("not sql at all", "parse_error", "BLOCK"),
]


@pytest.mark.parametrize("sql,rule,severity", CATCH)
def test_rule_catches_it(sql, rule, severity):
    found = [r for r in check_sql(sql).results if r.rule == rule]
    assert found, f"{rule} was not reported for: {sql}"
    assert found[0].severity == severity
    assert found[0].suggestion  # every problem comes with a safer way


# SQL that is fine and must NOT be flagged
SAFE = [
    LOCK + "ALTER TABLE users ADD COLUMN tier text",
    LOCK + "ALTER TABLE users ADD COLUMN tier text NOT NULL DEFAULT 'free'",
    LOCK + "CREATE INDEX CONCURRENTLY idx_d ON users (email)",
    LOCK + "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (user_id) REFERENCES users (id) NOT VALID",
    LOCK + "ALTER TABLE orders VALIDATE CONSTRAINT fk",
    "DELETE FROM users WHERE id = 5",
    "UPDATE users SET name = 'x' WHERE id = 5",
    "SELECT * FROM users",
    "UPDATE users SET name = 'x' WHERE 1 = 2",
]


@pytest.mark.parametrize("sql", SAFE)
def test_safe_sql_is_ok(sql):
    assert check_sql(sql).verdict == "OK", rules(sql)


def test_verdict_block_beats_warn():
    assert check_sql("DROP TABLE users; ALTER TABLE a ADD COLUMN b text").verdict == "BLOCK"


def test_verdict_warn():
    assert check_sql("ALTER TABLE users ADD COLUMN tier text").verdict == "WARN"


def test_each_statement_is_reported_separately():
    report = check_sql("SELECT 1; DROP TABLE users; TRUNCATE orders;")
    assert [r.rule for r in report.results] == ["drop_table", "truncate"]
    assert report.results[0].statement == "DROP TABLE users"


def test_capital_letters_and_spacing_do_not_matter():
    assert rules("dRoP    tAbLe   users") == rules("DROP TABLE users")
