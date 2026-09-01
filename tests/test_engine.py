"""Tests for the firing semantics — the part that decides whether this thing is
usable or a spam bot. Run: python -m tests.test_engine

Market data and delivery are both stubbed, so this runs offline and sends
nothing.
"""
import sys

from sqlalchemy import inspect, text

from src import engine, notify, quotes
from src.db import HEARTBEAT_KEY, Session, engine as db_engine, get_meta, init_db
from src.models import Alert, Fire, Kind, Meta, Repeat

PRICE = {"v": 100.0}
SENT = []


def _stub_quotes(tickers, include_extended=True):
    return {t: {"price": PRICE["v"], "prev_close": 100.0, "ts": "2026-01-02T15:00:00-05:00",
                "session": "REG", "pct": (PRICE["v"] / 100.0 - 1) * 100}
            for t in tickers}


def _stub_deliver(subject, body, direction="up"):
    SENT.append(subject)
    return "stub"


def setup():
    quotes.get_quotes = _stub_quotes
    quotes.market_session_now = lambda: "REG"
    notify.deliver = _stub_deliver
    init_db()
    s = Session()
    s.query(Fire).delete()
    s.query(Alert).delete()
    s.commit()
    return s


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got}, want {want}")
    return ok


def main():
    s = setup()
    ok = True

    # ---- schema migration: an old database gains the new columns on boot
    try:
        with db_engine.begin() as conn:
            conn.execute(text('ALTER TABLE alerts DROP COLUMN last_prev_close'))
    except Exception as e:                      # sqlite < 3.35 cannot drop
        print(f"  SKIP  migration test ({type(e).__name__})")
    else:
        init_db()
        cols = {c["name"] for c in inspect(db_engine).get_columns("alerts")}
        ok &= check("missing column is migrated in", "last_prev_close" in cols, True)

    # ---- rearm: fires on the crossing, not on every poll
    a = Alert(ticker="TEST", kind=Kind.price_above, threshold=110.0, repeat=Repeat.rearm)
    s.add(a); s.commit(); s.close()

    PRICE["v"] = 100.0
    ok &= check("below threshold does not fire", engine.check_once(), 0)

    PRICE["v"] = 115.0
    ok &= check("crossing up fires once", engine.check_once(), 1)
    ok &= check("still above does not re-fire", engine.check_once(), 0)
    ok &= check("still above, again", engine.check_once(), 0)

    PRICE["v"] = 105.0
    ok &= check("dropping back does not fire", engine.check_once(), 0)
    PRICE["v"] = 120.0
    ok &= check("second crossing fires again", engine.check_once(), 1)

    # ---- once: fires a single time, then deactivates itself
    s = Session(); s.query(Fire).delete(); s.query(Alert).delete(); s.commit()
    b = Alert(ticker="TEST", kind=Kind.price_above, threshold=110.0, repeat=Repeat.once)
    s.add(b); s.commit(); bid = b.id; s.close()

    PRICE["v"] = 115.0
    ok &= check("once fires", engine.check_once(), 1)
    ok &= check("once does not re-fire", engine.check_once(), 0)
    s = Session()
    ok &= check("once deactivated itself", s.get(Alert, bid).active, False)
    s.close()

    # ---- price_below and pct conditions
    s = Session(); s.query(Fire).delete(); s.query(Alert).delete(); s.commit()
    s.add(Alert(ticker="TEST", kind=Kind.price_below, threshold=90.0))
    s.add(Alert(ticker="TEST", kind=Kind.pct_down, threshold=5.0))
    s.commit(); s.close()

    PRICE["v"] = 96.0     # -4%: short of the -5% trigger, and above the 90 floor
    ok &= check("above floor, only -4%: no fire", engine.check_once(), 0)
    PRICE["v"] = 95.0     # exactly -5%: "falls at least 5%" should fire
    ok &= check("exactly -5% fires pct_down", engine.check_once(), 1)
    PRICE["v"] = 96.0     # clears, so pct_down re-arms
    ok &= check("recovering re-arms", engine.check_once(), 0)
    PRICE["v"] = 85.0     # -15% vs prev_close 100, and below 90
    ok &= check("below floor AND -15%: both fire", engine.check_once(), 2)

    # ---- extended-hours gating
    s = Session(); s.query(Fire).delete(); s.query(Alert).delete(); s.commit()
    s.add(Alert(ticker="TEST", kind=Kind.price_above, threshold=50.0, extended_hours=False))
    s.commit(); s.close()
    quotes.market_session_now = lambda: "POST"
    ok &= check("regular-hours-only alert skipped after close", engine.check_once(), 0)
    quotes.market_session_now = lambda: "CLOSED"
    ok &= check("nothing evaluated when market closed", engine.check_once(), 0)

    # ---- heartbeat: every pass records that the poller is alive, including
    # the passes that evaluate nothing, which are most of the day
    s = Session(); s.query(Fire).delete(); s.query(Alert).delete()
    s.query(Meta).delete(); s.commit(); s.close()
    quotes.market_session_now = lambda: "CLOSED"
    engine.check_once()
    market, seen = get_meta(HEARTBEAT_KEY)
    ok &= check("heartbeat written with no alerts at all", market, "CLOSED")
    ok &= check("heartbeat carries a timestamp", seen is not None, True)

    quotes.market_session_now = lambda: "REG"
    s = Session(); s.add(Alert(ticker="TEST", kind=Kind.price_above, threshold=999.0))
    s.commit(); s.close()
    engine.check_once()
    market, second = get_meta(HEARTBEAT_KEY)
    ok &= check("heartbeat updates on the next pass", market, "REG")
    ok &= check("heartbeat timestamp moves forward", second > seen, True)

    # the same value twice in a row must still refresh the timestamp: an ORM
    # that skips the UPDATE because nothing looks dirty would age the heartbeat
    # out and make a perfectly healthy poller read as dead
    engine.check_once()
    _, again = get_meta(HEARTBEAT_KEY)
    ok &= check("unchanged heartbeat value still refreshes", again > second, True)

    # a poll that raises must NOT look like a healthy pass
    def _boom(*a, **kw):
        raise RuntimeError("yahoo said no")
    quotes.get_quotes = _boom
    try:
        engine.check_once()
    except RuntimeError:
        pass
    _, third = get_meta(HEARTBEAT_KEY)
    ok &= check("a failed poll leaves the heartbeat untouched", third, again)
    quotes.get_quotes = _stub_quotes

    s = Session(); s.query(Fire).delete(); s.query(Alert).delete()
    s.query(Meta).delete(); s.commit(); s.close()
    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'}  ({len(SENT)} stub notifications)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
