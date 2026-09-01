"""Flask UI for managing alerts.

The page renders from `last_price`, which the engine writes on every poll, so
loading the UI costs no market-data requests and stays fast no matter how many
alerts exist. The "distance" column is computed from that same snapshot.
"""
import datetime as dt
import functools
import hmac
import math
import time

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)

from src import config
from src.db import HEARTBEAT_KEY, LAST_ERROR_KEY, Session, get_meta, init_db
from src.models import Alert, Fire, Kind, Repeat

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if config.UI_PASSWORD and not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


# Failed login timestamps per client address. In-process and deliberately so:
# adding a store for this would be more moving parts than a single-user app
# needs. Behind a reverse proxy every request shares the proxy's address, so the
# limit becomes global rather than per-client - still the right outcome here.
_failures: dict[str, list[float]] = {}


def _throttled(ip: str) -> int:
    """Seconds left in the lockout for this address, 0 if not locked out."""
    now = time.monotonic()
    window = config.LOGIN_LOCKOUT_SECONDS
    hits = [t for t in _failures.get(ip, []) if now - t < window]
    if hits:
        _failures[ip] = hits
    else:
        _failures.pop(ip, None)
    if len(hits) < config.LOGIN_MAX_ATTEMPTS:
        return 0
    # locked out until the oldest of the most recent MAX_ATTEMPTS failures ages
    # out of the window
    oldest = hits[-config.LOGIN_MAX_ATTEMPTS]
    return max(1, int(window - (now - oldest)))


def _record_failure(ip: str) -> None:
    if len(_failures) > 1024:               # keep a flood from growing the dict
        _failures.clear()
    _failures.setdefault(ip, []).append(time.monotonic())


@app.route("/login", methods=["GET", "POST"])
def login():
    if not config.UI_PASSWORD:
        return redirect(url_for("index"))
    ip = request.remote_addr or "?"
    if request.method == "POST":
        wait = _throttled(ip)
        if wait:
            flash(f"Too many attempts. Try again in {wait}s.", "error")
        elif hmac.compare_digest(request.form.get("password", ""),
                                 config.UI_PASSWORD):
            _failures.pop(ip, None)
            session["ok"] = True
            return redirect(_safe_next() or url_for("index"))
        else:
            _record_failure(ip)
            flash("Incorrect password.", "error")
    return render_template("login.html")


def _safe_next() -> str:
    """Only ever redirect within this app - never to a URL an attacker supplied."""
    nxt = request.args.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else ""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _distance(a: Alert):
    """How far the last observed price is from firing, as a signed %.

    Works for the percentage kinds too, via the price their condition currently
    implies - so "AAPL falls at least 3%" gets the same one-glance number as a
    plain price level.
    """
    trigger = a.trigger_price()
    if a.last_price is None or not trigger:
        return None
    return (trigger / a.last_price - 1.0) * 100.0


class FormError(ValueError):
    """A form field the user can fix, with a message worth showing them."""


def _fields(f) -> dict:
    """Parse and validate the alert form, shared by create and edit."""
    ticker = (f.get("ticker") or "").strip().upper()
    if not ticker:
        raise FormError("Ticker is required.")
    if len(ticker) > 16 or not all(c.isalnum() or c in ".-^=" for c in ticker):
        raise FormError(f"{ticker!r} is not a plausible ticker symbol.")
    try:
        threshold = float(f.get("threshold", ""))
    except (TypeError, ValueError):
        raise FormError("Threshold must be a number.")
    # nan and inf both survive a plain "<= 0" test, and either one produces an
    # alert whose condition can never be true
    if not math.isfinite(threshold) or threshold <= 0:
        raise FormError("Threshold must be a positive, finite number.")
    try:
        kind = Kind(f.get("kind", "price_above"))
        repeat = Repeat(f.get("repeat", "rearm"))
    except ValueError:
        raise FormError("Unknown condition or repeat mode.")
    if kind in (Kind.pct_up, Kind.pct_down) and threshold > 100:
        raise FormError("A percentage threshold above 100% will never fire.")
    try:
        cooldown = max(1, int(f.get("cooldown_minutes") or 60))
    except (TypeError, ValueError):
        raise FormError("Cooldown must be a whole number of minutes.")
    return {"ticker": ticker, "kind": kind, "threshold": threshold,
            "repeat": repeat, "cooldown_minutes": cooldown,
            "extended_hours": bool(f.get("extended_hours")),
            "note": (f.get("note") or "").strip()[:500]}


def _lookup(ticker: str):
    """(known, quote, warning) for a symbol.

    A typo is otherwise completely silent - the alert sits in the list looking
    healthy and simply never fires. A *lookup failure* is different from an
    unknown symbol, though: Yahoo being down must not stop you adding alerts,
    so that path warns and lets it through.
    """
    if not config.VALIDATE_TICKERS:
        return True, None, ""
    from src import quotes           # lazy: keeps pandas out of web startup
    try:
        q = quotes.resolve(ticker)
    except quotes.QuoteError as e:
        return True, None, f"Could not verify {ticker} right now ({e}). Added anyway."
    if q is None:
        return False, None, (f"{ticker} did not resolve to a symbol Yahoo knows. "
                             "Check the spelling, or set VALIDATE_TICKERS=0 to skip this check.")
    return True, q, ""


def _engine_status() -> dict:
    """What the poller last did, from the row it writes each pass."""
    market, seen = get_meta(HEARTBEAT_KEY)
    err, _ = get_meta(LAST_ERROR_KEY)
    age = None if seen is None else (dt.datetime.now(dt.timezone.utc) - seen).total_seconds()
    return {"seen_at": seen, "age_seconds": None if age is None else int(age),
            "market_session": market or None, "last_error": err or None,
            "stale": age is None or age > config.ENGINE_STALE_SECONDS}


@app.route("/")
@login_required
def index():
    s = Session()
    try:
        alerts = s.query(Alert).order_by(Alert.active.desc(), Alert.ticker).all()
        rows = [{"a": a, "distance": _distance(a)} for a in alerts]
        recent = s.query(Fire).order_by(Fire.fired_at.desc()).limit(8).all()
        return render_template("index.html", rows=rows, recent=recent,
                               kinds=list(Kind), repeats=list(Repeat),
                               channels=config.channels(),
                               poll=config.POLL_SECONDS,
                               engine=_engine_status())
    finally:
        s.close()


@app.post("/alerts")
@login_required
def create():
    try:
        vals = _fields(request.form)
    except FormError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))

    known, quote, warning = _lookup(vals["ticker"])
    if not known:
        flash(warning, "error")
        return redirect(url_for("index"))

    s = Session()
    try:
        a = Alert(**vals)
        if quote:
            # seed the snapshot so the row shows a price before the first poll
            a.last_price = quote["price"]
            a.last_prev_close = quote.get("prev_close")
        s.add(a)
        s.commit()
        flash(f"Added: {a.describe()}", "ok")
        if warning:
            flash(warning, "warn")
        elif quote and a.satisfied(quote["price"], quote.get("prev_close")):
            # armed alerts fire on the next poll, so a condition that is already
            # true is about to notify immediately - say so rather than surprise
            flash(f"Heads up: {vals['ticker']} already satisfies this condition "
                  f"at ${quote['price']:,.2f}, so it will fire on the next poll.",
                  "warn")
    except Exception as e:
        s.rollback()
        flash(f"Could not add alert: {type(e).__name__}", "error")
    finally:
        s.close()
    return redirect(url_for("index"))


@app.route("/alerts/<int:aid>/edit", methods=["GET", "POST"])
@login_required
def edit(aid):
    s = Session()
    try:
        a = s.get(Alert, aid)
        if a is None:
            abort(404)
        if request.method == "GET":
            return render_template("edit.html", a=a)
        try:
            vals = _fields(request.form)
        except FormError as e:
            flash(str(e), "error")
            return redirect(url_for("edit", aid=aid))

        changed_ticker = vals["ticker"] != a.ticker
        changed_condition = (changed_ticker or vals["kind"] != a.kind
                             or vals["threshold"] != a.threshold)
        if changed_condition:
            known, quote, warning = _lookup(vals["ticker"])
            if not known:
                flash(warning, "error")
                return redirect(url_for("edit", aid=aid))
            if warning:
                flash(warning, "warn")
        for k, v in vals.items():
            setattr(a, k, v)
        if changed_condition:
            # the armed/fired state described the old condition; start clean
            a.armed = True
        if changed_ticker:
            a.last_price = a.last_prev_close = None
            a.last_checked_at = None
        s.commit()
        flash(f"Updated: {a.describe()}", "ok")
        return redirect(url_for("index"))
    finally:
        s.close()


@app.post("/alerts/<int:aid>/toggle")
@login_required
def toggle(aid):
    s = Session()
    try:
        a = s.get(Alert, aid)
        if a:
            a.active = not a.active
            if a.active:
                a.armed = True          # re-arm when re-enabled
            s.commit()
    finally:
        s.close()
    return redirect(url_for("index"))


@app.post("/alerts/<int:aid>/delete")
@login_required
def delete(aid):
    s = Session()
    try:
        a = s.get(Alert, aid)
        if a:
            s.delete(a)
            s.commit()
            flash("Deleted.", "ok")
    finally:
        s.close()
    return redirect(url_for("index"))


@app.route("/history")
@login_required
def history():
    s = Session()
    try:
        fires = s.query(Fire).order_by(Fire.fired_at.desc()).limit(200).all()
        return render_template("history.html", fires=fires)
    finally:
        s.close()


@app.route("/healthz")
def healthz():
    """Uptime-monitor endpoint. Non-200 when the poller has stopped reporting.

    Returning 200 for a live web app with a dead poller would make this useless:
    the UI keeps serving perfectly while no alert ever fires again.
    """
    s = Session()
    try:
        n = s.query(Alert).filter(Alert.active.is_(True)).count()
        eng = _engine_status()
        body = {"ok": not eng["stale"], "active_alerts": n,
                "channels": config.channels(),
                "engine": {"last_poll_seconds_ago": eng["age_seconds"],
                           "market_session": eng["market_session"],
                           "last_error": eng["last_error"],
                           "stale_after_seconds": config.ENGINE_STALE_SECONDS}}
        return body, (200 if body["ok"] else 503)
    finally:
        s.close()


@app.context_processor
def _globals():
    return {"config_has_password": bool(config.UI_PASSWORD)}


@app.template_filter("ago")
def ago(value):
    if not value:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    secs = (dt.datetime.now(dt.timezone.utc) - value).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
