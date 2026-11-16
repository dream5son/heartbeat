import os
import time

from dotenv import load_dotenv


load_dotenv()

# 所有时间统一按本地时区处理；默认东八区，可通过 HEARTBEAT_TZ 或 TZ 覆盖。
TIMEZONE = os.getenv("HEARTBEAT_TZ") or os.getenv("TZ") or "Asia/Shanghai"
if TIMEZONE:
    os.environ["TZ"] = TIMEZONE
    if hasattr(time, "tzset"):
        time.tzset()

DATABASE_URL = os.getenv("HEARTBEAT_DB", "sqlite:///heartbeat.db")
PORT = int(os.getenv("PORT", "5000"))
START_SCHEDULER = os.getenv("START_SCHEDULER", "1") == "1"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = os.getenv("LOG_DIR", "logs")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USERNAME)
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "1") == "1"
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "0") == "1"
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "10"))
