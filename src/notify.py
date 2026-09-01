"""Alert delivery: email (SMTP) and phone push (ntfy.sh).

Both channels are optional and independent. A failure in one never blocks the
other, and never crashes the poller - a missed notification should not take the
whole alerter down. Every send returns the list of channels that actually
succeeded, which is recorded on the fire row so the history shows what really
went out.
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from src import config

log = logging.getLogger(__name__)


def send_email(subject: str, body: str) -> bool:
    if not (config.TO_EMAIL and config.SMTP_PASS):
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = config.FROM_EMAIL
        msg["To"] = config.TO_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as srv:
            srv.starttls()
            srv.login(config.SMTP_USER or config.FROM_EMAIL, config.SMTP_PASS)
            srv.sendmail(config.FROM_EMAIL, config.TO_EMAIL, msg.as_string())
        return True
    except Exception as e:
        log.warning("email delivery failed: %s: %s", type(e).__name__, e)
        return False


def send_push(title: str, body: str, tags="chart_with_upwards_trend", priority="default") -> bool:
    if not config.NTFY_TOPIC:
        return False
    try:
        r = requests.post(
            f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Tags": tags, "Priority": priority},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("push delivery failed: %s: %s", type(e).__name__, e)
        return False


def deliver(subject: str, body: str, direction: str = "up") -> str:
    """Send on every configured channel. Returns e.g. 'email+ntfy', or ''."""
    ok = []
    if send_email(subject, body):
        ok.append("email")
    tag = "chart_with_upwards_trend" if direction == "up" else "chart_with_downwards_trend"
    if send_push(subject, body, tags=tag, priority="high"):
        ok.append("ntfy")
    return "+".join(ok)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ch = config.channels()
    print(f"configured channels: {ch or 'NONE — fill in .env'}")
    if "--send" in sys.argv:
        got = deliver("price-alerts test", "If you can read this, delivery works.")
        print(f"delivered via: {got or 'nothing'}")
    else:
        print("run with --send to fire a real test notification")
