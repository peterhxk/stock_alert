# price-alerts

Self-hosted stock price alerting. Add alerts in a web UI, get email and phone
push when they trigger — including during pre-market and after-hours.

<!-- screenshot goes here once you have it running -->

## What it does

- **Four condition types** — price above/below a level, or up/down at least N%
  against the previous close.
- **Extended-hours aware.** Yahoo's *daily* bars contain no extended-hours data
  at all, so quotes come from 1-minute bars with `prepost=True`, covering
  04:00–20:00 ET. Alerts can opt out per-alert if you only care about the
  regular session.
- **Fires on transitions, not on every poll.** See below — this is the
  difference between an alerter and a spam bot.
- **Two channels, both optional** — SMTP email and [ntfy.sh](https://ntfy.sh)
  push. A failure in one never blocks the other or kills the poller.
- **History** of everything that fired, including which channels actually
  delivered.

## Firing semantics

A naive alerter re-sends every poll for as long as the condition holds — "AAPL
above 300" becomes 60 emails an hour. Alerts here fire on the **transition**
into the condition:

```
condition false          -> armed = True
condition true + armed   -> FIRE, armed = False
```

What happens next is per-alert:

| Repeat mode | Behaviour |
|---|---|
| `once` | Fires a single time, then disables itself. |
| `rearm` *(default)* | Re-arms only when the condition goes false again — one notification per **crossing**. Usually what you want. |
| `cooldown` | Re-fires while still true, at most once per `cooldown_minutes`. |

This is covered by tests:

```bash
python -m tests.test_engine    # 15 assertions, offline, sends nothing
```

## Run it

```bash
cp .env.example .env     # fill in at least one delivery channel
docker compose up -d     # web UI on http://localhost:8080
```

Two services share one image: `web` (Flask UI) and `engine` (the poller). They
are split so a slow notification or a Yahoo hiccup can't block the UI, and
restarting the UI can't drop the poller. SQLite lives on a named volume, so
alerts survive rebuilds.

Without Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.engine     # poller, one terminal
.venv/bin/python -m src.web        # UI, another
```

## Configuration

All via `.env` — see `.env.example`. Nothing secret is committed.

| Variable | Purpose |
|---|---|
| `NOTIFY_SMTP_PASS`, `NOTIFY_TO_EMAIL` | Email delivery. Gmail needs an [App Password](https://myaccount.google.com/apppasswords), not your account password. |
| `NTFY_TOPIC` | Phone push. Pick an **unguessable** topic — anyone who knows it can read your alerts. |
| `POLL_SECONDS` | Poll interval, default 60. |
| `INCLUDE_EXTENDED_HOURS` | Whether to fetch pre/post-market quotes at all. |
| `UI_PASSWORD` | Requires a login. **Set this before exposing the UI beyond localhost.** |

The UI tells you when no channel is configured — alerts still fire and are
recorded, but nothing gets sent.

## Deploying somewhere always-on

The point of the container is that a laptop sleeps and misses alerts. The same
image runs unchanged on any always-on host — a $5 VPS, Fly.io, a Raspberry Pi.
Copy `.env` across and `docker compose up -d`.

If you expose it publicly, **set `UI_PASSWORD`** and put it behind TLS. There is
no rate limiting on the login form, so a public deployment should sit behind a
reverse proxy that has some.

## Notes and limits

- **Yahoo is the only data source**, unofficial and unsupported. It rate-limits,
  occasionally returns nothing, and can change without notice. The poller logs
  and continues rather than dying, but this is not something to trade off.
- **Quotes are last-trade, delayed**, not real-time bid/ask. Fine for "tell me
  when AAPL crosses 330"; not for execution.
- **No extended-hours volume.** Yahoo reports zero for essentially all
  pre/post-market bars, so volume-based conditions would be meaningless outside
  the regular session and aren't offered.
- **`healthz`** returns active alert count and configured channels, for uptime
  monitoring.
