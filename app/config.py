"""Settings, read from the .env file."""
import os
from dotenv import load_dotenv

load_dotenv()

# Admin account: used only by setup scripts (seed, create read-only role).
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5433/shop")
# Read-only account: used by the schema reader. It cannot change or even read table rows.
READONLY_DATABASE_URL = os.getenv("READONLY_DATABASE_URL", "postgresql://reader:reader@localhost:5433/shop")
