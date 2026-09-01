"""SQLite engine + session factory. WAL so the poller and the web app can
write concurrently without locking each other out."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.config import DATA_DIR, DB_URL
from src.models import Base

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


def init_db():
    Base.metadata.create_all(engine)
