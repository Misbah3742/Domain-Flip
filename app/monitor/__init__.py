"""
monitor – Multi-threaded domain status checker with intelligent rate-limiting.

Public interface
----------------
DomainChecker       Thread-pool based checker; reads from the monitor Redis queue.
RateLimiter         Token-bucket rate limiter that prevents IP bans.
check_domain()      Top-level function invoked by RQ workers.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.database import DomainStatus

logger = logging.getLogger(__name__)


# ─── Rate Limiter (token-bucket algorithm) ────────────────────────────────────

class RateLimiter:
    """
    Thread-safe token-bucket rate limiter.

    Parameters
    ----------
    rate:
        Maximum number of calls allowed per *period* seconds.
    period:
        The sliding window duration in seconds (default: 1 second).

    Usage::

        limiter = RateLimiter(rate=5, period=1.0)  # 5 req/s
        limiter.acquire()   # blocks until a token is available
        make_whois_request()
    """

    def __init__(self, rate: float, period: float = 1.0) -> None:
        self._rate = rate
        self._period = period
        self._tokens = rate
        self._lock = threading.Lock()
        self._last_refill = time.monotonic()

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
            time.sleep(self._period / self._rate / 2)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._rate,
            self._tokens + elapsed * (self._rate / self._period),
        )
        self._last_refill = now


# ─── Status check result ──────────────────────────────────────────────────────

@dataclass
class CheckResult:
    domain_name: str
    status: DomainStatus
    expires_at: Optional[datetime]
    checked_at: Optional[datetime] = None

    def __post_init__(self):
        if self.checked_at is None:
            self.checked_at = datetime.now(timezone.utc)


# ─── Single-domain checker function (used as RQ job) ─────────────────────────

def check_domain(domain_name: str) -> CheckResult:
    """
    Perform a WHOIS / registry status check for *domain_name*.

    This function is the unit of work dispatched to RQ workers.  It:
    1. Looks up the domain via WHOIS (or the WhoisXML API).
    2. Determines the current lifecycle status.
    3. Persists the updated status to PostgreSQL.
    4. If the domain is in PENDING_DELETE, enqueues it for sniping.

    Returns
    -------
    CheckResult
        The resolved status and expiry date.
    """
    logger.info("monitor: checking domain %s", domain_name)

    # TODO: replace stub with real WHOIS lookup + DB update
    result = CheckResult(
        domain_name=domain_name,
        status=DomainStatus.ACTIVE,
        expires_at=None,
    )

    if result.status == DomainStatus.PENDING_DELETE:
        _enqueue_for_sniping(domain_name)

    return result


def _enqueue_for_sniping(domain_name: str) -> None:
    """Push a domain onto the sniper queue when it enters Pending Delete."""
    from app.queue import enqueue_snipe  # local import avoids circular dep
    enqueue_snipe(domain_name)
    logger.info("monitor: %s queued for sniping", domain_name)


# ─── Multi-threaded checker pool ──────────────────────────────────────────────

class DomainChecker:
    """
    Continuously dequeues domain names from Redis and checks them in a
    thread pool, respecting the configured rate limit.

    Parameters
    ----------
    max_workers:
        Number of concurrent checker threads.
    rate_limit:
        Maximum WHOIS lookups per second across all threads.

    Usage::

        checker = DomainChecker()
        checker.start()   # blocks; call in a dedicated process / thread
    """

    def __init__(
        self,
        max_workers: int | None = None,
        rate_limit: float = 2.0,
    ) -> None:
        self._max_workers = max_workers or settings.monitor_max_workers
        self._limiter = RateLimiter(rate=rate_limit)
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the thread pool and begin draining the monitor queue."""
        from app.queue import get_monitor_queue, get_redis_connection

        redis_conn = get_redis_connection()
        queue = get_monitor_queue(redis_conn)

        logger.info(
            "monitor: DomainChecker starting with %d workers", self._max_workers
        )
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            while not self._stop_event.is_set():
                job = queue.fetch_job(queue.job_ids[0]) if queue.job_ids else None
                if job is None:
                    time.sleep(settings.monitor_check_interval_seconds)
                    continue
                self._limiter.acquire()
                pool.submit(check_domain, job.args[0] if job.args else "")

    def stop(self) -> None:
        self._stop_event.set()
