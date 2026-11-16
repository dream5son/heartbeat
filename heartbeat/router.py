import datetime as dt

from flask import Flask, abort, jsonify, render_template, request
from sqlalchemy import text

from .logging_config import logger
from .model import (
    DEFAULT_ALARM_MAX_PER_WINDOW,
    DEFAULT_ALARM_WINDOW_MINUTES,
    DEFAULT_SUCCESS_RULE,
    SessionLocal,
    normalize_alarm_channels,
    normalize_alarm_int,
    normalize_success_rule,
    parse_alarm_recipients,
)
from .service import (
    ServiceHasChildrenError,
    children_recent_days,
    create_service,
    delete_service,
    failure_records_for_day,
    list_alarm_records,
    list_services,
    run_health_checks,
    service_current_status,
    service_daily_detail,
    service_failure_records,
    service_recent_days,
    service_recent_hours,
    service_status_calendar_data,
    service_to_dict,
    status_tree,
    update_service,
)


def _parse_alarm_settings(payload: dict[str, object]) -> dict[str, object]:
    alarm_enabled = bool(payload.get("alarm_enabled", False))
    channels_value = payload.get("alarm_channels")
    if alarm_enabled and not channels_value:
        channels_value = "email"
    channels = normalize_alarm_channels(channels_value)
    recipients = parse_alarm_recipients(payload.get("alarm_recipients"))
    if alarm_enabled and "email" in channels.split(",") and not recipients:
        abort(400, description="alarm_recipients is required when alarm is enabled")
    return {
        "alarm_enabled": alarm_enabled,
        "alarm_channels": channels,
        "alarm_recipients": ",".join(recipients),
        "alarm_window_minutes": normalize_alarm_int(
            payload.get("alarm_window_minutes"), DEFAULT_ALARM_WINDOW_MINUTES
        ),
        "alarm_max_per_window": normalize_alarm_int(
            payload.get("alarm_max_per_window"), DEFAULT_ALARM_MAX_PER_WINDOW
        ),
    }


def parse_heartbeat_configs(payload: dict[str, object]) -> list[dict[str, object]]:
    if "heartbeat_configs" in payload:
        configs = payload["heartbeat_configs"]
        if not isinstance(configs, list):
            abort(400, description="heartbeat_configs must be a list")
        normalized_configs: list[dict[str, object]] = []
        for item in configs:
            if not isinstance(item, dict):
                abort(400, description="heartbeat_configs entries must be objects")
            heartbeat_url = str(item.get("heartbeat_url") or "").strip()
            if not heartbeat_url:
                continue
            config: dict[str, object] = {
                "heartbeat_url": heartbeat_url,
                "success_rule": normalize_success_rule(item.get("success_rule")),
            }
            config.update(_parse_alarm_settings(item))
            normalized_configs.append(config)
        return normalized_configs

    heartbeat_urls = payload.get("heartbeat_urls") or []
    if not isinstance(heartbeat_urls, list):
        abort(400, description="heartbeat_urls must be a list")
    return [
        {
            "heartbeat_url": str(url),
            "success_rule": DEFAULT_SUCCESS_RULE,
            "alarm_enabled": False,
            "alarm_channels": "",
            "alarm_recipients": "",
            "alarm_window_minutes": DEFAULT_ALARM_WINDOW_MINUTES,
            "alarm_max_per_window": DEFAULT_ALARM_MAX_PER_WINDOW,
        }
        for url in heartbeat_urls
        if url
    ]


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

    days = request.args.get("days", default_days, type=int)
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

    @app.get("/healthz")
    def healthz() -> object:
        session = SessionLocal()
        try:
            session.execute(text("SELECT 1"))
        except Exception:
            logger.exception("health check failed: database unavailable")
            return jsonify({"status": "error"}), 503
        finally:
            session.close()
        return jsonify({"status": "ok"})

    @app.get("/api/services")
    def list_services_api() -> object:
        session = SessionLocal()
        try:
            return jsonify({"services": list_services(session)})
        finally:
            session.close()

    @app.post("/api/services")
    def create_service_api() -> object:
        payload = request.get_json(force=True, silent=False)
        name = (payload.get("name") or "").strip()
        if not name:
            abort(400, description="name is required")

        heartbeat_configs = parse_heartbeat_configs(payload)

        session = SessionLocal()
        try:
            service = create_service(
                session,
                name=name,
                parent_id=payload.get("parent_id"),
                enabled=bool(payload.get("enabled", True)),
                heartbeat_configs=heartbeat_configs,
            )
            return jsonify(service_to_dict(service)), 201
        finally:
            session.close()

    @app.patch("/api/services/<int:service_id>")
    def update_service_api(service_id: int) -> object:
        payload = request.get_json(force=True, silent=False)
        heartbeat_configs = None
        if "heartbeat_urls" in payload or "heartbeat_configs" in payload:
            heartbeat_configs = parse_heartbeat_configs(payload)

        session = SessionLocal()
        try:
            service = update_service(
                session,
                service_id,
                payload,
                heartbeat_configs=heartbeat_configs,
            )
            if service is None:
                abort(404, description="service not found")
            return jsonify(service_to_dict(service))
        finally:
            session.close()

    @app.delete("/api/services/<int:service_id>")
    def delete_service_api(service_id: int) -> object:
        session = SessionLocal()
        try:
            try:
                deleted = delete_service(session, service_id)
            except ServiceHasChildrenError:
                abort(409, description="service has child services; delete child services first")
            if not deleted:
                abort(404, description="service not found")
            return jsonify({"status": "ok"})
        finally:
            session.close()

    @app.post("/api/check-now")
    def check_now() -> object:
        logger.info("manual health check requested")
        run_health_checks()
        return jsonify({"status": "ok"})

    @app.get("/alarm-records")
    def alarm_records_page() -> object:
        return render_template("alarm_records.html")

    @app.get("/api/alarm-records")
    def list_alarm_records_api() -> object:
        start_date, end_date, _ = _resolve_date_window(default_days=7, max_days=180)

        send_status = (request.args.get("send_status") or "").strip()
        if send_status and send_status not in {"sent", "failed"}:
            abort(400, description="send_status must be sent or failed")

        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)
        if limit < 1 or limit > 500:
            abort(400, description="limit must be between 1 and 500")
        if offset < 0:
            abort(400, description="offset must be >= 0")

        service_id = request.args.get("service_id", type=int)

        start_dt = dt.datetime.combine(start_date, dt.time.min)
        end_dt = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min)

        session = SessionLocal()
        try:
            return jsonify(
                list_alarm_records(
                    session,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    service_id=service_id,
                    send_status=send_status,
                    limit=limit,
                    offset=offset,
                )
            )
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/status/current")
    def current_status(service_id: int) -> object:
        session = SessionLocal()
        try:
            result = service_current_status(session, service_id)
            if result is None:
                abort(404, description="service not found")
            return jsonify(result)
        finally:
            session.close()

    @app.get("/api/status-tree")
    def status_tree_api() -> object:
        session = SessionLocal()
        try:
            return jsonify(
                {
                    "generated_at": dt.datetime.now().isoformat(),
                    "roots": status_tree(session),
                }
            )
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/status/recent")
    def recent_status(service_id: int) -> object:
        days = request.args.get("days", 7, type=int)
        if days < 1:
            abort(400, description="days must be >= 1")

        session = SessionLocal()
        try:
            result = service_recent_days(session, service_id, days)
            if result is None:
                abort(404, description="service not found")
            return jsonify(result)
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/status/recent-daily-detail")
    def recent_daily_detail(service_id: int) -> object:
        start_date, end_date, _ = _resolve_date_window(default_days=90, max_days=180)

        session = SessionLocal()
        try:
            result = service_daily_detail(session, service_id, start_date, end_date)
            if result is None:
                abort(404, description="service not found")
            return jsonify(result)
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/status/recent-hours")
    def recent_status_hours(service_id: int) -> object:
        hours = request.args.get("hours", 12, type=int)
        if hours < 1 or hours > 24:
            abort(400, description="hours must be between 1 and 24")

        session = SessionLocal()
        try:
            result = service_recent_hours(session, service_id, hours)
            if result is None:
                abort(404, description="service not found")
            return jsonify(result)
        finally:
            session.close()

    @app.get("/api/services/<int:service_id>/children/status/recent")
    def children_recent_status(service_id: int) -> object:
        days = request.args.get("days", 7, type=int)
        if days < 1:
            abort(400, description="days must be >= 1")

        session = SessionLocal()
        try:
            result = children_recent_days(session, service_id, days)
            if result is None:
                abort(404, description="service not found")
            return jsonify(result)
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

        session = SessionLocal()
        try:
            return jsonify(
                {
                    "service_id": service_id,
                    "date": check_date.isoformat(),
                    "failures": failure_records_for_day(session, service_id, check_date),
                }
            )
        finally:
            session.close()

    @app.get("/services/<int:service_id>/failures")
    def failure_logs_page(service_id: int) -> object:
        date_text = request.args.get("date")
        if not date_text:
            abort(400, description="date is required (YYYY-MM-DD)")

        try:
            check_date = dt.date.fromisoformat(date_text)
        except ValueError:
            abort(400, description="invalid date format")

        session = SessionLocal()
        try:
            data = service_failure_records(session, service_id, check_date)
            if data is None:
                abort(404, description="service not found")
            return render_template("failure_logs.html", **data)
        finally:
            session.close()

    @app.get("/services/<int:service_id>/service-status-calendar")
    def service_status_calendar(service_id: int) -> object:
        year = request.args.get("year", dt.date.today().year, type=int)
        month = request.args.get("month", dt.date.today().month, type=int)
        if not 1 <= month <= 12:
            abort(400, description="month must be between 1 and 12")

        session = SessionLocal()
        try:
            data = service_status_calendar_data(session, service_id, year, month)
            if data is None:
                abort(404, description="service not found")
            return render_template("service_status_calendar.html", **data)
        finally:
            session.close()

    return app
