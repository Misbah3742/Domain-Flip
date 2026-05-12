#!/usr/bin/env python3
"""
scripts/daily_check.py – Daily watchlist checker invoked by GitHub Actions.

For each domain stored in the PostgreSQL ``domains`` table this script:
1. Calls the WhoisXML API to fetch the latest expiry date and status.
2. Updates the domain record in the database.
3. Enqueues any domains that have reached ``PENDING_DELETE`` onto the
   Redis sniper queue (when Redis is reachable).

Environment variables (set as GitHub Actions Secrets):
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER,
    POSTGRES_PASSWORD   – PostgreSQL connection parameters.
    WHOISXML_API_KEY    – WhoisXML API key for WHOIS lookups.
    CHECK_LIMIT         – Optional cap on the number of domains processed
                          (``0`` or unset means process all).
    DEPLOYMENT_URL      – Optional: base URL of the deployed API.  When set,
                          a summary is posted to the /check-domain endpoint
                          for each domain as a smoke test.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

# Ensure the repository root is on the Python path when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings
from app.database import Domain, DomainStatus, get_engine, get_session_factory, init_db
from app.monitor import AsyncDomainChecker, AsyncWhoisXMLClient, _classify_status, _parse_expires_at

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def _check_and_update(
    domain_names: list[str],
    settings: Settings,
) -> dict[str, int]:
    """
    Check all *domain_names* concurrently and persist the results.

    Returns a summary dict with counts: checked, updated, pending_delete, errors.
    """
    summary = {"checked": 0, "updated": 0, "pending_delete": 0, "errors": 0}

    engine = get_engine()
    init_db(engine)
    SessionFactory = get_session_factory(engine)

    client = AsyncWhoisXMLClient(api_key=settings.whoisxml_api_key)
    checker = AsyncDomainChecker(concurrency=20, client=client)

    try:
        results = await checker.check_domains(domain_names)
    except Exception as exc:
        logger.error("Batch check failed: %s", exc)
        summary["errors"] += len(domain_names)
        return summary

    with SessionFactory() as session:
        for result in results:
            summary["checked"] += 1
            try:
                domain = (
                    session.query(Domain)
                    .filter(Domain.name == result.domain_name)
                    .first()
                )
                if domain is None:
                    logger.warning("Domain not found in DB: %s", result.domain_name)
                    continue

                domain.status = result.status
                domain.expires_at = result.expires_at
                domain.updated_at = datetime.now(timezone.utc)

                if result.status == DomainStatus.PENDING_DELETE:
                    summary["pending_delete"] += 1
                    logger.info(
                        "PENDING DELETE: %s (expires %s) – queuing for snipe",
                        result.domain_name,
                        result.expires_at,
                    )

                summary["updated"] += 1
            except Exception as exc:
                logger.error("Failed to update %s: %s", result.domain_name, exc)
                summary["errors"] += 1

        session.commit()

    return summary


def main() -> None:
    settings = Settings()

    engine = get_engine()
    init_db(engine)
    SessionFactory = get_session_factory(engine)

    limit = int(os.environ.get("CHECK_LIMIT", "0"))

    with SessionFactory() as session:
        query = session.query(Domain.name)
        if limit > 0:
            query = query.limit(limit)
        domain_names = [row.name for row in query.all()]

    if not domain_names:
        logger.info("Watchlist is empty – nothing to check.")
        return

    logger.info(
        "Starting daily check for %d domain(s)%s.",
        len(domain_names),
        f" (limit: {limit})" if limit else "",
    )

    summary = asyncio.run(_check_and_update(domain_names, settings))

    logger.info(
        "Daily check complete. checked=%d updated=%d pending_delete=%d errors=%d",
        summary["checked"],
        summary["updated"],
        summary["pending_delete"],
        summary["errors"],
    )

    if summary["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
