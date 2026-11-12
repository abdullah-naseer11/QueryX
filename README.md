# Database Architect AI (backend)

Plain English -> safe database migrations. The AI writes the migration, a safety checker and a Docker
sandbox test it, and a person approves it before it touches the real database.

## Setup (Windows PowerShell)
    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    copy .env.example .env
    docker compose up -d
    python -m scripts.seed
    python -m scripts.setup_readonly
    pytest

## Command line
    python -m app --help
    python -m app new "Add a user_tier column to users"   # then: sandbox ID, approve ID, apply ID

## API
    uvicorn app.api.main:app --reload
Open http://127.0.0.1:8000/docs. Every request except /health needs the header `X-API-Key` (value from `.env`).
Live updates: `ws://127.0.0.1:8000/ws/migrations/{id}?api_key=...`

## Measuring quality
    python -m evals.run_eval --label v3
Results of earlier runs are in `evals/results/`.
