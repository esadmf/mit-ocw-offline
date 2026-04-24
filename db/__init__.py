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
def _set_wal_mode(dbapi_conn, _):
    # WAL mode allows concurrent readers alongside a single writer,
    # which prevents "database is locked" when server and worker overlap.
    dbapi_conn.execute("PRAGMA journal_mode=WAL")

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
