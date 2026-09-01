"""Batched quote fetching, including pre-market and after-hours.

Yahoo's daily bars contain no extended-hours data, so the last price comes from
1-minute bars with prepost=True (04:00-20:00 ET). Previous close comes from the
daily series and is cached, since it only changes once a day.

One request covers every ticker being watched, so poll cost does not grow with
the number of alerts - only with the number of distinct symbols.
"""
import datetime as dt
import threading
import warnings

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ET = "America/New_York"
PRE_OPEN, MKT_OPEN, MKT_CLOSE, POST_CLOSE = 4.0, 9.5, 16.0, 20.0

_prev_close_cache: dict[str, tuple[float, dt.date]] = {}
_lock = threading.Lock()


def session_at(ts) -> str:
    """PRE / REG / POST / CLOSED for a timestamp, in US/Eastern."""
    if ts is None:
        return "CLOSED"
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC").tz_convert(ET) if t.tzinfo is None else t.tz_convert(ET)
    if t.weekday() >= 5:
        return "CLOSED"
    h = t.hour + t.minute / 60.0
    if PRE_OPEN <= h < MKT_OPEN:
        return "PRE"
    if MKT_OPEN <= h < MKT_CLOSE:
        return "REG"
    if MKT_CLOSE <= h < POST_CLOSE:
        return "POST"
    return "CLOSED"


def market_session_now() -> str:
    return session_at(pd.Timestamp.now(tz=ET))


def _prev_closes(tickers):
    """Previous session's close per ticker, cached for the day."""
    today = dt.date.today()
    with _lock:
        need = [t for t in tickers
                if t not in _prev_close_cache or _prev_close_cache[t][1] != today]
    if need:
        df = yf.download(need, period="7d", interval="1d", progress=False,
                         auto_adjust=False, threads=False, group_by="column")
        if not df.empty:
            close = df["Close"]
            if isinstance(close, pd.Series):          # single ticker
                close = close.to_frame(need[0])
            for t in need:
                if t not in close.columns:
                    continue
                s = close[t].dropna()
                if len(s) == 0:
                    continue
                # if the last bar is today's (in-progress), step back one
                last_day = pd.Timestamp(s.index[-1]).date()
                val = s.iloc[-2] if (last_day == today and len(s) >= 2) else s.iloc[-1]
                with _lock:
                    _prev_close_cache[t] = (float(val), today)
    with _lock:
        return {t: _prev_close_cache[t][0] for t in tickers if t in _prev_close_cache}


def get_quotes(tickers, include_extended=True):
    """{ticker: {price, prev_close, ts, session, pct}} for every resolvable symbol."""
    tickers = sorted({t.strip().upper() for t in tickers if t and t.strip()})
    if not tickers:
        return {}

    df = yf.download(tickers, period="1d", interval="1m", prepost=include_extended,
                     progress=False, auto_adjust=False, threads=False, group_by="column")
    prev = _prev_closes(tickers)
    out = {}
    if df.empty:
        return out

    close = df["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])

    for t in tickers:
        if t not in close.columns:
            continue
        s = close[t].dropna()
        if len(s) == 0:
            continue
        ts = s.index[-1]
        price = float(s.iloc[-1])
        pc = prev.get(t)
        out[t] = {
            "price": price,
            "prev_close": pc,
            "ts": ts,
            "session": session_at(ts),
            "pct": (price / pc - 1.0) * 100.0 if pc else None,
        }
    return out


if __name__ == "__main__":
    q = get_quotes(["AAPL", "NVDA", "SPY", "NOTAREALTICKER"])
    print(f"market session now: {market_session_now()}\n")
    print(f"{'ticker':<8}{'price':>10}{'prev close':>12}{'chg%':>8}{'session':>9}  as of")
    for t, d in q.items():
        pct = f"{d['pct']:+.2f}" if d["pct"] is not None else "n/a"
        print(f"{t:<8}{d['price']:>10.2f}{d['prev_close']:>12.2f}{pct:>8}{d['session']:>9}  "
              f"{pd.Timestamp(d['ts']).tz_convert(ET).strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"\nunresolved: {sorted(set(['AAPL','NVDA','SPY','NOTAREALTICKER']) - set(q))}")
