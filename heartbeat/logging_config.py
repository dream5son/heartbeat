import logging
import sys
from pathlib import Path

from loguru import logger

from .config import LOG_DIR, LOG_LEVEL

_CONFIGURED = False


class InterceptHandler(logging.Handler):
    """把标准库 logging 记录转发到 Loguru。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    logger.remove()
    logger.add(
        sys.stderr,
        level=LOG_LEVEL,
        colorize=sys.stderr.isatty(),
        backtrace=False,
        diagnose=False,
    )

    log_dir = Path(LOG_DIR)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        logger.warning("无法创建日志目录 {}，仅输出到控制台: {}", log_dir, err)
    else:
        common = {
            "rotation": "10 MB",
            "retention": "30 days",
            "compression": "zip",
            "enqueue": True,
            "encoding": "utf-8",
            "backtrace": True,
            "diagnose": False,
        }
        logger.add(log_dir / "heartbeat.log", level=LOG_LEVEL, **common)
        logger.add(log_dir / "heartbeat-error.log", level="ERROR", **common)

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
