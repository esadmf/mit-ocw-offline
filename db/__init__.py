from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from .models import Base
import config

engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={
        "check_same_thread": False,
        "timeout": 30,  # wait up to 30s for a lock to clear before raising
    },
)

@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_conn, _):
    # busy_timeout requires no lock — safe to set even when another process
    # has the DB open. Tells SQLite to retry for 30s before raising BUSY.
    dbapi_conn.execute("PRAGMA busy_timeout=30000")
    # WAL mode needs exclusive access to change. Attempt it, but don't crash
    # if another process already has the file open — WAL will be applied on
    # the next connect when the DB is idle, and busy_timeout covers us until then.
    try:
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
