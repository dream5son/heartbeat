import datetime as dt
import urllib.error
import urllib.request
from typing import Any

from .model import (
    DEFAULT_SUCCESS_RULE,
    DailyCheck,
    FailureRecord,
    Service,
    ServiceEndpoint,
    SessionLocal,
    YearlyCheck,
    day_column,
    minute_column,
    normalize_success_rule,
)


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
) -> bool:
    minute_col = minute_column(check_time)
    day_col = day_column(check_time.date())

    endpoint_results: list[tuple[bool, int, str, str, str]] = []
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
            session.add(
                FailureRecord(
                    service_id=service.id,
                    check_time=check_time,
                    status=status_code,
                    log=f"url={heartbeat_url or '-'} rule={success_rule} detail={detail}",
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
            }
            for endpoint in service.endpoints
        ],
        "heartbeat_urls": [e.heartbeat_url for e in service.endpoints],
        "child_service_ids": [child.id for child in service.children],
    }
