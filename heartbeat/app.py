import os
import threading
import time

import schedule

from .config import PORT, START_SCHEDULER
from .model import Base, ensure_schema, engine
from .router import create_app
from .service import run_health_checks


def start_scheduler() -> None:
    schedule.every(1).minutes.do(run_health_checks)

    def _loop() -> None:
        while True:
            schedule.run_pending()
            time.sleep(1)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()


app = create_app()


def main() -> None:
    if START_SCHEDULER:
        start_scheduler()
    app.run(host="0.0.0.0", port=PORT)


ensure_schema()
Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    main()
