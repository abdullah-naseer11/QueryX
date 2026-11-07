"""Shapes for safety results."""
from typing import Literal

from pydantic import BaseModel


class RuleResult(BaseModel):
    rule: str                              # short rule name
    severity: Literal["BLOCK", "WARN"]     # BLOCK = must not run, WARN = read before running
    message: str                           # what is wrong
    suggestion: str                        # the safer way
    statement: str = ""                    # the SQL this is about


class SafetyReport(BaseModel):
    results: list[RuleResult] = []

    @property
    def verdict(self) -> Literal["BLOCK", "WARN", "OK"]:
        if any(r.severity == "BLOCK" for r in self.results):
            return "BLOCK"
        return "WARN" if self.results else "OK"
