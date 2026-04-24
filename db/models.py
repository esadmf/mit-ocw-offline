from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    ForeignKey, BigInteger,
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True)
    slug = Column(String(500), unique=True, nullable=False, index=True)
    url = Column(String(1000), nullable=False)
    title = Column(String(500))
    description = Column(Text)
    department = Column(String(200))
    course_number = Column(String(50))
    level = Column(String(100))       # Undergraduate / Graduate
    term = Column(String(50))         # Fall / Spring / Summer / January
    year = Column(Integer)
    topics = Column(Text)             # JSON-encoded list
    image_url = Column(String(1000))

    # available | downloading | completed | failed
    status = Column(String(20), default="available", index=True)
    download_started_at = Column(DateTime)
    download_completed_at = Column(DateTime)
    download_error = Column(Text)
    local_path = Column(String(1000))

    page_count = Column(Integer, default=0)
    asset_count = Column(Integer, default=0)
    video_count = Column(Integer, default=0)
    total_size_bytes = Column(BigInteger, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    assets = relationship("Asset", back_populates="course", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    url = Column(String(2000), nullable=False)
    local_path = Column(String(1000))
    filename = Column(String(500))
    # html | pdf | video | image | archive | other
    asset_type = Column(String(20), default="other")
    size_bytes = Column(BigInteger)
    # pending | downloading | completed | failed | skipped
    status = Column(String(20), default="pending", index=True)
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    course = relationship("Course", back_populates="assets")
