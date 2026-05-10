"""
monitor – Multi-threaded domain status checker with intelligent rate-limiting.

Public interface
----------------
DomainChecker       Thread-pool based checker; reads from the monitor Redis queue.
RateLimiter         Token-bucket rate limiter that prevents IP bans.
check_domain()      Top-level function invoked by RQ workers.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

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


# ─── Async WHOIS client ───────────────────────────────────────────────────────

class AsyncWhoisXMLClient:
    """Non-blocking WhoisXML client used by the high-concurrency monitor path."""

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self._api_key = api_key or settings.whoisxml_api_key
        self._api_url = api_url or settings.whoisxml_api_url
        self._http = httpx.AsyncClient(
            timeout=timeout_seconds or settings.monitor_request_timeout_seconds,
        )

    async def lookup(self, domain_name: str) -> dict:
        if not self._api_key:
            return {}

        response = await self._http.get(
            self._api_url,
            params={
                "apiKey": self._api_key,
                "domainName": domain_name,
                "outputFormat": "JSON",
            },
        )
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._http.aclose()


def _parse_expires_at(payload: dict) -> Optional[datetime]:
    whois_record = payload.get("WhoisRecord", {})
    registry_data = whois_record.get("registryData", {})

    expires_raw = registry_data.get("expiresDate") or whois_record.get(
        "expiresDate"
    )
    if not expires_raw:
        return None

    try:
        return datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _classify_status(expires_at: Optional[datetime]) -> DomainStatus:
    if expires_at is None:
        return DomainStatus.ACTIVE

    now = datetime.now(timezone.utc)
    delta_seconds = (expires_at - now).total_seconds()

    if delta_seconds > 30 * 24 * 60 * 60:
        return DomainStatus.ACTIVE
    if delta_seconds > 0:
        return DomainStatus.EXPIRING_SOON
    if delta_seconds >= -5 * 24 * 60 * 60:
        return DomainStatus.REDEMPTION
    if delta_seconds >= -40 * 24 * 60 * 60:
        return DomainStatus.PENDING_DELETE
    return DomainStatus.AVAILABLE


async def check_domain_async(
    domain_name: str,
    client: AsyncWhoisXMLClient | None = None,
) -> CheckResult:
    """Asynchronously check one domain and return a lifecycle snapshot."""
    owns_client = client is None
    client = client or AsyncWhoisXMLClient()

    try:
        payload = await client.lookup(domain_name)
        expires_at = _parse_expires_at(payload)
        result = CheckResult(
            domain_name=domain_name,
            status=_classify_status(expires_at),
            expires_at=expires_at,
        )

        if result.status == DomainStatus.PENDING_DELETE:
            _enqueue_for_sniping(domain_name)

        return result
    except httpx.HTTPError as exc:
        logger.warning("monitor: async WHOIS lookup failed for %s: %s", domain_name, exc)
        return CheckResult(
            domain_name=domain_name,
            status=DomainStatus.ACTIVE,
            expires_at=None,
        )
    finally:
        if owns_client:
            await client.aclose()


class AsyncDomainChecker:
    """High-concurrency batch checker built on top of httpx.AsyncClient."""

    def __init__(
        self,
        concurrency: int | None = None,
        client: AsyncWhoisXMLClient | None = None,
    ) -> None:
        self._concurrency = concurrency or settings.monitor_concurrency
        self._client = client or AsyncWhoisXMLClient()
        self._owns_client = client is None

    async def check_domain(self, domain_name: str) -> CheckResult:
        return await check_domain_async(domain_name, client=self._client)

    async def check_domains(self, domain_names: list[str]) -> list[CheckResult]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def guarded_check(domain_name: str) -> CheckResult:
            async with semaphore:
                return await self.check_domain(domain_name)

        try:
            return await asyncio.gather(
                *(guarded_check(domain_name) for domain_name in domain_names)
            )
        finally:
            if self._owns_client:
                await self._client.aclose()


def check_domain(domain_name: str) -> CheckResult:
    """Compatibility wrapper used by the existing RQ job entrypoint."""
    return asyncio.run(check_domain_async(domain_name))


async def check_domains_async(domain_names: list[str]) -> list[CheckResult]:
    """Convenience helper for batch checking many domains concurrently."""
    checker = AsyncDomainChecker()
    return await checker.check_domains(domain_names)


# ─── Single-domain checker function (used as RQ job) ─────────────────────────
def _legacy_check_domain(domain_name: str) -> CheckResult:
    """Backward-compatible alias retained for older imports."""
    return check_domain(domain_name)


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
