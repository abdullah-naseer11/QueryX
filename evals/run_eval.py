"""Measures how well the agent works: runs every task in tasks.json and counts successes.
  python -m evals.run_eval --label v1                  (all 50 tasks)
  python -m evals.run_eval --label test --limit 5      (just the first 5)
  python -m evals.run_eval --label v1 --resume         (continue after a stop)

Each migration task must: give valid JSON, pass the safety check, contain the expected SQL parts,
and work in a temporary database (migration AND rollback). Dangerous tasks must be refused or blocked.
Tests run in temporary databases on your local PostgreSQL (same check as the Docker sandbox)."""
import argparse
import json
import time
import uuid
from collections import defaultdict
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.db.schema_reader import read_schema, to_prompt_text
from app.llm.agent import join_sql, propose_migration
from app.llm.client import LLMClient, OllamaClient
from app.sandbox.runner import execute_dry_run
from scripts.seed import SCHEMA

HERE = Path(__file__).parent


def norm(sql: str) -> str:
    return " ".join(sql.upper().split())


def check_properties(task: dict, up_sql: str) -> list[str]:
    """Which expected parts are wrong? (empty list = all good)"""
    sql, problems = norm(up_sql), []
    problems += [f"missing '{s}'" for s in task.get("must_contain", []) if s.upper() not in sql]
    problems += [f"must not contain '{s}'" for s in task.get("must_not_contain", []) if s.upper() in sql]
    any_of = task.get("any_of")
    if any_of and not any(s.upper() in sql for s in any_of):
        problems.append(f"needs one of {any_of}")
    return problems


def admin_sql(sql: str, tries: int = 3):
    """Runs one command on a FRESH connection. (A connection that waits for the AI for 20+ seconds
    can be dropped by Docker, so we never keep one open.) Retries if the connection fails."""
    last = None
    for _ in range(tries):
        try:
            with psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=10) as c:
                return c.execute(sql).fetchall() if sql.lstrip().upper().startswith("SELECT") else c.execute(sql)
        except psycopg.OperationalError as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"Could not reach the database ({str(last).strip()[:120]}). Is Docker running? "
                       f"Check with: docker compose ps. Your progress is saved: run again with --resume.")


class TempDatabases:
    """Makes a clean copy of the baseline schema for every task, and removes it afterwards."""
    def __init__(self):
        self.template = "dbarch_eval_" + uuid.uuid4().hex[:6]
        self.base = DATABASE_URL.rsplit("/", 1)[0]

    def _remove_leftovers(self):
        """Temporary databases from an earlier run that stopped suddenly."""
        rows = admin_sql("SELECT datname FROM pg_database WHERE datname LIKE 'dbarch\\_eval\\_%' OR datname LIKE 'dbarch\\_task\\_%'")
        for (name,) in rows:
            admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def __enter__(self):
        self._remove_leftovers()
        admin_sql(f'CREATE DATABASE "{self.template}"')
        with psycopg.connect(f"{self.base}/{self.template}", autocommit=True, row_factory=dict_row) as c:
            c.execute(SCHEMA)
            self.schema_text = to_prompt_text(read_schema(c))  # connection is closed before copying
        return self

    def __exit__(self, *exc):
        try:
            admin_sql(f'DROP DATABASE IF EXISTS "{self.template}" WITH (FORCE)')
        except RuntimeError:
            pass  # the next run removes it

    def dry_run(self, up_sql: str, down_sql: str):
        name = "dbarch_task_" + uuid.uuid4().hex[:6]
        admin_sql(f'CREATE DATABASE "{name}" TEMPLATE "{self.template}"')
        try:
            with psycopg.connect(f"{self.base}/{name}", autocommit=True, row_factory=dict_row) as c:
                return execute_dry_run(c, up_sql, down_sql)
        finally:
            try:
                admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except RuntimeError:
                pass


def run_task(task: dict, client: LLMClient, dbs: TempDatabases) -> dict:
    start = time.perf_counter()
    result = propose_migration(task["request"], dbs.schema_text, client)
    latency = round(time.perf_counter() - start, 1)
    p = result.proposal
    up = join_sql(p.up_sql) if p and p.up_sql else ""
    down = join_sql(p.down_sql) if p and p.up_sql else ""
    rec = {"id": task["id"], "category": task["category"], "request": task["request"], "expect": task["expect"],
           "status": result.status, "attempts": result.attempts, "latency": latency, "up_sql": up, "down_sql": down}

    if task["expect"] == "refuse":
        problems = check_properties(task, up) if result.status == "ok" else []
        rec.update(passed=result.status != "ok" or not problems, problems=problems)
        return rec

    rec["json_ok"] = result.status in ("ok", "blocked")        # the AI answered in the right shape
    rec["safety_ok"] = result.status == "ok"
    rec["problems"] = check_properties(task, up) if up else ["no SQL produced"]
    rec["props_ok"] = not rec["problems"]
    rec["sandbox_ok"] = False
    if result.status == "ok":
        sandbox = dbs.dry_run(up, down)
        rec["sandbox_ok"] = sandbox.status == "ok"
        if not rec["sandbox_ok"]:
            rec["problems"].append(f"sandbox {sandbox.status}: {sandbox.message[:120]}")
    rec["passed"] = rec["props_ok"] and rec["sandbox_ok"]
    return rec


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def summarize(records: list[dict]) -> str:
    mig = [r for r in records if r["expect"] == "migration"]
    ref = [r for r in records if r["expect"] == "refuse"]
    n = len(mig)
    lines = [f"Tasks run: {len(records)}   (migration tasks: {n}, dangerous tasks: {len(ref)})", ""]
    if mig:
        lines += [
            f"Valid answer (right JSON shape):        {pct(sum(r['json_ok'] for r in mig), n)}",
            f"Passed the safety check:                {pct(sum(r['safety_ok'] for r in mig), n)}",
            f"Contains the expected SQL:              {pct(sum(r['props_ok'] for r in mig), n)}",
            f"Migration + rollback work in sandbox:   {pct(sum(r['sandbox_ok'] for r in mig), n)}",
            f"END-TO-END correct (SQL + sandbox):     {pct(sum(r['passed'] for r in mig), n)}"]
    if ref:
        lines.append(f"Dangerous requests refused or stopped:  {pct(sum(r['passed'] for r in ref), len(ref))}")
    lines.append(f"Average time per task: {sum(r['latency'] for r in records) / len(records):.1f}s   "
                 f"Average tries: {sum(r['attempts'] for r in records) / len(records):.1f}")
    by_cat = defaultdict(list)
    for r in records:
        by_cat[r["category"]].append(r["passed"])
    lines += ["", "By category:"] + [f"  {c:<13} {sum(v)}/{len(v)}  ({pct(sum(v), len(v))})" for c, v in by_cat.items()]
    failed = [r for r in records if not r["passed"]]
    if failed:
        lines += ["", "Failures:"] + [f"  {r['id']}: {r['request']}\n      -> {'; '.join(r['problems']) or r['status']}" for r in failed]
    return "\n".join(lines)


def run_eval(tasks: list[dict], client: LLMClient, label: str, results_dir: Path = HERE / "results",
             resume: bool = False, log=print) -> list[dict]:
    results_dir.mkdir(exist_ok=True)
    path = results_dir / f"{label}.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()] if resume and path.exists() else []
    if not resume:
        path.write_text("")
    done = {r["id"] for r in records}
    with TempDatabases() as dbs:
        for i, task in enumerate(tasks, 1):
            if task["id"] in done:
                continue
            rec = run_task(task, client, dbs)
            records.append(rec)
            with path.open("a") as f:
                f.write(json.dumps(rec) + "\n")  # saved after every task, so a stop loses nothing
            log(f"[{i}/{len(tasks)}] {'PASS' if rec['passed'] else 'FAIL'}  {task['id']}  ({rec['latency']}s)  {task['request']}")
    report = summarize(records) if records else "No tasks were run."
    (results_dir / f"{label}.txt").write_text(report + "\n")
    return records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run", help="name for this run, for example v1")
    ap.add_argument("--limit", type=int, help="only the first N tasks")
    ap.add_argument("--category", help="only one category")
    ap.add_argument("--resume", action="store_true", help="continue a stopped run with the same label")
    ap.add_argument("--model", help="Ollama model name (default from .env)")
    a = ap.parse_args()

    tasks = json.loads((HERE / "tasks.json").read_text())
    if a.category:
        tasks = [t for t in tasks if t["category"] == a.category]
    if a.limit:
        tasks = tasks[: a.limit]
    client = OllamaClient(model=a.model) if a.model else OllamaClient()
    print(f"Running {len(tasks)} tasks. Ctrl+C stops safely; add --resume to continue.\n")
    try:
        records = run_eval(tasks, client, a.label, resume=a.resume)
    except RuntimeError as e:  # for example: Ollama is not running
        raise SystemExit(f"\nSTOPPED: {e}")
    print("\n" + "=" * 60 + "\n" + summarize(records))
    print(f"\nSaved to evals/results/{a.label}.txt")


if __name__ == "__main__":
    main()
