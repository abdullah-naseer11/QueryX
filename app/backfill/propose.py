"""Plain English -> backfill spec, using the AI. Same idea as migrations: ask, validate, fix, retry."""
import json
import re

from pydantic import ValidationError

from app.backfill.spec import BackfillSpec, validate_spec
from app.db.models import SchemaSnapshot
from app.llm.client import LLMClient

SYSTEM_PROMPT = """You write PostgreSQL data backfills. You get a schema and a request.
Reply with ONE JSON object only:
{"summary": "one short sentence", "table": "users", "key": "id",
 "set_clause": "user_tier = 'VIP'", "where_clause": "id IN (SELECT user_id FROM orders GROUP BY user_id HAVING sum(total) > 1000)",
 "refusal": null}

Rules:
- "key" is the single-column integer PRIMARY KEY of the table.
- "set_clause" is only what comes after SET (column = value, ...). Never change the key column.
- "where_clause" is only the condition (no WHERE word). It must select exactly the rows to change.
- Use only tables and columns from the schema. The column being set must already exist.
- If the request is not a data update, or is unsafe, set "refusal" to a short reason and leave the other fields empty.
"""


def propose_backfill(request: str, schema_text: str, snapshot: SchemaSnapshot, client: LLMClient,
                     max_attempts: int = 3) -> tuple[BackfillSpec | None, str]:
    """Returns (spec, "") on success, or (None, reason)."""
    feedback = ""
    for _ in range(max_attempts):
        user = f"DATABASE SCHEMA:\n{schema_text}\n\nREQUEST:\n{request}\n"
        if feedback:
            user += f"\nYour previous answer was rejected. Fix it and answer again with the JSON object only.\nWHY:\n{feedback}\n"
        answer = client.complete(SYSTEM_PROMPT, user, BackfillSpec.model_json_schema())
        try:
            match = re.search(r"\{.*\}", answer, re.S)
            if not match:
                raise ValueError("No JSON object found.")
            spec = BackfillSpec.model_validate(json.loads(match.group(0)))
        except (ValueError, ValidationError) as e:
            feedback = f"Not the required JSON shape: {e}"
            continue
        if spec.refusal:
            return None, spec.refusal
        problem = validate_spec(spec, snapshot)
        if problem is None:
            return spec, ""
        feedback = problem
    return None, f"No valid backfill after {max_attempts} tries. Last problem: {feedback}"
