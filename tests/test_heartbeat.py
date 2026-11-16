import datetime as dt
import os
import unittest

os.environ["HEARTBEAT_DB"] = "sqlite:///:memory:"

import app as hb  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
