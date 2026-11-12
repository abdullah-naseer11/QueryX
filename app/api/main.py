"""The web API. Start it with:   uvicorn app.api.main:app --reload
Then open http://127.0.0.1:8000/docs to try every endpoint in the browser."""
import asyncio
from contextlib import asynccontextmanager

import psycopg
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api import deps
from app.backfill import engine as backfills
from app.backfill.propose import propose_backfill
from app.backfill.spec import BackfillSpec
from app.config import CORS_ORIGINS
from app.db.models import SchemaSnapshot
from app.db.schema_reader import read_schema, to_prompt_text
from app.llm.client import LLMClient
from app.migrations import lifecycle, store

ACTOR = "api"
FINISHED = {"APPLIED", "ROLLED_BACK", "REJECTED", "FAILED", "ROLLBACK_FAILED"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    with deps.open_admin() as conn:   # make sure our bookkeeping tables exist
        store.ensure_tables(conn)
    yield


app = FastAPI(title="Database Architect AI", version="1.0",
              description="Plain English -> safe database migrations. Every step needs the X-API-Key header.",
              lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ---- errors become clear HTTP answers ----
@app.exception_handler(ValueError)
async def wrong_state(_, e: ValueError):          # for example: approving before the sandbox
    return JSONResponse(status_code=409, content={"detail": str(e)})


@app.exception_handler(KeyError)
async def not_found(_, e: KeyError):
    return JSONResponse(status_code=404, content={"detail": e.args[0] if e.args else "Not found"})


@app.exception_handler(RuntimeError)
async def service_problem(_, e: RuntimeError):    # for example: Ollama or Docker is not running
    return JSONResponse(status_code=503, content={"detail": str(e)})


@app.get("/health")
def health():
    return {"status": "ok"}


secured = [Depends(deps.require_key)]


# ---- schema ----
@app.get("/schema", dependencies=secured, response_model=SchemaSnapshot)
def schema(conn: psycopg.Connection = Depends(deps.readonly_conn)):
    return read_schema(conn)


# ---- migrations ----
class ProposeIn(BaseModel):
    request: str = Field(min_length=3, max_length=500, examples=["Add a user_tier column to users"])


@app.post("/migrations/propose", dependencies=secured, response_model=store.Migration)
def propose(body: ProposeIn, conn: psycopg.Connection = Depends(deps.admin_conn),
            ro: psycopg.Connection = Depends(deps.readonly_conn), llm: LLMClient = Depends(deps.llm_client)):
    """The AI writes a migration. It is safety-checked and stored (status CHECKED, or REJECTED)."""
    return lifecycle.create(conn, body.request, to_prompt_text(read_schema(ro)), llm, ACTOR)


@app.get("/migrations", dependencies=secured, response_model=list[store.Migration])
def list_migrations(conn: psycopg.Connection = Depends(deps.admin_conn)):
    return store.list_all(conn)


@app.get("/migrations/{migration_id}", dependencies=secured, response_model=store.Migration)
def get_migration(migration_id: int, conn: psycopg.Connection = Depends(deps.admin_conn)):
    return store.get(conn, migration_id)


@app.get("/migrations/{migration_id}/audit", dependencies=secured)
def audit(migration_id: int, conn: psycopg.Connection = Depends(deps.admin_conn)):
    store.get(conn, migration_id)  # 404 if it does not exist
    return store.audit_log(conn, migration_id)


def step(name: str):
    """One endpoint per step: POST /migrations/{id}/<name>. The rules about order live in lifecycle.py."""
    def endpoint(migration_id: int, conn: psycopg.Connection = Depends(deps.admin_conn)):
        return getattr(lifecycle, name)(conn, migration_id, ACTOR)
    endpoint.__name__ = name
    app.post(f"/migrations/{{migration_id}}/{name}", dependencies=secured, response_model=store.Migration,
             summary=f"{name.capitalize()} a migration")(endpoint)


for _name in ("sandbox", "approve", "reject", "apply", "rollback"):
    step(_name)


# ---- live updates ----
def _state(conn, migration_id: int) -> dict | None:
    try:
        m = store.get(conn, migration_id)
    except KeyError:
        return None
    return {"id": m.id, "status": m.status, "sandbox_status": m.sandbox_status, "error": m.error,
            "approved_by": m.approved_by, "steps": [a["action"] for a in store.audit_log(conn, migration_id)]}


@app.websocket("/ws/migrations/{migration_id}")
async def watch(websocket: WebSocket, migration_id: int, api_key: str = "",
                conn: psycopg.Connection = Depends(deps.admin_conn)):
    """Sends the migration's state every time it changes, and closes when it is finished.
    Browsers cannot set headers on a WebSocket, so the key goes in the address: ?api_key=..."""
    if not deps.key_is_valid(api_key):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    last = None
    try:
        while True:
            state = await run_in_threadpool(_state, conn, migration_id)
            if state is None:
                await websocket.send_json({"error": "Migration not found"})
                break
            if state != last:
                await websocket.send_json(state)
                last = state
            if state["status"] in FINISHED:
                break
            await asyncio.sleep(1)
        await websocket.close()
    except WebSocketDisconnect:
        pass


# ---- backfills ----
class BackfillIn(BaseModel):
    request: str = Field(min_length=3, max_length=500)
    batch_size: int = 5000
    table: str | None = None            # fill table, set_clause and where_clause to skip the AI
    set_clause: str | None = None
    where_clause: str | None = None
    key: str = "id"


@app.post("/backfills", dependencies=secured, response_model=backfills.Backfill)
def create_backfill(body: BackfillIn, conn: psycopg.Connection = Depends(deps.admin_conn),
                    ro: psycopg.Connection = Depends(deps.readonly_conn), llm: LLMClient = Depends(deps.llm_client)):
    """Plan a data update done in small batches. Nothing is changed until you call /run."""
    snapshot: SchemaSnapshot = read_schema(ro)
    if body.set_clause:
        spec = BackfillSpec(table=body.table or "", key=body.key, set_clause=body.set_clause,
                            where_clause=body.where_clause or "", summary=body.request)
    else:
        spec, why = propose_backfill(body.request, to_prompt_text(snapshot), snapshot, llm)
        if spec is None:
            raise HTTPException(status_code=400, detail=why)
    try:
        return backfills.create(conn, body.request, spec, snapshot, ACTOR, body.batch_size)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/backfills", dependencies=secured, response_model=list[backfills.Backfill])
def list_backfills(conn: psycopg.Connection = Depends(deps.admin_conn)):
    return backfills.list_all(conn)


@app.get("/backfills/{backfill_id}", dependencies=secured, response_model=backfills.Backfill)
def get_backfill(backfill_id: int, conn: psycopg.Connection = Depends(deps.admin_conn)):
    return backfills.get(conn, backfill_id)


def _run_backfill(backfill_id: int) -> None:
    with deps.open_admin() as conn:
        backfills.run(conn, backfill_id)


@app.post("/backfills/{backfill_id}/run", dependencies=secured, status_code=202)
def run_backfill(backfill_id: int, background: BackgroundTasks, conn: psycopg.Connection = Depends(deps.admin_conn)):
    """Starts the backfill in the background. Watch progress with GET /backfills/{id}."""
    b = backfills.get(conn, backfill_id)
    if b.status not in ("PENDING", "PAUSED", "FAILED"):
        raise ValueError(f"Backfill {backfill_id} is {b.status}. This step needs: PENDING or PAUSED or FAILED.")
    background.add_task(_run_backfill, backfill_id)
    return {"id": backfill_id, "message": "Started. Check GET /backfills/{id} for progress."}
