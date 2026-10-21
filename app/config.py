"""Settings, read from the .env file."""
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5433/shop")
