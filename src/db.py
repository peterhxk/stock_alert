"""SQLite engine + session factory. WAL so the poller and the web app can
write concurrently without locking each other out."""
import datetime as dt
import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from src.config import DATA_DIR, DB_URL
from src.models import Base, Meta, utcnow

log = logging.getLogger(__name__)

DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(DB_URL, future=True, connect_args={"timeout": 15})


@event.listens_for(engine, "connect")
def _pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=15000")
    cur.close()


Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)


def _add_missing_columns():
    """Bring an existing database up to the current model.

    `create_all` creates missing tables but never alters existing ones, so a
    database written by an earlier version keeps its old columns and every
    query against a new one fails. This is deliberately the smallest thing that
    works: SQLite `ADD COLUMN` for nullable/defaulted columns only, which is all
    the schema has ever needed. Anything harder (a dropped or retyped column)
    would want a real migration tool.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not (col.nullable or col.default is not None):
                    log.warning("cannot add NOT NULL column %s.%s automatically",
                                table.name, col.name)
                    continue
                ddl = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" '
                                  f'ADD COLUMN "{col.name}" {ddl}'))
                log.info("migrated: added %s.%s", table.name, col.name)


def init_db():
    Base.metadata.create_all(engine)
    _add_missing_columns()


# ---- meta key/value, the only channel between the poller and the web process

HEARTBEAT_KEY = "engine_last_poll"     # ISO timestamp of the last completed pass
LAST_ERROR_KEY = "engine_last_error"   # "" when the last pass was clean


def set_meta(key: str, value: str, session_factory=Session) -> None:
    s = session_factory()
    try:
        row = s.get(Meta, key)
        if row is None:
            s.add(Meta(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utcnow()
        s.commit()
    except Exception:
        s.rollback()
        log.warning("could not write meta %s", key)
    finally:
        s.close()


def get_meta(key: str, session_factory=Session) -> tuple[str, dt.datetime | None]:
    """(value, updated_at-as-UTC-aware). ('', None) if never written."""
    s = session_factory()
    try:
        row = s.get(Meta, key)
        if row is None:
            return "", None
        ts = row.updated_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return row.value, ts
    finally:
        s.close()
