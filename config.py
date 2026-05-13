import os
from pathlib import Path

def load_env_file():
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')

        if key and key not in os.environ:
            os.environ[key] = value


# Load environment variables early
load_env_file()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
DATA_FILE = "movies.json"
ADMIN_FILE = "admins.json"
PENDING_KEY = "pending_post"
DEFAULT_ADMIN_ID = "1800599162"
DEFAULT_GENRE = ["Action"]
DEFAULT_TAGS = ["auto", "generated"]
DEFAULT_TYPE = "movie"
DEFAULT_STATUS = "published"
