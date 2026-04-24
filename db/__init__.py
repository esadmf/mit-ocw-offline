from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base
import config

engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,   # drop and replace stale connections automatically
    pool_size=5,
    max_overflow=10,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
