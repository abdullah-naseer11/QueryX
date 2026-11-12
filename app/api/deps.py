"""Things every endpoint needs: the API key check, database connections, and the AI client."""
import hmac
from typing import Iterator

import psycopg
from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from psycopg.rows import dict_row

from app.config import API_KEY, DATABASE_URL, READONLY_DATABASE_URL
from app.llm.client import LLMClient, OllamaClient


def open_admin() -> psycopg.Connection:
    """A connection that may change things. Autocommit is on (see migrations/lifecycle.py)."""
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)


def open_readonly() -> psycopg.Connection:
    return psycopg.connect(READONLY_DATABASE_URL, row_factory=dict_row)


def admin_conn() -> Iterator[psycopg.Connection]:
    with open_admin() as conn:
        yield conn


def readonly_conn() -> Iterator[psycopg.Connection]:
    with open_readonly() as conn:
        yield conn


def llm_client() -> LLMClient:
    return OllamaClient()


def key_is_valid(key: str) -> bool:
    return hmac.compare_digest(key.encode(), API_KEY.encode())


# Declaring the key like this is what makes the "Authorize" button appear on the /docs page.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Your API_KEY from the .env file")


def require_key(x_api_key: str | None = Security(api_key_header)) -> None:
    if not key_is_valid(x_api_key or ""):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key header.")
