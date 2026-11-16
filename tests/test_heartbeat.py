import datetime as dt
import importlib
import os
import types
import unittest

os.environ["HEARTBEAT_DB"] = "sqlite:///:memory:"

heartbeat_app_module = importlib.import_module("heartbeat.app")
from heartbeat.model import (
    Base,
    DailyCheck,
    FailureRecord,
    Service,
    ServiceEndpoint,
    SessionLocal,
    YearlyCheck,
    engine,
)
from heartbeat.service import _service_status_history, perform_service_check

hb = types.SimpleNamespace(
    app=heartbeat_app_module.app,
    Base=Base,
    DailyCheck=DailyCheck,
    FailureRecord=FailureRecord,
    Service=Service,
    ServiceEndpoint=ServiceEndpoint,
    SessionLocal=SessionLocal,
    YearlyCheck=YearlyCheck,
    engine=engine,
    perform_service_check=perform_service_check,
    _service_status_history=_service_status_history,
)


class HeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        hb.Base.metadata.drop_all(bind=hb.engine)
        hb.Base.metadata.create_all(bind=hb.engine)

    def test_service_config_supports_subservices_and_urls(self) -> None:
        client = hb.app.test_client()

        parent_resp = client.post(
            "/api/services",
            json={
                "name": "gateway",
                "heartbeat_configs": [{"heartbeat_url": "http://a/health", "success_rule": "status==200"}],
            },
        )
        self.assertEqual(parent_resp.status_code, 201)
        parent_id = parent_resp.get_json()["id"]
        self.assertEqual(parent_resp.get_json()["heartbeat_configs"][0]["success_rule"], "status==200")

        child_resp = client.post(
            "/api/services",
            json={
                "name": "gateway-worker",
                "parent_id": parent_id,
                "heartbeat_configs": [
                    {"heartbeat_url": "http://b/health", "success_rule": "status==200"},
                    {"heartbeat_url": "http://c/health", "success_rule": "body_contains:OK"},
                ],
            },
        )
        self.assertEqual(child_resp.status_code, 201)

        list_resp = client.get("/api/services")
        payload = list_resp.get_json()
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(payload["services"]), 2)

    def test_success_rule_is_respected_during_checks(self) -> None:
        session = hb.SessionLocal()
        service = hb.Service(name="auth")
        service.endpoints.append(
            hb.ServiceEndpoint(heartbeat_url="http://example/health", success_rule="body_contains:OK")
        )
        session.add(service)
        session.commit()
        session.refresh(service)

        def checker(_: str) -> tuple[bool, int, str, str]:
            return True, 200, "status=200", "ERROR"

        hb.perform_service_check(session, service, dt.datetime(2026, 1, 1, 0, 5), checker=checker)
        session.commit()

        daily = session.query(hb.DailyCheck).filter_by(service_id=service.id, date=dt.date(2026, 1, 1)).one()
        self.assertEqual(getattr(daily, "minute_0005"), 0)
        session.close()

    def test_checking_persists_daily_yearly_and_failures(self) -> None:
        session = hb.SessionLocal()
        service = hb.Service(name="billing")
        service.endpoints.append(hb.ServiceEndpoint(heartbeat_url="http://example/health"))
        session.add(service)
        session.commit()
        session.refresh(service)

        check_time = dt.datetime(2026, 1, 1, 0, 5)

        def fail_checker(_: str) -> tuple[bool, int, str]:
            return False, 500, "bad gateway"

        hb.perform_service_check(session, service, check_time, checker=fail_checker)
        session.commit()

        daily = session.query(hb.DailyCheck).filter_by(service_id=service.id, date=check_time.date()).one()
        yearly = session.query(hb.YearlyCheck).filter_by(service_id=service.id, year=2026).one()
        failures = session.query(hb.FailureRecord).filter_by(service_id=service.id).all()

        self.assertEqual(getattr(daily, "minute_0005"), 0)
        self.assertEqual(getattr(yearly, "day_0101"), 0)
        self.assertEqual(len(failures), 1)
        self.assertIn("bad gateway", failures[0].log)

        session.close()

    def test_homepage_and_status_tree_with_failure_hint(self) -> None:
        session = hb.SessionLocal()
        root = hb.Service(name="gateway")
        root.endpoints.append(hb.ServiceEndpoint(heartbeat_url="http://gateway/health"))
        child = hb.Service(name="gateway-worker", parent=root)
        child.endpoints.append(hb.ServiceEndpoint(heartbeat_url="http://worker/health"))
        session.add_all([root, child])
        session.commit()
        session.refresh(root)
        session.refresh(child)

        today = dt.date.today()
        root_daily = hb.DailyCheck(service_id=root.id, date=today)
        child_daily = hb.DailyCheck(service_id=child.id, date=today)
        setattr(root_daily, "minute_0001", 1)
        setattr(child_daily, "minute_0002", 0)
        session.add_all([root_daily, child_daily])
        session.add(
            hb.FailureRecord(
                service_id=child.id,
                check_time=dt.datetime.combine(today, dt.time(0, 2)),
                status=500,
                log="url=http://worker/health rule=status==200 detail=timeout",
            )
        )
        session.commit()
        session.close()

        client = hb.app.test_client()
        home_resp = client.get("/")
        self.assertEqual(home_resp.status_code, 200)
        self.assertIn("失败日历", home_resp.get_data(as_text=True))
        self.assertIn("系统状态", home_resp.get_data(as_text=True))

        config_resp = client.get("/config")
        self.assertEqual(config_resp.status_code, 200)
        self.assertIn("服务检测配置", config_resp.get_data(as_text=True))

        tree_resp = client.get("/api/status-tree")
        self.assertEqual(tree_resp.status_code, 200)
        payload = tree_resp.get_json()
        self.assertEqual(len(payload["roots"]), 1)
        root_node = payload["roots"][0]
        self.assertEqual(root_node["status"], "partial")
        self.assertEqual(len(root_node["children"]), 1)
        self.assertEqual(root_node["children"][0]["status"], "unhealthy")
        self.assertIsNotNone(root_node["failure_hint"])
        self.assertIn("timeout", root_node["failure_hint"]["summary"])

    def test_recent_hours_history_uses_minute_resolution_and_unknown_for_missing_values(self) -> None:
        session = hb.SessionLocal()
        service = hb.Service(name="api")
        session.add(service)
        session.commit()
        session.refresh(service)

        now = dt.datetime.now().replace(second=0, microsecond=0)
        start = now - dt.timedelta(minutes=59)
        daily = hb.DailyCheck(service_id=service.id, date=now.date())
        setattr(daily, f"minute_{(start.hour * 60 + start.minute):04d}", 1)
        setattr(daily, f"minute_{(now.hour * 60 + now.minute):04d}", 0)
        session.add(daily)
        session.commit()

        history = hb._service_status_history(session, service.id, hours=1, check_time=now)
        self.assertEqual(len(history), 60)
        self.assertEqual(history[0], "healthy")
        self.assertEqual(history[-1], "unhealthy")
        self.assertIn("unknown", history)

        client = hb.app.test_client()
        resp = client.get(f"/api/services/{service.id}/status/recent-hours?hours=1")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(len(payload["status"]), 60)
        self.assertEqual(payload["status"][0], "healthy")
        self.assertEqual(payload["status"][-1], "unhealthy")

    def test_recent_daily_detail_returns_counts_and_failure_times(self) -> None:
        session = hb.SessionLocal()
        service = hb.Service(name="daily-api")
        session.add(service)
        session.commit()
        session.refresh(service)

        today = dt.date.today()
        all_failed_day = today - dt.timedelta(days=2)
        partial_day = today - dt.timedelta(days=1)

        all_failed = hb.DailyCheck(service_id=service.id, date=all_failed_day)
        for minute_idx in range(24 * 60):
            setattr(all_failed, f"minute_{minute_idx:04d}", 0)

        partial = hb.DailyCheck(service_id=service.id, date=partial_day)
        setattr(partial, "minute_0001", 1)
        setattr(partial, "minute_0015", 0)

        healthy = hb.DailyCheck(service_id=service.id, date=today)
        setattr(healthy, "minute_0002", 1)

        session.add_all([all_failed, partial, healthy])
        session.commit()
        service_id = service.id
        session.close()

        client = hb.app.test_client()
        resp = client.get(f"/api/services/{service_id}/status/recent-daily-detail?days=3")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(len(payload["days"]), 3)

        failed_info = payload["days"][0]
        self.assertEqual(failed_info["status"], "unhealthy")
        self.assertEqual(failed_info["healthy_count"], 0)
        self.assertEqual(failed_info["unhealthy_count"], 24 * 60)
        self.assertEqual(failed_info["unknown_count"], 0)
        self.assertEqual(failed_info["failure_times"][0], "00:00")
        self.assertEqual(failed_info["failure_times"][-1], "23:59")

        partial_info = payload["days"][1]
        self.assertEqual(partial_info["status"], "unhealthy")
        self.assertEqual(partial_info["healthy_count"], 1)
        self.assertEqual(partial_info["unhealthy_count"], 1)
        self.assertIn("00:15", partial_info["failure_times"])

        healthy_info = payload["days"][2]
        self.assertEqual(healthy_info["status"], "healthy")
        self.assertEqual(healthy_info["healthy_count"], 1)
        self.assertEqual(healthy_info["unhealthy_count"], 0)

    def test_recent_daily_detail_supports_explicit_date_range(self) -> None:
        session = hb.SessionLocal()
        service = hb.Service(name="range-api")
        session.add(service)
        session.commit()
        session.refresh(service)

        today = dt.date.today()
        target_start = today - dt.timedelta(days=4)
        target_end = today - dt.timedelta(days=2)

        for day, minute_value in (
            (target_start, 1),
            (target_start + dt.timedelta(days=1), 0),
            (target_end, 1),
        ):
            daily = hb.DailyCheck(service_id=service.id, date=day)
            setattr(daily, "minute_0001", minute_value)
            session.add(daily)

        session.commit()
        service_id = service.id
        session.close()

        client = hb.app.test_client()
        resp = client.get(
            (
                f"/api/services/{service_id}/status/recent-daily-detail"
                f"?start_date={target_start.isoformat()}&end_date={target_end.isoformat()}"
            )
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(len(payload["days"]), 3)
        self.assertEqual(payload["days"][0]["date"], target_start.isoformat())
        self.assertEqual(payload["days"][-1]["date"], target_end.isoformat())
        self.assertEqual(payload["days"][0]["status"], "healthy")
        self.assertEqual(payload["days"][1]["status"], "unhealthy")

    def test_recent_daily_detail_rejects_invalid_date_range_queries(self) -> None:
        session = hb.SessionLocal()
        service = hb.Service(name="range-validation")
        session.add(service)
        session.commit()
        service_id = service.id
        session.close()

        client = hb.app.test_client()
        only_start_resp = client.get(
            f"/api/services/{service_id}/status/recent-daily-detail?start_date=2026-01-01"
        )
        self.assertEqual(only_start_resp.status_code, 400)

        bad_format_resp = client.get(
            f"/api/services/{service_id}/status/recent-daily-detail?start_date=2026/01/01&end_date=2026-01-02"
        )
        self.assertEqual(bad_format_resp.status_code, 400)

        reversed_range_resp = client.get(
            f"/api/services/{service_id}/status/recent-daily-detail?start_date=2026-01-03&end_date=2026-01-01"
        )
        self.assertEqual(reversed_range_resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
