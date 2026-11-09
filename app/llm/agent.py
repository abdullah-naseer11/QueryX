"""Request in, checked migration out.  Loop: ask AI -> validate -> safety check -> (fix and retry)."""
import json
import re

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pydantic import BaseModel, ValidationError

from app.llm.client import LLMClient
from app.llm.prompts import SYSTEM_PROMPT, build_user_prompt
from app.safety.checker import check_sql
from app.safety.models import SafetyReport


class Proposal(BaseModel):
    summary: str = ""
    up_sql: list[str] = []
    down_sql: list[str] = []
    risks: list[str] = []
    irreversible: bool = False
    refusal: str | None = None


class ProposalResult(BaseModel):
    status: str                        # "ok" | "refused" | "blocked" | "failed"
    proposal: Proposal | None = None
    report: SafetyReport | None = None  # safety check of up_sql
    attempts: int = 0
    message: str = ""                  # what went wrong, if anything


def join_sql(statements: list[str]) -> str:
    return "\n".join(s.rstrip() if s.rstrip().endswith(";") else s.rstrip() + ";" for s in statements)


def makes_no_change(sql: str) -> bool:
    """True if the SQL only sets session options (like SET lock_timeout) and changes nothing."""
    try:
        statements = parse_sql(sql)
    except ParseError:
        return False  # the safety checker reports syntax errors
    return all(isinstance(raw.stmt, ast.VariableSetStmt) for raw in statements)


def parse_proposal(text: str) -> Proposal:
    """The AI sometimes wraps JSON in ```code fences``` or adds words around it. Cut those off."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("No JSON object found in the answer.")
    return Proposal.model_validate(json.loads(match.group(0)))


def propose_migration(request: str, schema_text: str, client: LLMClient, max_attempts: int = 3) -> ProposalResult:
    feedback: str | None = None
    proposal: Proposal | None = None
    report: SafetyReport | None = None

    for attempt in range(1, max_attempts + 1):
        answer = client.complete(SYSTEM_PROMPT, build_user_prompt(schema_text, request, feedback),
                                 Proposal.model_json_schema())
        try:
            proposal = parse_proposal(answer)
        except (ValueError, ValidationError) as e:
            feedback = f"The answer was not the required JSON shape: {e}"
            continue

        if proposal.refusal:
            # A refused request must carry no SQL at all, even if the AI wrote some anyway.
            proposal = proposal.model_copy(update={"up_sql": [], "down_sql": []})
            return ProposalResult(status="refused", proposal=proposal, attempts=attempt, message=proposal.refusal)
        if not proposal.up_sql:
            feedback = "up_sql is empty. Write the SQL, or set refusal with a reason."
            continue

        if makes_no_change(join_sql(proposal.up_sql)):
            feedback = "up_sql only sets options and changes nothing. It must contain the real change (for example ALTER TABLE ...)."
            continue

        report = check_sql(join_sql(proposal.up_sql))
        if report.verdict != "BLOCK":
            return ProposalResult(status="ok", proposal=proposal, report=report, attempts=attempt)
        feedback = "The safety checker blocked it:\n" + "\n".join(
            f"- {r.message} Safer way: {r.suggestion}" for r in report.results if r.severity == "BLOCK")

    status = "blocked" if report and report.verdict == "BLOCK" else "failed"
    return ProposalResult(status=status, proposal=proposal, report=report, attempts=max_attempts,
                          message=f"No safe answer after {max_attempts} tries. Last problem: {feedback}")
