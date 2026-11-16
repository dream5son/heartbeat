import calendar
import datetime as dt
import time
import urllib.error
import urllib.request
from typing import Any

from .alarm import maybe_send_alarm, send_email_alarm
from .logging_config import logger
from .model import (
    DEFAULT_SUCCESS_RULE,
    AlarmRecord,
    DailyCheck,
    FailureRecord,
    Service,
    ServiceEndpoint,
    SessionLocal,
    YearlyCheck,
    day_column,
    minute_column,
    normalize_alarm_channels,
    normalize_success_rule,
    parse_alarm_recipients,
)


class ServiceHasChildrenError(Exception):
    """Raised when deleting a service that still has child services."""


def endpoint_check_passed(
    success_rule: str,
    request_ok: bool,
    status_code: int,
    response_body: str,
) -> bool:
    rule = normalize_success_rule(success_rule)
    if not request_ok:
        return False
    if rule.startswith("status=="):
        expected_text = rule.split("==", 1)[1].strip()
        try:
            expected_status = int(expected_text)
        except ValueError:
            return False
        return status_code == expected_status
    if rule.startswith("body_contains:"):
        expected_text = rule.split(":", 1)[1]
        return expected_text in response_body
    return False


def check_url(heartbeat_url: str, timeout: int = 8) -> tuple[bool, int, str, str]:
    try:
        with urllib.request.urlopen(heartbeat_url, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read(64 * 1024).decode("utf-8", errors="replace")
            return True, int(status), f"status={status}", body
    except urllib.error.HTTPError as err:
        body = err.read(64 * 1024).decode("utf-8", errors="replace")
        return False, int(err.code), str(err), body
    except Exception as err:  # noqa: BLE001
        return False, -1, str(err), ""


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
    notifier=send_email_alarm,
) -> bool:
    minute_col = minute_column(check_time)
    day_col = day_column(check_time.date())

    endpoint_results: list[tuple[bool, int, str, str, str]] = []
    failed_endpoints: list[tuple[ServiceEndpoint, int, str]] = []
    for endpoint in service.endpoints:
        result = checker(endpoint.heartbeat_url)
        if len(result) == 3:
            ok, status_code, detail = result
            endpoint_ok = bool(ok)
            endpoint_results.append(
                (
                    endpoint_ok,
                    int(status_code),
                    str(detail),
                    endpoint.heartbeat_url,
                    normalize_success_rule(endpoint.success_rule),
                )
            )
            if not endpoint_ok:
                failed_endpoints.append((endpoint, int(status_code), str(detail)))
            continue

        request_ok, status_code, detail, response_body = result
        success_rule = normalize_success_rule(endpoint.success_rule)
        endpoint_ok = endpoint_check_passed(
            success_rule=success_rule,
            request_ok=bool(request_ok),
            status_code=int(status_code),
            response_body=str(response_body),
        )
        endpoint_results.append(
            (
                endpoint_ok,
                int(status_code),
                str(detail),
                endpoint.heartbeat_url,
                success_rule,
            )
        )
        if not endpoint_ok:
            failed_endpoints.append((endpoint, int(status_code), str(detail)))

    if endpoint_results:
        service_ok = all(item[0] for item in endpoint_results)
    else:
        service_ok = False
        endpoint_results.append((False, -1, "no heartbeat urls configured", "", DEFAULT_SUCCESS_RULE))

    daily = _get_or_create_daily(session, service.id, check_time.date())
    setattr(daily, minute_col, 1 if service_ok else 0)

    yearly = _get_or_create_yearly(session, service.id, check_time.year)
    previous_day = getattr(yearly, day_col)
    if service_ok and previous_day is None:
        setattr(yearly, day_col, 1)
    if not service_ok:
        setattr(yearly, day_col, 0)

    if not service_ok:
        for ok, status_code, detail, heartbeat_url, success_rule in endpoint_results:
            if ok:
                continue
            logger.warning(
                "health check failed: service={} url={} rule={} status={} detail={}",
                service.name,
                heartbeat_url or "-",
                success_rule,
                status_code,
                detail,
            )
            session.add(
                FailureRecord(
                    service_id=service.id,
                    check_time=check_time,
                    status=status_code,
                    log=f"url={heartbeat_url or '-'} rule={success_rule} detail={detail}",
                )
            )

        for endpoint, status_code, detail in failed_endpoints:
            maybe_send_alarm(
                session,
                service,
                endpoint,
                check_time,
                status_code,
                detail,
                endpoint_results,
                notifier=notifier,
            )

    return service_ok


def run_health_checks(check_time: dt.datetime | None = None) -> None:
    check_time = check_time or dt.datetime.now().replace(second=0, microsecond=0)
    started = time.monotonic()
    logger.info("health check run started: check_time={}", check_time.isoformat(timespec="minutes"))
    session = SessionLocal()
    try:
        services = session.query(Service).filter(Service.enabled.is_(True)).all()
        healthy = 0
        unhealthy = 0
        for service in services:
            if perform_service_check(session, service, check_time):
                healthy += 1
            else:
                unhealthy += 1
        session.commit()
        logger.info(
            "health check run finished: total={} healthy={} unhealthy={} elapsed_ms={}",
            len(services),
            healthy,
            unhealthy,
            int((time.monotonic() - started) * 1000),
        )
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception(
            "health check run failed: check_time={}",
            check_time.isoformat(timespec="minutes"),
        )
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


def _daily_row_detail(daily: DailyCheck | None) -> dict[str, Any]:
    if daily is None:
        return {
            "status": "unknown",
            "healthy_count": 0,
            "unhealthy_count": 0,
            "unknown_count": 24 * 60,
            "failure_times": [],
        }

    healthy_count = 0
    unhealthy_count = 0
    unknown_count = 0
    failure_times: list[str] = []
    for minute_idx in range(24 * 60):
        value = getattr(daily, f"minute_{minute_idx:04d}")
        if value == 1:
            healthy_count += 1
        elif value == 0:
            unhealthy_count += 1
            failure_times.append(f"{minute_idx // 60:02d}:{minute_idx % 60:02d}")
        else:
            unknown_count += 1

    status_value = _daily_row_health(daily)
    return {
        "status": _status_text(status_value),
        "healthy_count": healthy_count,
        "unhealthy_count": unhealthy_count,
        "unknown_count": unknown_count,
        "failure_times": failure_times,
    }


def _status_text(status_value: int | None) -> str:
    if status_value == 1:
        return "healthy"
    if status_value == 0:
        return "unhealthy"
    return "unknown"


def _combine_service_state(states: list[str]) -> str:
    if not states:
        return "unknown"
    if all(state == "healthy" for state in states):
        return "healthy"
    if all(state == "unknown" for state in states):
        return "unknown"
    if any(state == "unhealthy" for state in states) and any(state == "healthy" for state in states):
        return "partial"
    if any(state == "unhealthy" for state in states):
        return "unhealthy"
    return "unknown"


def _service_status_history(
    session: Any,
    service_id: int,
    hours: int = 12,
    check_time: dt.datetime | None = None,
) -> list[str]:
    if hours < 1:
        return []

    window_minutes = hours * 60
    now = (check_time or dt.datetime.now()).replace(second=0, microsecond=0)
    start_time = now - dt.timedelta(minutes=window_minutes - 1)
    start_date = (start_time - dt.timedelta(days=1)).date()
    end_date = now.date()

    rows = (
        session.query(DailyCheck)
        .filter(
            DailyCheck.service_id == service_id,
            DailyCheck.date >= start_date,
            DailyCheck.date <= end_date,
        )
        .all()
    )
    row_map = {row.date: row for row in rows}

    history: list[str] = []
    for minute_offset in range(window_minutes):
        check_time = start_time + dt.timedelta(minutes=minute_offset)
        value = None
        daily = row_map.get(check_time.date())
        if daily is not None:
            value = getattr(daily, f"minute_{(check_time.hour * 60 + check_time.minute):04d}", None)
        if value == 1:
            history.append("healthy")
        elif value == 0:
            history.append("unhealthy")
        else:
            history.append("unknown")
    return history


def _latest_status_snapshot(session: Any, service_id: int) -> tuple[int | None, str | None]:
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


def _latest_failure_hint(session: Any, service_ids: list[int]) -> dict[str, Any] | None:
    if not service_ids:
        return None
    row = (
        session.query(FailureRecord)
        .filter(FailureRecord.service_id.in_(service_ids))
        .order_by(FailureRecord.check_time.desc())
        .first()
    )
    if row is None:
        return None
    return {
        "service_id": row.service_id,
        "check_time": row.check_time.isoformat(),
        "status": row.status,
        "summary": row.log.strip().splitlines()[0][:180],
    }


def _service_status_node(session: Any, service: Service) -> dict[str, Any]:
    latest_status, latest_time = _latest_status_snapshot(session, service.id)
    return {
        "service": service_to_dict(service),
        "status": _status_text(latest_status),
        "last_check_time": latest_time,
        "failure_hint": _latest_failure_hint(session, [service.id]),
    }


def service_to_dict(service: Service) -> dict[str, Any]:
    return {
        "id": service.id,
        "name": service.name,
        "parent_id": service.parent_id,
        "enabled": service.enabled,
        "heartbeat_configs": [
            {
                "id": endpoint.id,
                "heartbeat_url": endpoint.heartbeat_url,
                "success_rule": normalize_success_rule(endpoint.success_rule),
                "alarm_enabled": bool(endpoint.alarm_enabled),
                "alarm_channels": normalize_alarm_channels(endpoint.alarm_channels),
                "alarm_recipients": ",".join(parse_alarm_recipients(endpoint.alarm_recipients)),
                "alarm_window_minutes": endpoint.alarm_window_minutes,
                "alarm_max_per_window": endpoint.alarm_max_per_window,
            }
            for endpoint in service.endpoints
        ],
        "heartbeat_urls": [e.heartbeat_url for e in service.endpoints],
        "child_service_ids": [child.id for child in service.children],
    }


def list_services(session: Any) -> list[dict[str, Any]]:
    return [service_to_dict(service) for service in session.query(Service).all()]


def create_service(
    session: Any,
    *,
    name: str,
    parent_id: int | None,
    enabled: bool,
    heartbeat_configs: list[dict[str, Any]],
) -> Service:
    service = Service(name=name, parent_id=parent_id, enabled=enabled)
    for config in heartbeat_configs:
        service.endpoints.append(ServiceEndpoint(**config))
    session.add(service)
    session.commit()
    session.refresh(service)
    logger.info(
        "service created: id={} name={} parent_id={} endpoints={}",
        service.id,
        service.name,
        service.parent_id,
        len(service.endpoints),
    )
    return service


def update_service(
    session: Any,
    service_id: int,
    payload: dict[str, Any],
    heartbeat_configs: list[dict[str, Any]] | None = None,
) -> Service | None:
    service = session.get(Service, service_id)
    if service is None:
        return None

    if "name" in payload:
        service.name = payload["name"].strip()
    if "parent_id" in payload:
        service.parent_id = payload["parent_id"]
    if "enabled" in payload:
        service.enabled = bool(payload["enabled"])
    if heartbeat_configs is not None:
        service.endpoints.clear()
        for config in heartbeat_configs:
            service.endpoints.append(ServiceEndpoint(**config))

    session.commit()
    session.refresh(service)
    logger.info("service updated: id={} fields={}", service.id, sorted(payload.keys()))
    return service


def delete_service(session: Any, service_id: int) -> bool:
    service = session.get(Service, service_id)
    if service is None:
        return False
    if service.children:
        raise ServiceHasChildrenError(
            f"service {service_id} has child services; delete child services first"
        )

    session.query(DailyCheck).filter(DailyCheck.service_id == service_id).delete(
        synchronize_session=False
    )
    session.query(YearlyCheck).filter(YearlyCheck.service_id == service_id).delete(
        synchronize_session=False
    )
    session.query(FailureRecord).filter(FailureRecord.service_id == service_id).delete(
        synchronize_session=False
    )
    session.query(AlarmRecord).filter(AlarmRecord.service_id == service_id).delete(
        synchronize_session=False
    )

    name = service.name
    session.delete(service)
    session.commit()
    logger.info("service deleted: id={} name={}", service_id, name)
    return True


def service_current_status(session: Any, service_id: int) -> dict[str, Any] | None:
    service = session.get(Service, service_id)
    if service is None:
        return None

    latest_status, latest_time = _latest_status_snapshot(session, service_id)
    return {
        "service": service_to_dict(service),
        "status": _status_text(latest_status),
        "last_check_time": latest_time,
    }


def status_tree(session: Any) -> list[dict[str, Any]]:
    roots = (
        session.query(Service)
        .filter(Service.parent_id.is_(None))
        .order_by(Service.name.asc())
        .all()
    )
    output: list[dict[str, Any]] = []
    for root in roots:
        child_nodes = [_service_status_node(session, child) for child in root.children]
        root_node = _service_status_node(session, root)

        combined_statuses = [root_node["status"], *[node["status"] for node in child_nodes]]
        root_node["status"] = _combine_service_state(combined_statuses)
        root_node["children"] = child_nodes
        scoped_ids = [root.id, *[child.id for child in root.children]]
        root_node["failure_hint"] = _latest_failure_hint(session, scoped_ids)
        output.append(root_node)
    return output


def _daily_status_series(
    session: Any,
    service_id: int,
    start_date: dt.date,
    days: int,
) -> list[dict[str, Any]]:
    end_date = start_date + dt.timedelta(days=days - 1)
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

    series: list[dict[str, Any]] = []
    for offset in range(days):
        current_date = start_date + dt.timedelta(days=offset)
        series.append(
            {
                "date": current_date.isoformat(),
                "status": _status_text(_daily_row_health(row_map.get(current_date))),
            }
        )
    return series


def service_recent_days(session: Any, service_id: int, days: int) -> dict[str, Any] | None:
    service = session.get(Service, service_id)
    if service is None:
        return None

    start_date = dt.date.today() - dt.timedelta(days=days - 1)
    return {
        "service": service_to_dict(service),
        "days": _daily_status_series(session, service_id, start_date, days),
    }


def service_daily_detail(
    session: Any,
    service_id: int,
    start_date: dt.date,
    end_date: dt.date,
) -> dict[str, Any] | None:
    service = session.get(Service, service_id)
    if service is None:
        return None

    days = (end_date - start_date).days + 1
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

    result: list[dict[str, Any]] = []
    for offset in range(days):
        current_date = start_date + dt.timedelta(days=offset)
        detail = _daily_row_detail(row_map.get(current_date))
        result.append({"date": current_date.isoformat(), **detail})

    return {"service": service_to_dict(service), "days": result}


def service_recent_hours(session: Any, service_id: int, hours: int) -> dict[str, Any] | None:
    service = session.get(Service, service_id)
    if service is None:
        return None

    return {
        "service": service_to_dict(service),
        "hours": hours,
        "status": _service_status_history(session, service_id, hours=hours),
    }


def children_recent_days(session: Any, service_id: int, days: int) -> dict[str, Any] | None:
    parent = session.get(Service, service_id)
    if parent is None:
        return None

    start_date = dt.date.today() - dt.timedelta(days=days - 1)
    children = [
        {
            "service": service_to_dict(child),
            "days": _daily_status_series(session, child.id, start_date, days),
        }
        for child in parent.children
    ]
    return {"parent_service": service_to_dict(parent), "children": children}


def failure_records_for_day(
    session: Any,
    service_id: int,
    check_date: dt.date,
) -> list[dict[str, Any]]:
    start_dt = dt.datetime.combine(check_date, dt.time.min)
    end_dt = dt.datetime.combine(check_date + dt.timedelta(days=1), dt.time.min)
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
    return [
        {
            "id": row.id,
            "check_time": row.check_time.isoformat(),
            "status": row.status,
            "log": row.log,
        }
        for row in records
    ]


def service_failure_records(
    session: Any,
    service_id: int,
    check_date: dt.date,
) -> dict[str, Any] | None:
    service = session.get(Service, service_id)
    if service is None:
        return None

    return {
        "service": service,
        "date": check_date.isoformat(),
        "failures": failure_records_for_day(session, service_id, check_date),
    }


def service_status_calendar_data(
    session: Any,
    service_id: int,
    year: int,
    month: int,
) -> dict[str, Any] | None:
    service = session.get(Service, service_id)
    if service is None:
        return None

    yearly = (
        session.query(YearlyCheck)
        .filter(YearlyCheck.service_id == service_id, YearlyCheck.year == year)
        .one_or_none()
    )
    unhealthy_days: set[str] = set()
    if yearly is not None:
        month_days = calendar.monthrange(year, month)[1]
        for day in range(1, month_days + 1):
            value = getattr(yearly, f"day_{month:02d}{day:02d}")
            if value == 0:
                unhealthy_days.add(dt.date(year, month, day).isoformat())

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return {
        "service": service,
        "year": year,
        "month": month,
        "weeks": calendar.monthcalendar(year, month),
        "unhealthy_days": unhealthy_days,
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
        "today": dt.date.today().isoformat(),
    }


def list_alarm_records(
    session: Any,
    *,
    start_dt: dt.datetime,
    end_dt: dt.datetime,
    service_id: int | None = None,
    send_status: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    query = session.query(AlarmRecord).filter(
        AlarmRecord.alarm_time >= start_dt,
        AlarmRecord.alarm_time < end_dt,
    )
    if service_id is not None:
        query = query.filter(AlarmRecord.service_id == service_id)
    if send_status:
        query = query.filter(AlarmRecord.send_status == send_status)

    total = query.count()
    rows = (
        query.order_by(AlarmRecord.alarm_time.desc(), AlarmRecord.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": [
            {
                "id": row.id,
                "service_id": row.service_id,
                "service_name": row.service_name,
                "endpoint_id": row.endpoint_id,
                "heartbeat_url": row.heartbeat_url,
                "success_rule": row.success_rule,
                "alarm_time": row.alarm_time.isoformat(),
                "channel": row.channel,
                "recipients": row.recipients,
                "send_status": row.send_status,
                "error": row.error,
                "check_status": row.check_status,
                "detail": row.detail,
            }
            for row in rows
        ],
    }
