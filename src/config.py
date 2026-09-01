"""Environment-driven configuration. No secrets in the repo."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _bool(name, default=False):
    return str(os.environ.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "alerts.db"
DB_URL = f"sqlite:///{DB_PATH}"

POLL_SECONDS = _int("POLL_SECONDS", 60)
INCLUDE_EXTENDED_HOURS = _bool("INCLUDE_EXTENDED_HOURS", True)

# email
SMTP_HOST = os.environ.get("NOTIFY_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _int("NOTIFY_SMTP_PORT", 587)
SMTP_USER = os.environ.get("NOTIFY_SMTP_USER", "")
SMTP_PASS = os.environ.get("NOTIFY_SMTP_PASS", "")
TO_EMAIL = os.environ.get("NOTIFY_TO_EMAIL", "")
FROM_EMAIL = os.environ.get("NOTIFY_FROM_EMAIL", "") or SMTP_USER or TO_EMAIL

# push
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

# web
PORT = _int("PORT", 8080)
UI_PASSWORD = os.environ.get("UI_PASSWORD", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "") or os.urandom(24).hex()


def channels():
    """Which delivery channels are actually configured."""
    out = []
    if TO_EMAIL and SMTP_PASS:
        out.append("email")
    if NTFY_TOPIC:
        out.append("ntfy")
    return out
