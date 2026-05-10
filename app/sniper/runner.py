"""
sniper.runner – Entry point for the sniper service.

Starts an RQ worker that processes jobs from the ``sniper`` queue.
The worker is long-lived and restarts automatically (via Docker).
"""

from __future__ import annotations

import logging

from rq import Worker

from app.database import init_db
from app.queue import get_redis_connection, get_sniper_queue

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("sniper service starting")
    init_db()

    conn = get_redis_connection()
    queue = get_sniper_queue(conn)

    worker = Worker([queue], connection=conn)
    logger.info("sniper: RQ worker listening on queue 'sniper'")
    worker.work()


if __name__ == "__main__":
    main()
