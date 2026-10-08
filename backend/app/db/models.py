from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True, default="queued")
    params: Mapped[str] = mapped_column(Text, default="{}")          # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_done: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    output_path: Mapped[str | None] = mapped_column(String(255), nullable=True)   # file name in outputs/
    stage: Mapped[str] = mapped_column(String(32), default="INIT")
    percent: Mapped[float] = mapped_column(Float, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    stats: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON from the final "done" event


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)   # also the SSE id
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text)                        # Event JSON


class JobError(Base):
    __tablename__ = "job_errors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    stage: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    technical_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    function: Mapped[str | None] = mapped_column(String(255), nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    job_continues: Mapped[bool] = mapped_column(Boolean, default=True)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)   # server log + job details only


class OutputFile(Base):
    __tablename__ = "output_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expired: Mapped[bool] = mapped_column(Boolean, default=False)
