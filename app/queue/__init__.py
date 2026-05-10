"""
queue – Redis-backed job queue management.

Uses the `rq` (Redis Queue) library so workers can be scaled independently.

Queues
------
zone_parser_q    Jobs for parsing zone-file chunks or calling WhoisXML
monitor_q        Per-domain status check jobs
sniper_q         High-priority registration jobs (processed ASAP)
"""

from __future__ import annotations

from redis import Redis
from rq import Queue

from app.config import settings

# ─── Redis connection ─────────────────────────────────────────────────────────

def get_redis_connection() -> Redis:
    """Return a reusable Redis connection."""
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        decode_responses=False,
    )


# ─── Named queues ─────────────────────────────────────────────────────────────

def get_zone_parser_queue(connection: Redis | None = None) -> Queue:
    conn = connection or get_redis_connection()
    return Queue("zone_parser", connection=conn)


def get_monitor_queue(connection: Redis | None = None) -> Queue:
    conn = connection or get_redis_connection()
    return Queue("monitor", connection=conn)


def get_sniper_queue(connection: Redis | None = None) -> Queue:
    """Sniper queue has higher priority – processed before other queues."""
    conn = connection or get_redis_connection()
    return Queue("sniper", connection=conn, default_timeout=60)


# ─── Convenience helpers ──────────────────────────────────────────────────────

def enqueue_domain_check(domain_name: str, connection: Redis | None = None) -> None:
    """Push a domain onto the monitor queue for a status check."""
    from app.monitor.checker import check_domain  # local import avoids circular dep
    queue = get_monitor_queue(connection)
    queue.enqueue(check_domain, domain_name)


def enqueue_snipe(domain_name: str, connection: Redis | None = None) -> None:
    """Push a domain onto the high-priority sniper queue."""
    from app.sniper.executor import attempt_registration
    queue = get_sniper_queue(connection)
    queue.enqueue(attempt_registration, domain_name)
