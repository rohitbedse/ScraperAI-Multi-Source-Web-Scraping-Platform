from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from app import config
from app.db.models import Base

config.ensure_dirs()
engine = create_engine(config.DB_URL, connect_args={"check_same_thread": False, "timeout": 30})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    # tiny migration for databases created by an earlier version
    cols = {c["name"] for c in inspect(engine).get_columns("job_errors")}
    with engine.begin() as c:
        if "reason" in cols and "technical_message" not in cols:
            c.execute(text("ALTER TABLE job_errors RENAME COLUMN reason TO technical_message"))
        if "error_type" not in cols:
            c.execute(text("ALTER TABLE job_errors ADD COLUMN error_type VARCHAR(128)"))


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
