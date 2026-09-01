"""The polling loop: evaluate every active alert, fire on transitions.

Runs as its own process (see docker-compose) so a slow notification or a Yahoo
hiccup cannot block the web UI, and the UI restarting cannot drop the poller.
"""
import datetime as dt
import logging
import time

from src import config, notify, quotes
from src.db import (HEARTBEAT_KEY, LAST_ERROR_KEY, Session, init_db, set_meta)
from src.models import Alert, Fire, Kind, Meta, Repeat, utcnow

log = logging.getLogger("engine")

TRADING_SESSIONS = {"PRE", "REG", "POST"}


def _should_fire(a: Alert, hit: bool, now: dt.datetime) -> bool:
    """Transition logic. See models.Alert docstring for the semantics."""
    if not hit:
        return False
    if a.repeat == Repeat.cooldown:
        if a.last_fired_at is None:
            return True
        age = (now - a.last_fired_at.replace(tzinfo=dt.timezone.utc)).total_seconds()
        return age >= a.cooldown_minutes * 60
    return a.armed          # once / rearm both require being armed


def _body(a: Alert, q: dict) -> str:
    pct = f"{q['pct']:+.2f}%" if q.get("pct") is not None else "n/a"
    ts = q["ts"]
    return (
        f"{a.describe()}\n\n"
        f"  price       ${q['price']:,.2f}\n"
        f"  prev close  ${q['prev_close']:,.2f}\n"
        f"  change      {pct}\n"
        f"  session     {q['session']}\n"
        f"  as of       {ts}\n"
        + (f"\nnote: {a.note}\n" if a.note else "")
    )


def _heartbeat(s, market: str) -> None:
    """Record that a pass completed, in the same transaction as its results.

    The poller is a separate container; this row is the only way the web app can
    tell a live poller from a dead one.
    """
    for key, value in ((HEARTBEAT_KEY, market), (LAST_ERROR_KEY, "")):
        row = s.get(Meta, key)
        if row is None:
            s.add(Meta(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utcnow()


def check_once(session_factory=Session) -> int:
    """One evaluation pass. Returns the number of alerts fired."""
    s = session_factory()
    fired = 0
    try:
        alerts = s.query(Alert).filter(Alert.active.is_(True)).all()
        market = quotes.market_session_now()
        # only fetch symbols that some alert actually wants evaluated right now
        wanted = {a.ticker for a in alerts
                  if market in TRADING_SESSIONS
                  and (a.extended_hours or market == "REG")}
        if not wanted:
            if alerts:
                log.info("market %s — no alerts eligible this session", market)
            _heartbeat(s, market)
            s.commit()
            return 0

        q = quotes.get_quotes(sorted(wanted),
                              include_extended=config.INCLUDE_EXTENDED_HOURS)
        now = utcnow()

        for a in alerts:
            if a.ticker not in q:
                continue
            if not a.extended_hours and q[a.ticker]["session"] != "REG":
                continue
            d = q[a.ticker]
            a.last_price = d["price"]
            if d.get("prev_close"):
                a.last_prev_close = d["prev_close"]
            a.last_checked_at = now
            hit = a.satisfied(d["price"], d["prev_close"])

            if _should_fire(a, hit, now):
                direction = "up" if a.kind in (Kind.price_above, Kind.pct_up) else "down"
                subject = f"{a.ticker} {'▲' if direction=='up' else '▼'} ${d['price']:,.2f} — {a.describe()}"
                body = _body(a, d)
                delivered = notify.deliver(subject, body, direction=direction)
                s.add(Fire(alert_id=a.id, price=d["price"], session=d["session"],
                           delivered=delivered, message=subject))
                a.last_fired_at = now
                a.armed = False
                if a.repeat == Repeat.once:
                    a.active = False
                fired += 1
                log.info("FIRED %s @ %.2f -> %s", a.describe(), d["price"], delivered or "no channel")
            elif not hit:
                a.armed = True          # re-arm once the condition clears

        _heartbeat(s, market)
        s.commit()
        return fired
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def run():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    log.info("engine up | poll=%ss | extended_hours=%s | channels=%s",
             config.POLL_SECONDS, config.INCLUDE_EXTENDED_HOURS,
             config.channels() or "NONE")
    while True:
        try:
            n = check_once()
            if n:
                log.info("%d alert(s) fired", n)
        except Exception as e:
            # never let a transient data error kill the loop
            log.error("poll failed: %s: %s", type(e).__name__, e)
            set_meta(LAST_ERROR_KEY, f"{type(e).__name__}: {e}")
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    run()
