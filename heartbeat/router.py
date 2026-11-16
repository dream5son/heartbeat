import calendar
import datetime as dt

from flask import Flask, abort, jsonify, render_template, request

from .model import (
    DEFAULT_SUCCESS_RULE,
    DailyCheck,
    FailureRecord,
    Service,
    ServiceEndpoint,
    SessionLocal,
    YearlyCheck,
    normalize_success_rule,
)
from .service import (
    _combine_service_state,
    _daily_row_detail,
    _daily_row_health,
    _latest_failure_hint,
    _service_status_history,
    _service_status_node,
    run_health_checks,
    service_to_dict,
)


def parse_heartbeat_configs(payload: dict[str, object]) -> list[tuple[str, str]]:
    if "heartbeat_configs" in payload:
        configs = payload["heartbeat_configs"]
        if not isinstance(configs, list):
            abort(400, description="heartbeat_configs must be a list")
        normalized_configs: list[tuple[str, str]] = []
        for item in configs:
            if not isinstance(item, dict):
                abort(400, description="heartbeat_configs entries must be objects")
            heartbeat_url = str(item.get("heartbeat_url") or "").strip()
            if not heartbeat_url:
                continue
            success_rule = normalize_success_rule(item.get("success_rule"))
            normalized_configs.append((heartbeat_url, success_rule))
        return normalized_configs

    heartbeat_urls = payload.get("heartbeat_urls") or []
    if not isinstance(heartbeat_urls, list):
        abort(400, description="heartbeat_urls must be a list")
    return [(str(url), DEFAULT_SUCCESS_RULE) for url in heartbeat_urls if url]


def _resolve_date_window(default_days: int, max_days: int) -> tuple[dt.date, dt.date, int]:
    start_date_text = request.args.get("start_date")
    end_date_text = request.args.get("end_date")

    if start_date_text or end_date_text:
        if not start_date_text or not end_date_text:
            abort(400, description="start_date and end_date must both be provided")
        try:
            start_date = dt.date.fromisoformat(start_date_text)
            end_date = dt.date.fromisoformat(end_date_text)
        except ValueError:
            abort(400, description="start_date and end_date must be valid yyyy-mm-dd")
        if start_date > end_date:
            abort(400, description="start_date must be <= end_date")
        days = (end_date - start_date).days + 1
        if days > max_days:
            abort(400, description=f"date range must be <= {max_days} days")
        return start_date, end_date, days

    try:
        days = int(request.args.get("days", default_days))
    except (TypeError, ValueError):
        abort(400, description="days must be an integer")
    if days < 1 or days > max_days:
        abort(400, description=f"days must be between 1 and {max_days}")
    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=days - 1)
    return start_date, end_date, days


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates")

    @app.teardown_appcontext
    def shutdown_session(_: object) -> None:
        SessionLocal.remove()

    @app.get("/")
    def index() -> object:
        return render_template("status_home.html")

    @app.get("/config")
    def service_config() -> object:
        return render_template("service_config.html")

    @app.get("/api/services")
    def list_services() -> object:
        session = SessionLocal()
        try:
            services = session.query(Service).all()
            return jsonify({"services": [service_to_dict(s) for s in services]})
        finally:
            session.close()

    @app.post("/api/services")
    def create_service() -> object:
        payload = request.get_json(force=True, silent=False)
        name = (payload.get("name") or "").strip()
        if not name:
            abort(400, description="name is required")

        heartbeat_configs = parse_heartbeat_configs(payload)

        service = Service(
            name=name,
            parent_id=payload.get("parent_id"),
            enabled=bool(payload.get("enabled", True)),
        )
        for heartbeat_url, success_rule in heartbeat_configs:
            service.endpoints.append(
                ServiceEndpoint(
                    heartbeat_url=heartbeat_url,
                    success_rule=success_rule,
                )
            )

        session = SessionLocal()
        try:
            session.add(service)
            session.commit()
            session.refresh(service)
            return jsonify(service_to_dict(service)), 201
        finally:
            session.close()

    @app.patch("/api/services/<int:service_id>")
    def update_service(service_id: int) -> object:
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
            if "heartbeat_urls" in payload or "heartbeat_configs" in payload:
                heartbeat_configs = parse_heartbeat_configs(payload)
                service.endpoints.clear()
                for heartbeat_url, success_rule in heartbeat_configs:
                    service.endpoints.append(
                        ServiceEndpoint(
                            heartbeat_url=heartbeat_url,
                            success_rule=success_rule,
                        )
                    )

            session.commit()
            session.refresh(service)
            return jsonify(service_to_dict(service))
        finally:
            session.close()

    @app.post("/api/check-now")
    def check_now() -> object:
        run_health_checks()
        return jsonify({"status": "ok"})

    @app.get("/api/services/<int:service_id>/status/current")
    def current_status(service_id: int) -> object:
        session = SessionLocal()
        try:
            service = session.get(Service, service_id)
            if service is None:
                abort(404, description="service not found")

            latest_status, latest_time = _latest_status_snapshot(session, service_id)

            return jsonify(
                {
                    "service": service_to_dict(service),
                    "status": _status_text(latest_status),
                    "last_check_time": latest_time,
                }
            )
        finally:
            session.close()

    @app.get("/api/status-tree")
    def status_tree() -> object:
        session = SessionLocal()
        try:
            roots = (
                session.query(Service)
                .filter(Service.parent_id.is_(None))
                .order_by(Service.name.asc())
                .all()
            )
            output = []
            for root in roots:
                child_nodes = [_service_status_node(session, child) for child in root.children]
                root_node = _service_status_node(session, root)

                combined_statuses = [root_node["status"], *[node["status"] for node in child_nodes]]
                root_node["status"] = _combine_service_state(combined_statuses)
                root_node["children"] = child_nodes
                scoped_ids = [root.id, *[child.id for child in root.children]]
                root_node["failure_hint"] = _latest_failure_hint(session, scoped_ids)
                output.append(root_node)

            return jsonify(
                {
                    "generated_at": dt.datetime.now().isoformat(),
                    "roots": output,
                }
            )
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/status/recent")
    def recent_status(service_id: int) -> object:
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

    @app.get("/api/services/<int:service_id>/status/recent-daily-detail")
    def recent_daily_detail(service_id: int) -> object:
        start_date, end_date, days = _resolve_date_window(default_days=90, max_days=180)

        session = SessionLocal()
        try:
            service = session.get(Service, service_id)
            if service is None:
                abort(404, description="service not found")

            rows = (
                session.query(DailyCheck)
                .filter(
                    DailyCheck.service_id == service_id,
                    DailyCheck.date >= start_date,
                    DailyCheck.date <= end_date,
                )
                .order_by(DailyCheck.date.asc())
                .all()
            )
            row_map = {row.date: row for row in rows}

            result = []
            for offset in range(days):
                current_date = start_date + dt.timedelta(days=offset)
                detail = _daily_row_detail(row_map.get(current_date))
                result.append({"date": current_date.isoformat(), **detail})

            return jsonify({"service": service_to_dict(service), "days": result})
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/status/recent-hours")
    def recent_status_hours(service_id: int) -> object:
        hours = int(request.args.get("hours", 12))
        if hours < 1 or hours > 24:
            abort(400, description="hours must be between 1 and 24")

        session = SessionLocal()
        try:
            service = session.get(Service, service_id)
            if service is None:
                abort(404, description="service not found")

            return jsonify(
                {
                    "service": service_to_dict(service),
                    "hours": hours,
                    "status": _service_status_history(session, service_id, hours=hours),
                }
            )
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/children/status/recent")
    def children_recent_status(service_id: int) -> object:
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
    def failures_for_day(service_id: int) -> object:
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
    def failure_calendar(service_id: int) -> object:
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


def _latest_status_snapshot(session, service_id: int):
    rows = (
        session.query(DailyCheck)
        .filter(DailyCheck.service_id == service_id)
        .order_by(DailyCheck.date.desc())
        .all()
    )
    for daily in rows:
        for minute_idx in reversed(range(24 * 60)):
            value = getattr(daily, f"minute_{minute_idx:04d}")
            if value is None:
                continue
            check_time = dt.datetime.combine(
                daily.date,
                dt.time(hour=minute_idx // 60, minute=minute_idx % 60),
            ).isoformat()
            return value, check_time
    return None, None


def _status_text(status_value: int | None) -> str:
    if status_value == 1:
        return "healthy"
    if status_value == 0:
        return "unhealthy"
    return "unknown"


def _service_status_node(session, service):
    latest_status, latest_time = _latest_status_snapshot(session, service.id)
    return {
        "service": service_to_dict(service),
        "status": _status_text(latest_status),
        "last_check_time": latest_time,
        "failure_hint": _latest_failure_hint(session, [service.id]),
    }
