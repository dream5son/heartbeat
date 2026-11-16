import calendar
import datetime as dt
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import schedule
from flask import Flask, abort, jsonify, render_template, request
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


DATABASE_URL = os.getenv("HEARTBEAT_DB", "sqlite:///heartbeat.db")

Base = declarative_base()


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


def minute_column(check_time: dt.datetime) -> str:
    return f"minute_{(check_time.hour * 60 + check_time.minute):04d}"


def day_column(check_date: dt.date) -> str:
    return f"day_{check_date.month:02d}{check_date.day:02d}"


def check_url(heartbeat_url: str, timeout: int = 8) -> tuple[bool, int, str]:
    try:
        with urllib.request.urlopen(heartbeat_url, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return status == 200, int(status), f"status={status}"
    except urllib.error.HTTPError as err:
        return False, int(err.code), str(err)
    except Exception as err:  # noqa: BLE001
        return False, -1, str(err)


def _get_or_create_daily(session: Any, service_id: int, check_date: dt.date) -> DailyCheck:
    daily = (
        session.query(DailyCheck)
        .filter(DailyCheck.service_id == service_id, DailyCheck.date == check_date)
        .one_or_none()
    )
    if daily is None:
        daily = DailyCheck(service_id=service_id, date=check_date)
        session.add(daily)
    return daily


def _get_or_create_yearly(session: Any, service_id: int, year: int) -> YearlyCheck:
    yearly = (
        session.query(YearlyCheck)
        .filter(YearlyCheck.service_id == service_id, YearlyCheck.year == year)
        .one_or_none()
    )
    if yearly is None:
        yearly = YearlyCheck(service_id=service_id, year=year)
        session.add(yearly)
    return yearly


def perform_service_check(
    session: Any,
    service: Service,
    check_time: dt.datetime,
    checker=check_url,
) -> bool:
    minute_col = minute_column(check_time)
    day_col = day_column(check_time.date())

    endpoint_results: list[tuple[bool, int, str, str]] = []
    for endpoint in service.endpoints:
        ok, status_code, detail = checker(endpoint.heartbeat_url)
        endpoint_results.append((ok, status_code, detail, endpoint.heartbeat_url))

    if endpoint_results:
        service_ok = all(item[0] for item in endpoint_results)
    else:
        service_ok = False
        endpoint_results.append((False, -1, "no heartbeat urls configured", ""))

    daily = _get_or_create_daily(session, service.id, check_time.date())
    setattr(daily, minute_col, 1 if service_ok else 0)

    yearly = _get_or_create_yearly(session, service.id, check_time.year)
    previous_day = getattr(yearly, day_col)
    if service_ok and previous_day is None:
        setattr(yearly, day_col, 1)
    if not service_ok:
        setattr(yearly, day_col, 0)

    if not service_ok:
        for ok, status_code, detail, heartbeat_url in endpoint_results:
            if ok:
                continue
            session.add(
                FailureRecord(
                    service_id=service.id,
                    check_time=check_time,
                    status=status_code,
                    log=f"url={heartbeat_url or '-'} detail={detail}",
                )
            )

    return service_ok


def run_health_checks(check_time: dt.datetime | None = None) -> None:
    check_time = check_time or dt.datetime.now().replace(second=0, microsecond=0)
    session = SessionLocal()
    try:
        services = session.query(Service).filter(Service.enabled.is_(True)).all()
        for service in services:
            perform_service_check(session, service, check_time)
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        raise
    finally:
        session.close()


def _daily_row_health(daily: DailyCheck | None) -> int | None:
    if daily is None:
        return None

    saw_ok = False
    for minute_idx in range(24 * 60):
        value = getattr(daily, f"minute_{minute_idx:04d}")
        if value == 0:
            return 0
        if value == 1:
            saw_ok = True

    return 1 if saw_ok else None


def service_to_dict(service: Service) -> dict[str, Any]:
    return {
        "id": service.id,
        "name": service.name,
        "parent_id": service.parent_id,
        "enabled": service.enabled,
        "heartbeat_urls": [e.heartbeat_url for e in service.endpoints],
        "child_service_ids": [child.id for child in service.children],
    }


def create_app() -> Flask:
    app = Flask(__name__)

    @app.teardown_appcontext
    def shutdown_session(_: Any) -> None:
        SessionLocal.remove()

    @app.get("/")
    def index() -> Any:
        session = SessionLocal()
        try:
            services = session.query(Service).all()
            return jsonify({"services": [service_to_dict(s) for s in services]})
        finally:
            session.close()

    @app.get("/api/services")
    def list_services() -> Any:
        session = SessionLocal()
        try:
            services = session.query(Service).all()
            return jsonify({"services": [service_to_dict(s) for s in services]})
        finally:
            session.close()

    @app.post("/api/services")
    def create_service() -> Any:
        payload = request.get_json(force=True, silent=False)
        name = (payload.get("name") or "").strip()
        if not name:
            abort(400, description="name is required")

        heartbeat_urls = payload.get("heartbeat_urls") or []
        if not isinstance(heartbeat_urls, list):
            abort(400, description="heartbeat_urls must be a list")

        service = Service(
            name=name,
            parent_id=payload.get("parent_id"),
            enabled=bool(payload.get("enabled", True)),
        )
        for heartbeat_url in heartbeat_urls:
            if heartbeat_url:
                service.endpoints.append(ServiceEndpoint(heartbeat_url=str(heartbeat_url)))

        session = SessionLocal()
        try:
            session.add(service)
            session.commit()
            session.refresh(service)
            return jsonify(service_to_dict(service)), 201
        finally:
            session.close()

    @app.patch("/api/services/<int:service_id>")
    def update_service(service_id: int) -> Any:
        payload = request.get_json(force=True, silent=False)
        session = SessionLocal()
        try:
            service = session.get(Service, service_id)
            if service is None:
                abort(404, description="service not found")

            if "name" in payload:
                service.name = payload["name"].strip()
            if "parent_id" in payload:
                service.parent_id = payload["parent_id"]
            if "enabled" in payload:
                service.enabled = bool(payload["enabled"])
            if "heartbeat_urls" in payload:
                heartbeat_urls = payload["heartbeat_urls"]
                if not isinstance(heartbeat_urls, list):
                    abort(400, description="heartbeat_urls must be a list")
                service.endpoints.clear()
                for heartbeat_url in heartbeat_urls:
                    if heartbeat_url:
                        service.endpoints.append(ServiceEndpoint(heartbeat_url=str(heartbeat_url)))

            session.commit()
            session.refresh(service)
            return jsonify(service_to_dict(service))
        finally:
            session.close()

    @app.post("/api/check-now")
    def check_now() -> Any:
        run_health_checks()
        return jsonify({"status": "ok"})

    @app.get("/api/services/<int:service_id>/status/current")
    def current_status(service_id: int) -> Any:
        session = SessionLocal()
        try:
            service = session.get(Service, service_id)
            if service is None:
                abort(404, description="service not found")

            daily = (
                session.query(DailyCheck)
                .filter(DailyCheck.service_id == service_id)
                .order_by(DailyCheck.date.desc())
                .first()
            )
            latest_status = None
            latest_time = None
            if daily is not None:
                for minute_idx in reversed(range(24 * 60)):
                    value = getattr(daily, f"minute_{minute_idx:04d}")
                    if value is not None:
                        latest_status = value
                        minute_value = minute_idx
                        latest_time = dt.datetime.combine(
                            daily.date,
                            dt.time(hour=minute_value // 60, minute=minute_value % 60),
                        ).isoformat()
                        break

            return jsonify(
                {
                    "service": service_to_dict(service),
                    "status": "healthy" if latest_status == 1 else "unhealthy" if latest_status == 0 else "unknown",
                    "last_check_time": latest_time,
                }
            )
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/status/recent")
    def recent_status(service_id: int) -> Any:
        days = int(request.args.get("days", 7))
        if days < 1:
            abort(400, description="days must be >= 1")

        session = SessionLocal()
        try:
            service = session.get(Service, service_id)
            if service is None:
                abort(404, description="service not found")

            start_date = dt.date.today() - dt.timedelta(days=days - 1)
            rows = (
                session.query(DailyCheck)
                .filter(DailyCheck.service_id == service_id, DailyCheck.date >= start_date)
                .order_by(DailyCheck.date.asc())
                .all()
            )
            row_map = {row.date: row for row in rows}

            result = []
            for offset in range(days):
                current_date = start_date + dt.timedelta(days=offset)
                health = _daily_row_health(row_map.get(current_date))
                result.append(
                    {
                        "date": current_date.isoformat(),
                        "status": "healthy" if health == 1 else "unhealthy" if health == 0 else "unknown",
                    }
                )

            return jsonify({"service": service_to_dict(service), "days": result})
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/children/status/recent")
    def children_recent_status(service_id: int) -> Any:
        days = int(request.args.get("days", 7))
        if days < 1:
            abort(400, description="days must be >= 1")

        session = SessionLocal()
        try:
            parent = session.get(Service, service_id)
            if parent is None:
                abort(404, description="service not found")

            start_date = dt.date.today() - dt.timedelta(days=days - 1)
            output = []
            for child in parent.children:
                rows = (
                    session.query(DailyCheck)
                    .filter(DailyCheck.service_id == child.id, DailyCheck.date >= start_date)
                    .order_by(DailyCheck.date.asc())
                    .all()
                )
                row_map = {row.date: row for row in rows}
                child_days = []
                for offset in range(days):
                    current_date = start_date + dt.timedelta(days=offset)
                    health = _daily_row_health(row_map.get(current_date))
                    child_days.append(
                        {
                            "date": current_date.isoformat(),
                            "status": "healthy" if health == 1 else "unhealthy" if health == 0 else "unknown",
                        }
                    )
                output.append({"service": service_to_dict(child), "days": child_days})

            return jsonify({"parent_service": service_to_dict(parent), "children": output})
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/failures")
    def failures_for_day(service_id: int) -> Any:
        date_text = request.args.get("date")
        if not date_text:
            abort(400, description="date is required (YYYY-MM-DD)")

        try:
            check_date = dt.date.fromisoformat(date_text)
        except ValueError:
            abort(400, description="invalid date format")

        start_dt = dt.datetime.combine(check_date, dt.time.min)
        end_dt = dt.datetime.combine(check_date + dt.timedelta(days=1), dt.time.min)

        session = SessionLocal()
        try:
            records = (
                session.query(FailureRecord)
                .filter(
                    FailureRecord.service_id == service_id,
                    FailureRecord.check_time >= start_dt,
                    FailureRecord.check_time < end_dt,
                )
                .order_by(FailureRecord.check_time.asc())
                .all()
            )
            return jsonify(
                {
                    "service_id": service_id,
                    "date": check_date.isoformat(),
                    "failures": [
                        {
                            "id": row.id,
                            "check_time": row.check_time.isoformat(),
                            "status": row.status,
                            "log": row.log,
                        }
                        for row in records
                    ],
                }
            )
        finally:
            session.close()

    @app.get("/services/<int:service_id>/failure-calendar")
    def failure_calendar(service_id: int) -> Any:
        year = int(request.args.get("year", dt.date.today().year))

        session = SessionLocal()
        try:
            service = session.get(Service, service_id)
            if service is None:
                abort(404, description="service not found")

            yearly = (
                session.query(YearlyCheck)
                .filter(YearlyCheck.service_id == service_id, YearlyCheck.year == year)
                .one_or_none()
            )
            unhealthy_days = set()
            if yearly is not None:
                for month in range(1, 13):
                    _, month_days = calendar.monthrange(year, month)
                    if month == 2:
                        month_days = 29
                    for day in range(1, month_days + 1):
                        value = getattr(yearly, f"day_{month:02d}{day:02d}")
                        if value == 0:
                            unhealthy_days.add(dt.date(year, month, day).isoformat())

            month_weeks = {month: calendar.monthcalendar(year, month) for month in range(1, 13)}

            return render_template(
                "failure_calendar.html",
                service=service,
                year=year,
                unhealthy_days=unhealthy_days,
                month_weeks=month_weeks,
            )
        finally:
            session.close()

    return app


def start_scheduler() -> None:
    schedule.every(1).minutes.do(run_health_checks)

    def _loop() -> None:
        while True:
            schedule.run_pending()
            time.sleep(1)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()


Base.metadata.create_all(bind=engine)
app = create_app()


if __name__ == "__main__":
    if os.getenv("START_SCHEDULER", "1") == "1":
        start_scheduler()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
