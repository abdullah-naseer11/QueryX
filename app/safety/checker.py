"""Runs every rule on every statement and returns one report."""
from pglast import parse_sql
from pglast.parser import ParseError

from app.safety.models import RuleResult, SafetyReport
from app.safety.rules import STATEMENT_RULES, is_schema_change, sets_lock_timeout


def check_sql(sql: str) -> SafetyReport:
    try:
        raw_statements = parse_sql(sql)
    except ParseError as e:
        return SafetyReport(results=[RuleResult(
            rule="parse_error", severity="BLOCK", message=f"The SQL could not be parsed: {e}",
            suggestion="Fix the SQL syntax.", statement=sql.strip()[:200])])

    results: list[RuleResult] = []
    has_lock_timeout = False
    changes_schema = False
    for raw in raw_statements:
        stmt = raw.stmt
        text = sql[raw.stmt_location: raw.stmt_location + (raw.stmt_len or len(sql))].strip()
        for rule in STATEMENT_RULES:
            for found in rule(stmt):
                found.statement = text
                results.append(found)
        has_lock_timeout = has_lock_timeout or sets_lock_timeout(stmt)
        changes_schema = changes_schema or is_schema_change(stmt)

    if changes_schema and not has_lock_timeout:
        results.append(RuleResult(
            rule="missing_lock_timeout", severity="WARN",
            message="No lock_timeout is set. If the change waits for a lock, the whole app can freeze behind it.",
            suggestion="Start with SET lock_timeout = '3s'; and retry if it fails."))
    return SafetyReport(results=results)
