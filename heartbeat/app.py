import os
import threading
import time

import schedule

from .config import PORT, START_SCHEDULER
from .logging_config import logger, setup_logging
from .model import Base, ensure_schema, engine
from .router import create_app
from .service import run_health_checks

setup_logging()


def start_scheduler() -> None:
    schedule.every().minute.at(":00").do(run_health_checks)

    def _loop() -> None:
        while True:
            try:
                schedule.run_pending()
            except Exception:  # noqa: BLE001
                logger.exception("scheduled health check failed")
            time.sleep(1)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    logger.info("scheduler started: health checks every 1 minute")


app = create_app()


def main() -> None:
    logger.info(
        "heartbeat starting: host=0.0.0.0 port={} scheduler={}",
        PORT,
        START_SCHEDULER,
    )
    if START_SCHEDULER:
        start_scheduler()
    app.run(host="0.0.0.0", port=PORT)


ensure_schema()
Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    main()
