"""Flask UI for managing alerts.

The page renders from `last_price`, which the engine writes on every poll, so
loading the UI costs no market-data requests and stays fast no matter how many
alerts exist. The "distance" column is computed from that same snapshot.
"""
import datetime as dt
import functools

from flask import (Flask, flash, redirect, render_template, request, session,
                   url_for)

from src import config
from src.db import Session, init_db
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


@app.route("/login", methods=["GET", "POST"])
def login():
    if not config.UI_PASSWORD:
        return redirect(url_for("index"))
    if request.method == "POST":
        if request.form.get("password") == config.UI_PASSWORD:
            session["ok"] = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("Incorrect password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _distance(a: Alert):
    """How far the last observed price is from firing, as a signed %."""
    if a.last_price is None:
        return None
    if a.kind in (Kind.price_above, Kind.price_below):
        if not a.threshold:
            return None
        return (a.threshold / a.last_price - 1.0) * 100.0
    return None


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
                               poll=config.POLL_SECONDS)
    finally:
        s.close()


@app.post("/alerts")
@login_required
def create():
    f = request.form
    ticker = (f.get("ticker") or "").strip().upper()
    if not ticker:
        flash("Ticker is required.", "error")
        return redirect(url_for("index"))
    try:
        threshold = float(f.get("threshold", ""))
    except ValueError:
        flash("Threshold must be a number.", "error")
        return redirect(url_for("index"))
    if threshold <= 0:
        flash("Threshold must be greater than zero.", "error")
        return redirect(url_for("index"))

    s = Session()
    try:
        a = Alert(
            ticker=ticker,
            kind=Kind(f.get("kind", "price_above")),
            threshold=threshold,
            repeat=Repeat(f.get("repeat", "rearm")),
            cooldown_minutes=max(1, int(f.get("cooldown_minutes") or 60)),
            extended_hours=bool(f.get("extended_hours")),
            note=(f.get("note") or "").strip()[:500],
        )
        s.add(a)
        s.commit()
        flash(f"Added: {a.describe()}", "ok")
    except Exception as e:
        s.rollback()
        flash(f"Could not add alert: {type(e).__name__}", "error")
    finally:
        s.close()
    return redirect(url_for("index"))


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
    s = Session()
    try:
        n = s.query(Alert).filter(Alert.active.is_(True)).count()
        return {"ok": True, "active_alerts": n,
                "channels": config.channels()}, 200
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
