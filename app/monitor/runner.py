"""
monitor.runner – Entry point for the monitor service.

Starts a :class:`~app.monitor.DomainChecker` that drains the Redis monitor
queue and periodically re-enqueues all tracked domains for re-checking.
"""

from __future__ import annotations

import logging

from app.database import init_db
from app.monitor import DomainChecker

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("monitor service starting")
    init_db()

    checker = DomainChecker()
    try:
        checker.start()
    except KeyboardInterrupt:
        checker.stop()
        logger.info("monitor service stopped")


if __name__ == "__main__":
    main()
