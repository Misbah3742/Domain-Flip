"""
zone_parser.runner – Entry point for the zone-parser service.

Responsibilities
----------------
1. Download / read ICANN zone files (or poll WhoisXML) on a schedule.
2. Parse each file and upsert :class:`~app.database.Domain` records.
3. Enqueue newly discovered domains onto the monitor queue.
"""

from __future__ import annotations

import logging
import time

import schedule

from app.config import settings
from app.database import init_db
from app.zone_parser import WhoisXMLClient, ZoneFileParser

logger = logging.getLogger(__name__)


def run_zone_parse_cycle() -> None:
    """
    Single parse cycle – called by the scheduler.

    In a production deployment this would:
    * Download zone files from ICANN CZDS using OAuth2 credentials.
    * Or call the WhoisXML Expiring Domains feed.
    * Upsert discovered domains into PostgreSQL.
    * Enqueue them for monitoring.

    The stub below logs the intent and exits cleanly so that the interface
    can be validated without live credentials.
    """
    logger.info("zone_parser: starting parse cycle")
    # TODO: replace with real zone-file download + WhoisXMLClient calls
    logger.info("zone_parser: parse cycle complete")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("zone_parser service starting")
    init_db()

    # Run immediately on startup, then every hour
    run_zone_parse_cycle()
    schedule.every(1).hours.do(run_zone_parse_cycle)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
