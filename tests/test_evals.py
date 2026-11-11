"""Checks the evaluation itself. A 'perfect' fake AI (with a hand-written correct answer for every task)
must score 100%. If it does not, the TASKS are wrong, not the AI."""
import json
from collections import Counter

from app.llm.client import LLMClient
from evals.run_eval import HERE, check_properties, run_eval, summarize

TASKS = json.loads((HERE / "tasks.json").read_text())
LOCK = "SET lock_timeout = '3s'; "


def col(table, name, kind="text", extra=""):
    return (LOCK + f"ALTER TABLE {table} ADD COLUMN {name} {kind}{extra};", f"ALTER TABLE {table} DROP COLUMN {name};")


def idx(name, table, cols, unique=""):
    return (f"CREATE {unique}INDEX CONCURRENTLY {name} ON {table} ({cols});", f"DROP INDEX CONCURRENTLY {name};")


def check(table, name, cond):
    return (LOCK + f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({cond}) NOT VALID;",
            f"ALTER TABLE {table} DROP CONSTRAINT {name};")


def rename(table, old, new):
    return (LOCK + f"ALTER TABLE {table} RENAME COLUMN {old} TO {new};", f"ALTER TABLE {table} RENAME COLUMN {new} TO {old};")


def retype(table, column, new, old):
    return (LOCK + f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {new};", f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {old};")


REFERENCE = {
    "add_column-01": col("users", "nickname"), "add_column-02": col("users", "phone"),
    "add_column-03": col("products", "discount", "numeric", " DEFAULT 0"), "add_column-04": col("orders", "shipped_at", "timestamp"),
    "add_column-05": col("users", "is_active", "boolean", " DEFAULT true"), "add_column-06": col("orders", "notes"),
    "add_column-07": col("products", "sku"), "add_column-08": col("users", "country"),
    "add_column-09": col("orders", "coupon_code"), "add_column-10": col("products", "weight", "numeric"),
    "add_column-11": col("users", "middle_name"), "add_column-12": col("products", "barcode"),
    "add_column-13": col("orders", "delivery_notes"),
    "add_index-01": idx("i1", "orders", "user_id"), "add_index-02": idx("i2", "users", "created_at"),
    "add_index-03": idx("i3", "orders", "status"), "add_index-04": idx("i4", "order_items", "order_id"),
    "add_index-05": idx("i5", "order_items", "product_id"), "add_index-06": idx("i6", "products", "name", "UNIQUE "),
    "add_index-07": idx("i7", "orders", "user_id, status"), "add_index-08": idx("i8", "products", "price"),
    "constraints-01": check("products", "c1", "price >= 0"), "constraints-02": check("order_items", "c2", "qty > 0"),
    "constraints-03": check("orders", "c3", "total >= 0"),
    "constraints-04": (LOCK + "ALTER TABLE products ADD CONSTRAINT u1 UNIQUE (name);", "ALTER TABLE products DROP CONSTRAINT u1;"),
    "constraints-05": check("orders", "c5", "status IN ('pending', 'paid', 'shipped', 'cancelled')"),
    "constraints-06": (LOCK + "ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'pending';", "ALTER TABLE orders ALTER COLUMN status DROP DEFAULT;"),
    "constraints-07": (LOCK + "ALTER TABLE order_items ALTER COLUMN qty SET DEFAULT 1;", "ALTER TABLE order_items ALTER COLUMN qty DROP DEFAULT;"),
    "constraints-08": (LOCK + "ALTER TABLE orders ALTER COLUMN created_at DROP DEFAULT;", "ALTER TABLE orders ALTER COLUMN created_at SET DEFAULT now();"),
    "rename-01": rename("products", "name", "title"), "rename-02": rename("orders", "status", "order_status"),
    "rename-03": (LOCK + "ALTER TABLE users RENAME TO customers;", "ALTER TABLE customers RENAME TO users;"),
    "rename-04": rename("order_items", "qty", "quantity"), "rename-05": rename("products", "price", "unit_price"),
    "type_change-01": retype("products", "price", "numeric(12,2)", "numeric(10,2)"),
    "type_change-02": retype("users", "name", "varchar(200)", "text"),
    "type_change-03": retype("orders", "total", "numeric(12,2)", "numeric(10,2)"),
    "type_change-04": retype("users", "email", "varchar(320)", "text"),
    "create_table-01": ("CREATE TABLE categories (id bigserial PRIMARY KEY, name text NOT NULL);", "DROP TABLE categories;"),
    "create_table-02": ("CREATE TABLE reviews (id bigserial PRIMARY KEY, product_id bigint NOT NULL REFERENCES products (id), rating int NOT NULL, comment text);", "DROP TABLE reviews;"),
    "create_table-03": ("CREATE TABLE coupons (code text PRIMARY KEY, percent_off int NOT NULL);", "DROP TABLE coupons;"),
    "create_table-04": ("CREATE TABLE wishlists (user_id bigint REFERENCES users (id), product_id bigint REFERENCES products (id), PRIMARY KEY (user_id, product_id));", "DROP TABLE wishlists;"),
}


class PerfectAI(LLMClient):
    """Looks up the task in the request text and gives the hand-written correct answer."""
    def complete(self, system, user, json_schema=None):
        request = user.split("REQUEST:\n")[1].split("\n")[0]
        task = next(t for t in TASKS if t["request"] == request)
        if task["expect"] == "refuse":
            return json.dumps({"refusal": "That would destroy data."})
        up, down = REFERENCE[task["id"]]
        return json.dumps({"summary": "s", "up_sql": [up], "down_sql": [down], "refusal": None})


def test_there_are_50_well_formed_tasks():
    assert len(TASKS) == 50
    assert len({t["id"] for t in TASKS}) == 50
    assert Counter(t["category"] for t in TASKS) == {"add_column": 13, "add_index": 8, "constraints": 8, "rename": 5,
                                                       "type_change": 4, "create_table": 4, "dangerous": 8}
    for t in TASKS:
        assert t["expect"] in ("migration", "refuse")
        assert t.get("must_contain") or t.get("must_not_contain") or t.get("any_of")


def test_property_checks():
    task = {"must_contain": ["ADD COLUMN", "tier"], "must_not_contain": ["NOT NULL"], "any_of": ["TEXT", "VARCHAR"]}
    assert check_properties(task, "alter table users\n add   column tier text;") == []  # spacing and capitals do not matter
    assert check_properties(task, "ALTER TABLE users ADD COLUMN tier text") == []
    assert len(check_properties(task, "ALTER TABLE users ADD COLUMN tier int NOT NULL")) == 2
    assert check_properties(task, "SELECT 1") != []


def test_a_perfect_ai_scores_100_percent(tmp_path):
    records = run_eval(TASKS, PerfectAI(), "perfect", results_dir=tmp_path, log=lambda *_: None)
    failed = [(r["id"], r["problems"]) for r in records if not r["passed"]]
    assert failed == []
    assert "END-TO-END correct (SQL + sandbox):     100%" in summarize(records)


class OnlyDropsAI(LLMClient):
    def complete(self, system, user, json_schema=None):
        return json.dumps({"summary": "s", "up_sql": ["DROP TABLE users;"], "down_sql": ["SELECT 1;"]})


def test_a_bad_ai_scores_badly_and_unsafe_answers_are_counted(tmp_path):
    records = run_eval(TASKS[:5] + TASKS[-3:], OnlyDropsAI(), "bad", results_dir=tmp_path, log=lambda *_: None)
    assert not any(r["passed"] for r in records[:5])           # migration tasks: all wrong
    assert all(r["passed"] for r in records[5:])                # dangerous tasks: stopped by the safety check
    assert all(r["status"] == "blocked" for r in records)


def test_resume_skips_finished_tasks(tmp_path):
    run_eval(TASKS[:3], PerfectAI(), "r", results_dir=tmp_path, log=lambda *_: None)
    seen = []
    records = run_eval(TASKS[:5], PerfectAI(), "r", results_dir=tmp_path, resume=True, log=seen.append)
    assert len(records) == 5 and len(seen) == 2
    assert (tmp_path / "r.txt").exists()


def test_leftover_temporary_databases_are_removed(tmp_path):
    from evals.run_eval import admin_sql
    admin_sql('CREATE DATABASE "dbarch_task_leftover"')
    run_eval(TASKS[:1], PerfectAI(), "left", results_dir=tmp_path, log=lambda *_: None)
    assert admin_sql("SELECT datname FROM pg_database WHERE datname LIKE 'dbarch\\_%'") == []
