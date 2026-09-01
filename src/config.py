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

# How long the web app waits before calling the poller dead. Three missed polls,
# with a floor so a fast POLL_SECONDS does not make /healthz flap on one slow
# Yahoo request.
ENGINE_STALE_SECONDS = _int("ENGINE_STALE_SECONDS", max(180, POLL_SECONDS * 3))

# Check a symbol resolves before saving an alert for it. A typo is otherwise
# silent: the alert sits there looking healthy and never fires.
VALIDATE_TICKERS = _bool("VALIDATE_TICKERS", True)

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
LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 8)
LOGIN_LOCKOUT_SECONDS = _int("LOGIN_LOCKOUT_SECONDS", 300)


def _secret_key():
    """Stable across restarts, so a container restart does not log you out.

    A random per-process key also breaks the moment the web app runs more than
    one worker, since each would sign cookies differently.
    """
    from_env = os.environ.get("SECRET_KEY", "").strip()
    if from_env:
        return from_env
    path = DATA_DIR / ".secret_key"
    try:
        if path.exists():
            saved = path.read_text().strip()
            if saved:
                return saved
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32).hex()
        path.write_text(key)
        path.chmod(0o600)
        return key
    except OSError:
        # read-only data dir: fall back to ephemeral rather than refusing to boot
        return os.urandom(32).hex()


SECRET_KEY = _secret_key()


def channels():
    """Which delivery channels are actually configured."""
    out = []
    if TO_EMAIL and SMTP_PASS:
        out.append("email")
    if NTFY_TOPIC:
        out.append("ntfy")
    return out
