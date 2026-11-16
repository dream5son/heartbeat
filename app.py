import heartbeat_app as _hb

# App entrypoint compatibility: expose the real application objects at module level
for _name in [
    "Base",
    "DEFAULT_SUCCESS_RULE",
    "Service",
    "ServiceEndpoint",
    "DailyCheck",
    "YearlyCheck",
    "FailureRecord",
    "database_url",
    "DATABASE_URL",
    "engine",
    "SessionLocal",
    "ensure_schema",
    "minute_column",
    "day_column",
    "normalize_success_rule",
    "endpoint_check_passed",
    "check_url",
    "parse_heartbeat_configs",
    "perform_service_check",
    "run_health_checks",
    "_daily_row_health",
    "service_to_dict",
    "create_app",
    "start_scheduler",
    "app",
    "main",
]:
    if hasattr(_hb, _name):
        globals()[_name] = getattr(_hb, _name)


if __name__ == "__main__":
    _hb.main()
