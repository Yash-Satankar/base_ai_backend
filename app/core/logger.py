import logging
import os
from datetime import datetime
from pathlib import Path


def setup_logging(debug: bool = False):
    """
    Configure application-wide logging.
    Logs to console and to logs/app.log file.
    """
    # Create logs directory
    Path("logs").mkdir(exist_ok=True)

    log_level = logging.DEBUG if debug else logging.INFO

    # Log format
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    # Root logger
    logging.basicConfig(
        level=log_level,
        format=fmt,
        datefmt=date_fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                f"logs/app_{datetime.now().strftime('%Y%m%d')}.log",
                encoding="utf-8",
            ),
        ],
    )

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(
        f"Logging initialised | level={'DEBUG' if debug else 'INFO'}"
    )