"""Safety rules. Each rule looks at ONE parsed statement and returns a list of problems.
Rules read the parsed tree (AST), not the raw text, so spacing and capital letters do not fool them."""
from pglast import ast
from pglast.enums import AlterTableType as AT
from pglast.enums import ConstrType, ObjectType

from app.safety.models import RuleResult


def _r(rule: str, severity: str, message: str, suggestion: str) -> RuleResult:
    return RuleResult(rule=rule, severity=severity, message=message, suggestion=suggestion)


def rule_drop(stmt) -> list[RuleResult]:
    if isinstance(stmt, ast.DropdbStmt):
        return [_r("drop_database", "BLOCK", "DROP DATABASE removes the whole database for good.",
                   "Never run this from the agent. Take a backup and do it by hand if truly needed.")]
    if isinstance(stmt, ast.DropStmt) and stmt.removeType in (ObjectType.OBJECT_TABLE, ObjectType.OBJECT_SCHEMA):
        what = "table" if stmt.removeType == ObjectType.OBJECT_TABLE else "schema"
        return [_r(f"drop_{what}", "BLOCK", f"DROP {what.upper()} removes the {what} and all its data for good.",
                   f"Rename the {what} first, or archive its data, then remove it later.")]
    return []


def rule_truncate(stmt) -> list[RuleResult]:
    if isinstance(stmt, ast.TruncateStmt):
        return [_r("truncate", "BLOCK", "TRUNCATE deletes every row in the table at once.",
                   "Delete in small batches with a WHERE condition (use the backfill engine).")]
    return []


def rule_missing_where(stmt) -> list[RuleResult]:
    if isinstance(stmt, ast.DeleteStmt) and stmt.whereClause is None:
        return [_r("delete_without_where", "BLOCK", "DELETE without WHERE removes every row.",
                   "Add a WHERE condition, and run it in small batches.")]
    if isinstance(stmt, ast.UpdateStmt) and stmt.whereClause is None:
        return [_r("update_without_where", "BLOCK", "UPDATE without WHERE changes every row.",
                   "Add a WHERE condition, and run it in small batches.")]
    return []


def rule_alter_table(stmt) -> list[RuleResult]:
    out: list[RuleResult] = []
    if not isinstance(stmt, ast.AlterTableStmt):
        return out
    for cmd in stmt.cmds or ():
        if cmd.subtype == AT.AT_DropColumn:
            out.append(_r("drop_column", "WARN", "DROP COLUMN loses that column's data for good.",
                          "First stop the app from using it. Drop it in a later release."))
        elif cmd.subtype == AT.AT_AlterColumnType:
            out.append(_r("alter_column_type", "WARN", "Changing a column type can rewrite the whole table and block it.",
                          "Add a new column, copy the data in batches, then switch over (expand and contract)."))
        elif cmd.subtype == AT.AT_AddConstraint and isinstance(cmd.def_, ast.Constraint):
            c = cmd.def_
            if c.contype in (ConstrType.CONSTR_FOREIGN, ConstrType.CONSTR_CHECK) and not c.skip_validation:
                kind = "FOREIGN KEY" if c.contype == ConstrType.CONSTR_FOREIGN else "CHECK"
                out.append(_r("constraint_without_not_valid", "WARN",
                              f"Adding a {kind} constraint scans the whole table while holding a lock.",
                              "Add it with NOT VALID, then run VALIDATE CONSTRAINT as a separate step."))
        elif cmd.subtype == AT.AT_AddColumn and isinstance(cmd.def_, ast.ColumnDef):
            kinds = {c.contype for c in (cmd.def_.constraints or ())}
            if ConstrType.CONSTR_NOTNULL in kinds and ConstrType.CONSTR_DEFAULT not in kinds:
                out.append(_r("not_null_without_default", "WARN",
                              "A NOT NULL column with no default fails when the table already has rows.",
                              "Add it as nullable, fill it in batches, then add NOT NULL."))
    return out


def rule_index(stmt) -> list[RuleResult]:
    if isinstance(stmt, ast.IndexStmt) and not stmt.concurrent:
        return [_r("index_not_concurrent", "WARN", "CREATE INDEX blocks writes to the table while it builds.",
                   "Use CREATE INDEX CONCURRENTLY (it cannot run inside a transaction).")]
    return []


STATEMENT_RULES = [rule_drop, rule_truncate, rule_missing_where, rule_alter_table, rule_index]


def is_schema_change(stmt) -> bool:
    return isinstance(stmt, (ast.AlterTableStmt, ast.IndexStmt))


def sets_lock_timeout(stmt) -> bool:
    return isinstance(stmt, ast.VariableSetStmt) and stmt.name == "lock_timeout"
