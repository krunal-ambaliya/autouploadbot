import os
from pathlib import Path

BOT_TOKEN = "8203882497:AAHWPnRtmp7Ve6PvnsVRF2GsvQmYJsFPsnU"
DATA_FILE = "movies.json"
PENDING_KEY = "pending_post"
DEFAULT_GENRE = ["Action"]
DEFAULT_TAGS = ["auto", "generated"]
DEFAULT_TYPE = "movie"
DEFAULT_STATUS = "published"


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
