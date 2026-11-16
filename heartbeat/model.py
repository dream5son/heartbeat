import calendar

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker

from .config import DATABASE_URL


Base = declarative_base()
DEFAULT_SUCCESS_RULE = "status==200"


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    parent_id = Column(Integer, ForeignKey("services.id"), nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)

    parent = relationship("Service", remote_side=[id], backref="children")
    endpoints = relationship(
        "ServiceEndpoint",
        cascade="all, delete-orphan",
        back_populates="service",
    )


class ServiceEndpoint(Base):
    __tablename__ = "service_endpoints"

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False, index=True)
    heartbeat_url = Column(String(2048), nullable=False)
    success_rule = Column(String(512), nullable=False, default=DEFAULT_SUCCESS_RULE)

    service = relationship("Service", back_populates="endpoints")


class DailyCheck(Base):
    __tablename__ = "daily_checks"
    __table_args__ = (UniqueConstraint("service_id", "date", name="uq_daily_service_date"),)

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)


for minute_idx in range(24 * 60):
    setattr(DailyCheck, f"minute_{minute_idx:04d}", Column(Integer, nullable=True))


class YearlyCheck(Base):
    __tablename__ = "yearly_checks"
    __table_args__ = (UniqueConstraint("service_id", "year", name="uq_yearly_service_year"),)

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)


for month in range(1, 13):
    _, month_days = calendar.monthrange(2024 if month == 2 else 2025, month)
    if month == 2:
        month_days = 29
    for day in range(1, month_days + 1):
        setattr(YearlyCheck, f"day_{month:02d}{day:02d}", Column(Integer, nullable=True))


class FailureRecord(Base):
    __tablename__ = "failure_records"

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False, index=True)
    check_time = Column(DateTime, nullable=False, index=True)
    status = Column(Integer, nullable=False)
    log = Column(Text, nullable=False)


engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))


def ensure_schema() -> None:
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        tables = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='service_endpoints'"
        ).fetchall()
        if not tables:
            return
        columns = conn.exec_driver_sql("PRAGMA table_info(service_endpoints)").fetchall()
        if any(column[1] == "success_rule" for column in columns):
            return
        conn.exec_driver_sql(
            "ALTER TABLE service_endpoints ADD COLUMN success_rule VARCHAR(512) DEFAULT 'status==200' NOT NULL"
        )


def minute_column(check_time):
    return f"minute_{(check_time.hour * 60 + check_time.minute):04d}"


def day_column(check_date):
    return f"day_{check_date.month:02d}{check_date.day:02d}"


def normalize_success_rule(success_rule):
    if success_rule is None:
        return DEFAULT_SUCCESS_RULE
    normalized = str(success_rule).strip()
    return normalized or DEFAULT_SUCCESS_RULE
