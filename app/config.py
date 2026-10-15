import os
from pathlib import Path


def _int(name, default):
    return int(os.environ.get(name, default))


DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
MEDIA_DIR = DATA_DIR / "media"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "ytcuan.db"

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")  # Fernet key, encrypts tokens + stream keys

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
CLIENT_SECRETS_FILE = DATA_DIR / "client_secrets.json"  # optional, overrides the two above

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
RTMP_BASE = os.environ.get("RTMP_BASE", "rtmp://a.rtmp.youtube.com/live2/")

TRANSCODE_CONCURRENCY = _int("TRANSCODE_CONCURRENCY", 1)
MAX_ACTIVE_BROADCASTS = _int("MAX_ACTIVE_BROADCASTS", 30)
JOB_DELAY_SEC = _int("JOB_DELAY_SEC", 60)
QUOTA_LIMIT = _int("QUOTA_LIMIT", 10000)
MAX_RECONNECTS = _int("MAX_RECONNECTS", 10)
KEY_MAX_BROADCAST_FAILS = _int("KEY_MAX_BROADCAST_FAILS", 3)
TIMEZONE = os.environ.get("TZ_DISPLAY", "Asia/Jakarta")

VERSION = os.environ.get("APP_VERSION", "dev")  # set at image build
HUB_URL = os.environ.get("HUB_URL", "https://ytcuan.autopreneur.id").rstrip("/")
AGENT_TOKEN_FILE = DATA_DIR / "agent_token"  # written by `python -m app.pair <code>`
STATUS_INTERVAL = _int("STATUS_INTERVAL", 10)

# Per-channel defaults; every value is editable from the dashboard.
CHANNEL_DEFAULTS = {
    "resolution": 1080,
    "rate_mode": "vbr",          # vbr | cbr
    "privacy": "public",
    "category_id": "10",         # Music
    "tags": "",
    "cycle_hours": 12.0,         # 0 = no end
    "slots_per_key": 3,
    "change_title": True,
    "delete_title": False,
    "filter_view": False,
    "fv_min_minutes": 60,
    "fv_max_restarts": 3,
    "jitter_min": 5,
    "jitter_max": 30,
    "description_template": "",
    "keep_original": False,
}

for d in (MEDIA_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)
