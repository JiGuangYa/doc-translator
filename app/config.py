"""Path constants and global configuration."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent      # project root doc-translator/
APP_DIR = BASE_DIR / "app"
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = DATA_DIR / "config"
TASKS_DIR = DATA_DIR / "tasks"
STATIC_DIR = BASE_DIR / "static"

PROVIDERS_FILE = CONFIG_DIR / "providers.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

for d in (DATA_DIR, CONFIG_DIR, TASKS_DIR):
    d.mkdir(parents=True, exist_ok=True)

HOST = "127.0.0.1"
PORT = 8765

MAX_UPLOAD_MB = 100
ALLOWED_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".pdf"}

# Batch processing parameters (overridable in settings)
DEFAULT_BATCH_MAX_CHARS = 4000
DEFAULT_BATCH_MAX_SEGMENTS = 20
DEFAULT_CONCURRENCY_BATCHES = 3

LLM_TIMEOUT = 120
LLM_MAX_RETRIES = 3          # number of exponential backoff retries for network/429 errors
LLM_BACKOFF_BASE = 2.0       # seconds
MAX_CONSECUTIVE_BATCH_FAILURES = 3

# Task TTL: tasks in a terminal state (done/cancelled/failed) older than this many
# days are cleaned up at startup; 0 disables cleanup.
TASK_TTL_DAYS = 30
