"""Tests for the web layer: validation, editing, liveness, login throttling.

Runs offline - the ticker lookup and the poller are both stubbed. Run:
    python -m tests.test_web
"""
import sys

from src import config, quotes, web
from src.db import HEARTBEAT_KEY, LAST_ERROR_KEY, Session, init_db, set_meta
from src.models import Alert, Fire, Kind, Meta, Repeat, utcnow

QUOTE = {"price": 100.0, "prev_close": 100.0, "ts": None, "session": "REG", "pct": 0.0}
KNOWN = {"AAPL", "MSFT", "SPY"}


def _stub_resolve(ticker):
    return dict(QUOTE) if ticker.upper() in KNOWN else None


def setup():
    quotes.resolve = _stub_resolve
    config.VALIDATE_TICKERS = True          # exercise the path, against the stub
    config.UI_PASSWORD = ""
    init_db()
    s = Session()
    s.query(Fire).delete()
    s.query(Alert).delete()
    s.query(Meta).delete()
    s.commit()
    s.close()
    web.app.config["TESTING"] = True
    return web.app.test_client()


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok


def _flashes(c):
    """Follow a redirect and pull the flash messages out of the rendered page."""
    html = c.get("/", follow_redirects=True).get_data(as_text=True)
    return [line for line in html.splitlines() if 'class="flash' in line]


def _only(s, **kw):
    return s.query(Alert).filter_by(**kw).one_or_none()


def main():
    c = setup()
    ok = True

    # ---- unknown tickers are rejected, not silently accepted
    r = c.post("/alerts", data={"ticker": "NOTREAL", "kind": "price_above",
                                "threshold": "10"})
    s = Session()
    ok &= check("unknown ticker not saved", s.query(Alert).count(), 0)
    s.close()
    ok &= check("unknown ticker flashes an error",
                any("did not resolve" in f for f in _flashes(c)), True)

    # ---- a known ticker saves, and is seeded with a price for the UI
    c.post("/alerts", data={"ticker": "aapl", "kind": "price_above",
                            "threshold": "150", "repeat": "rearm"})
    s = Session()
    a = _only(s, ticker="AAPL")
    ok &= check("known ticker saved, upper-cased", a is not None, True)
    ok &= check("seeded with last price", a.last_price, 100.0)
    aid = a.id
    s.close()

    # ---- a condition that is already true warns instead of surprising you
    c.post("/alerts", data={"ticker": "MSFT", "kind": "price_below",
                            "threshold": "500"})
    ok &= check("already-true condition warns",
                any("already satisfies" in f for f in _flashes(c)), True)

    # ---- form validation
    bad = [({"ticker": "", "threshold": "1"}, "Ticker is required"),
           ({"ticker": "AAPL", "threshold": "abc"}, "must be a number"),
           ({"ticker": "AAPL", "threshold": "-5"}, "positive, finite"),
           ({"ticker": "AAPL", "threshold": "nan"}, "positive, finite"),
           ({"ticker": "AAPL", "threshold": "inf"}, "positive, finite"),
           ({"ticker": "AA PL", "threshold": "5"}, "not a plausible ticker"),
           ({"ticker": "AAPL", "threshold": "150", "kind": "pct_up"}, "never fire")]
    for data, want in bad:
        c.post("/alerts", data=data)
        got = _flashes(c)
        ok &= check(f"rejects {data}", any(want in f for f in got), True)

    # ---- editing
    r = c.get(f"/alerts/{aid}/edit")
    ok &= check("edit form renders", r.status_code, 200)
    s = Session()
    s.get(Alert, aid).armed = False          # pretend it has fired
    s.commit(); s.close()

    c.post(f"/alerts/{aid}/edit", data={"ticker": "AAPL", "kind": "price_above",
                                        "threshold": "175", "repeat": "cooldown",
                                        "cooldown_minutes": "30", "note": "n"})
    s = Session()
    a = s.get(Alert, aid)
    ok &= check("edit updates threshold", a.threshold, 175.0)
    ok &= check("edit updates repeat", a.repeat, Repeat.cooldown)
    ok &= check("changing the condition re-arms", a.armed, True)
    ok &= check("unchecked box clears extended_hours", a.extended_hours, False)
    s.close()

    c.post(f"/alerts/{aid}/edit", data={"ticker": "AAPL", "kind": "price_above",
                                        "threshold": "0"})
    s = Session()
    ok &= check("invalid edit does not save", s.get(Alert, aid).threshold, 175.0)
    s.close()
    ok &= check("edit of a missing alert 404s",
                c.get("/alerts/99999/edit").status_code, 404)

    # ---- "to trigger" distance, including the percentage kinds
    pct = Alert(ticker="SPY", kind=Kind.pct_down, threshold=5.0,
                last_price=100.0, last_prev_close=100.0)
    ok &= check("pct alert has a trigger price", pct.trigger_price(), 95.0)
    ok &= check("pct distance is the move still needed",
                round(web._distance(pct), 2), -5.0)
    ok &= check("no distance without a snapshot",
                web._distance(Alert(ticker="X", kind=Kind.pct_up, threshold=5.0)), None)

    # ---- healthz reports the poller, not just itself
    body = c.get("/healthz")
    ok &= check("healthz fails when the poller never ran", body.status_code, 503)
    set_meta(HEARTBEAT_KEY, "REG")
    set_meta(LAST_ERROR_KEY, "")
    body = c.get("/healthz")
    ok &= check("healthz ok once the poller reports", body.status_code, 200)
    ok &= check("healthz reports the session",
                body.get_json()["engine"]["market_session"], "REG")

    # ---- login: throttled, constant-time, no open redirect
    config.UI_PASSWORD = "hunter2"
    c = web.app.test_client()
    web._failures.clear()
    ok &= check("protected page redirects to login",
                c.get("/").headers.get("Location", "").endswith("/login?next=/"), True)
    for _ in range(config.LOGIN_MAX_ATTEMPTS):
        c.post("/login", data={"password": "wrong"})
    r = c.post("/login", data={"password": "hunter2"}, follow_redirects=True)
    ok &= check("correct password refused while locked out",
                "Too many attempts" in r.get_data(as_text=True), True)
    web._failures.clear()
    r = c.post("/login", data={"password": "hunter2"})
    ok &= check("correct password signs in after the lockout clears",
                r.headers.get("Location"), "/")
    web._failures.clear()
    c2 = web.app.test_client()
    r = c2.post("/login?next=https://evil.example/x", data={"password": "hunter2"})
    ok &= check("external next= is ignored", r.headers.get("Location"), "/")
    config.UI_PASSWORD = ""

    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'}  (web)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
