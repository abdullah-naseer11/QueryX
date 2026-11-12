"""Settings, read from the .env file."""
import os
from dotenv import load_dotenv

load_dotenv()

# Admin account: used only by setup scripts (seed, create read-only role).
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5433/shop")
# Read-only account: used by the schema reader. It cannot change or even read table rows.
READONLY_DATABASE_URL = os.getenv("READONLY_DATABASE_URL", "postgresql://reader:reader@localhost:5433/shop")

# Local AI (Ollama). Change the model name in .env to try a bigger model.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

# Sandbox: a throw-away database in Docker, used to test migrations safely.
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "postgres:16")

# API. Change API_KEY in .env: every request must send it in the X-API-Key header.
API_KEY = os.getenv("API_KEY", "dev-key-change-me")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
