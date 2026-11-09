import json

from app.llm.agent import join_sql, parse_proposal, propose_migration
from app.llm.client import LLMClient
from app.llm.prompts import build_user_prompt

SCHEMA = "TABLE users\n  id bigint NOT NULL\n  email text NOT NULL"


class FakeAI(LLMClient):
    """Gives pre-written answers one by one, and remembers what it was asked."""
    def __init__(self, *answers: str):
        self.answers, self.asked = list(answers), []

    def complete(self, system, user, json_schema=None):
        self.asked.append(user)
        return self.answers.pop(0)


def answer(up, down=None, refusal=None):
    return json.dumps({"summary": "s", "up_sql": up, "down_sql": down or [], "refusal": refusal})


GOOD = answer(["SET lock_timeout = '3s';", "ALTER TABLE users ADD COLUMN tier text;"],
              ["ALTER TABLE users DROP COLUMN tier;"])
BAD = answer(["DROP TABLE users;"])


def test_good_answer_is_accepted_first_time():
    r = propose_migration("add tier", SCHEMA, FakeAI(GOOD))
    assert (r.status, r.attempts) == ("ok", 1)
    assert r.report.verdict == "OK"


def test_json_inside_code_fence_is_understood():
    r = propose_migration("add tier", SCHEMA, FakeAI("Sure!\n```json\n" + GOOD + "\n```"))
    assert r.status == "ok"


def test_broken_json_is_retried():
    ai = FakeAI("this is not json", GOOD)
    r = propose_migration("add tier", SCHEMA, ai)
    assert (r.status, r.attempts) == ("ok", 2)
    assert "not the required JSON" in ai.asked[1]


def test_blocked_sql_is_sent_back_and_fixed():
    ai = FakeAI(BAD, GOOD)
    r = propose_migration("add tier", SCHEMA, ai)
    assert (r.status, r.attempts) == ("ok", 2)
    assert "safety checker blocked" in ai.asked[1]


def test_always_blocked_ends_as_blocked():
    r = propose_migration("remove users", SCHEMA, FakeAI(BAD, BAD, BAD))
    assert r.status == "blocked" and r.attempts == 3
    assert r.report.verdict == "BLOCK"


def test_refusal_is_returned_as_refused():
    r = propose_migration("delete everything", SCHEMA, FakeAI(answer([], refusal="Too destructive")))
    assert r.status == "refused" and r.message == "Too destructive"


def test_empty_sql_is_retried():
    r = propose_migration("add tier", SCHEMA, FakeAI(answer([]), GOOD))
    assert (r.status, r.attempts) == ("ok", 2)


def test_never_answers_json_ends_as_failed():
    r = propose_migration("add tier", SCHEMA, FakeAI("no", "no", "no"))
    assert r.status == "failed"


def test_warning_is_accepted_but_reported():
    r = propose_migration("add index", SCHEMA, FakeAI(answer(["CREATE INDEX CONCURRENTLY i ON users (email);"])))
    assert r.status == "ok" and r.report.verdict == "WARN"


def test_prompt_contains_schema_and_request():
    text = build_user_prompt(SCHEMA, "add tier")
    assert "TABLE users" in text and "add tier" in text


def test_join_sql_adds_missing_semicolons():
    assert join_sql(["SELECT 1", "SELECT 2;"]) == "SELECT 1;\nSELECT 2;"


def test_parse_proposal_rejects_text_without_json():
    import pytest
    with pytest.raises(ValueError):
        parse_proposal("hello")


def test_sql_that_changes_nothing_is_retried():
    only_set = answer(["SET lock_timeout = '3s';"], ["ALTER TABLE users DROP COLUMN tier;"])
    ai = FakeAI(only_set, GOOD)
    r = propose_migration("add tier", SCHEMA, ai)
    assert (r.status, r.attempts) == ("ok", 2)
    assert "changes nothing" in ai.asked[1]


def test_sql_that_always_changes_nothing_fails():
    only_set = answer(["SET lock_timeout = '3s';"])
    assert propose_migration("add tier", SCHEMA, FakeAI(only_set, only_set, only_set)).status == "failed"


def test_refusal_drops_any_sql_the_ai_wrote_anyway():
    r = propose_migration("drop users", SCHEMA, FakeAI(answer(["DROP TABLE users;"], ["SELECT 1;"], refusal="Destructive")))
    assert r.status == "refused"
    assert r.proposal.up_sql == [] and r.proposal.down_sql == []
