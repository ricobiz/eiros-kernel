import os
from pathlib import Path

# Paths
WORKSPACE = "./workspace"
LOGS_DIR = "./logs"
DB_PATH = "./eiros_state.db"
EVENTS_LOG = os.path.join(LOGS_DIR, "events.jsonl")
REFLECTIONS_LOG = os.path.join(LOGS_DIR, "reflections.jsonl")
WORKSPACE_ROOT = Path(WORKSPACE).resolve()

# LLM
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL = os.getenv("EIROS_MODEL", "google/gemini-3.1-flash-lite")
PLANNER_MODEL = os.getenv("EIROS_PLANNER_MODEL", "google/gemini-3.1-flash-lite")

# Auth
EIROS_API_KEY = os.getenv("EIROS_API_KEY", "")

# Limits
MAX_FILE_WRITE_CHARS = 200_000
MAX_FILE_READ_BYTES = 1_000_000
MAX_INPUT_CHARS = 8000
EVENTS_LIMIT_CAP = 500
MEMORY_SEARCH_LIMIT_CAP = 50
WORKSPACE_LIST_LIMIT = 200
MAX_BROWSER_STEPS_PER_TASK = 20
BROWSER_ACTION_TIMEOUT = 30

# Rate limiting
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_RPM", "60"))
RATE_LIMIT_WINDOW = 60

# Server
EIROS_HOST = os.getenv("EIROS_HOST", "127.0.0.1")  # explicit 0.0.0.0 requires env
EIROS_PORT = int(os.getenv("EIROS_PORT", "8000"))

os.makedirs(WORKSPACE, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
