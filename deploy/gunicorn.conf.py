import os

bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"
worker_class = "gthread"
workers = int(os.getenv("GUNICORN_WORKERS", "1"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = 120
graceful_timeout = 30
keepalive = 5
log_dir = os.getenv("GUNICORN_LOG_DIR", "")
if log_dir:
    accesslog = os.path.join(log_dir, "access.log")
    errorlog = os.path.join(log_dir, "error.log")
else:
    accesslog = "-"
    errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")


def when_ready(server):
    """在 master 进程启动一次巡检调度器，避免多 worker 重复执行。"""
    if os.getenv("START_SCHEDULER", "1") != "1":
        return
    from heartbeat.app import start_scheduler

    start_scheduler()
    server.log.info("heartbeat scheduler started (every 1 minute)")
