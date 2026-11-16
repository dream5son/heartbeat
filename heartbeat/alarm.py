import datetime as dt
import json
import smtplib
from email.message import EmailMessage
from typing import Any, Callable

from sqlalchemy import func

from .config import (
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_TIMEOUT,
    SMTP_USE_SSL,
    SMTP_USE_TLS,
    SMTP_USERNAME,
)
from .logging_config import logger
from .model import (
    AlarmRecord,
    normalize_alarm_channels,
    normalize_success_rule,
    parse_alarm_recipients,
)

Notifier = Callable[[list[str], str, str], None]


def send_email_alarm(recipients: list[str], subject: str, body: str) -> None:
    if not SMTP_HOST:
        raise RuntimeError("SMTP_HOST is not configured")
    if not recipients:
        raise ValueError("no alarm recipients configured")

    sender = SMTP_FROM or SMTP_USERNAME
    if not sender:
        raise RuntimeError("SMTP_FROM is not configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    # 外层 try except 保障不会因为发送失败中断，此处有问题raise即可
    if SMTP_USE_SSL:
        client = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
    else:
        client = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)

    with client as smtp:
        if not SMTP_USE_SSL and SMTP_USE_TLS:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)


def _has_quota(session: Any, endpoint: Any, check_time: dt.datetime) -> bool:
    window_start = check_time - dt.timedelta(minutes=endpoint.alarm_window_minutes)
    sent_count = (
        session.query(func.count(AlarmRecord.id))
        .filter(
            AlarmRecord.endpoint_id == endpoint.id,
            AlarmRecord.channel == "email",
            AlarmRecord.send_status == "sent",
            AlarmRecord.alarm_time >= window_start,
            AlarmRecord.alarm_time <= check_time,
        )
        .scalar()
    )
    return int(sent_count or 0) < endpoint.alarm_max_per_window


def _build_detail(
    service: Any,
    check_time: dt.datetime,
    heartbeat_url: str,
    success_rule: str,
    status_code: int,
    endpoint_detail: str,
    endpoint_results: list[tuple[bool, int, str, str, str]],
) -> str:
    payload = {
        "service": service.name,
        "service_status": "unhealthy",
        "check_time": check_time.isoformat(),
        "trigger": {
            "heartbeat_url": heartbeat_url,
            "success_rule": success_rule,
            "status": status_code,
            "detail": endpoint_detail,
        },
        "endpoints": [
            {
                "heartbeat_url": url,
                "success_rule": rule,
                "ok": ok,
                "status": status,
                "detail": detail,
            }
            for ok, status, detail, url, rule in endpoint_results
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_body(
    service_name: str,
    check_time: dt.datetime,
    heartbeat_url: str,
    success_rule: str,
    status_code: int,
    endpoint_detail: str,
    endpoint_results: list[tuple[bool, int, str, str, str]],
) -> str:
    lines = [
        f"服务: {service_name}",
        f"检测时间: {check_time.isoformat()}",
        f"失败地址: {heartbeat_url or '-'}",
        f"成功规则: {success_rule}",
        f"HTTP 状态: {status_code}",
        f"错误详情: {endpoint_detail}",
        "",
        "本次所有检测结果:",
    ]
    for ok, status, detail, url, rule in endpoint_results:
        result = "通过" if ok else "失败"
        lines.append(
            f"- [{result}] {url or '-'} 规则={rule} 状态={status} 明细={detail}"
        )
    return "\n".join(lines)


def maybe_send_alarm(
    session: Any,
    service: Any,
    endpoint: Any,
    check_time: dt.datetime,
    status_code: int,
    endpoint_detail: str,
    endpoint_results: list[tuple[bool, int, str, str, str]],
    notifier: Notifier = send_email_alarm,
) -> bool:
    if not endpoint.alarm_enabled:
        return False

    channels = normalize_alarm_channels(endpoint.alarm_channels)
    if "email" not in channels.split(","):
        return False

    recipients = parse_alarm_recipients(endpoint.alarm_recipients)
    if not recipients:
        return False

    if not _has_quota(session, endpoint, check_time):
        logger.info(
            "alarm suppressed by rate limit: service={} url={} window_minutes={} max_per_window={}",
            service.name,
            endpoint.heartbeat_url,
            endpoint.alarm_window_minutes,
            endpoint.alarm_max_per_window,
        )
        return False

    success_rule = normalize_success_rule(endpoint.success_rule)
    detail = _build_detail(
        service,
        check_time,
        endpoint.heartbeat_url,
        success_rule,
        status_code,
        endpoint_detail,
        endpoint_results,
    )
    subject = f"[Heartbeat] 服务异常: {service.name}"
    body = _build_body(
        service.name,
        check_time,
        endpoint.heartbeat_url,
        success_rule,
        status_code,
        endpoint_detail,
        endpoint_results,
    )

    send_status = "sent"
    error = None
    try:
        notifier(recipients, subject, body)
    except Exception as exc:  # noqa: BLE001
        send_status = "failed"
        error = str(exc)[:2000]
        logger.warning(
            "alarm send failed: service={} url={} recipients={} error={}",
            service.name,
            endpoint.heartbeat_url,
            ",".join(recipients),
            error,
        )

    session.add(
        AlarmRecord(
            service_id=service.id,
            endpoint_id=endpoint.id,
            service_name=service.name,
            heartbeat_url=endpoint.heartbeat_url,
            success_rule=success_rule,
            alarm_time=check_time,
            channel="email",
            recipients=",".join(recipients),
            send_status=send_status,
            error=error,
            check_status=int(status_code),
            detail=detail,
        )
    )
    session.flush()

    if send_status == "sent":
        logger.info(
            "alarm sent: service={} url={} recipients={} status={}",
            service.name,
            endpoint.heartbeat_url,
            ",".join(recipients),
            status_code,
        )
    return send_status == "sent"
