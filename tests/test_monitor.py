"""
tests/test_monitor.py

Unit tests for the monitor module (RateLimiter and CheckResult).
No external connections required.
"""

import threading
import time
from datetime import datetime, timezone

import pytest

from app.database import DomainStatus
from app.monitor import CheckResult, RateLimiter


class TestRateLimiter:
    """Token-bucket rate limiter correctness."""

    def test_single_acquire_does_not_block(self):
        """A fresh limiter with rate ≥ 1 should not block on first acquire."""
        limiter = RateLimiter(rate=5, period=1.0)
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"First acquire should be near-instant, took {elapsed:.3f}s"

    def test_tokens_exhausted_causes_delay(self):
        """Consuming all tokens should make subsequent acquires wait."""
        rate = 2
        limiter = RateLimiter(rate=rate, period=1.0)
        # Drain all tokens
        for _ in range(rate):
            limiter.acquire()
        # Next acquire must wait for a refill
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.1, "Expected a short wait after exhausting tokens"

    def test_thread_safety(self):
        """Multiple threads can acquire concurrently without errors."""
        limiter = RateLimiter(rate=10, period=1.0)
        errors = []

        def worker():
            try:
                limiter.acquire()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"Thread errors: {errors}"


class TestCheckResult:
    """CheckResult dataclass behaviour."""

    def test_defaults_checked_at_to_now(self):
        before = datetime.now(timezone.utc)
        result = CheckResult(
            domain_name="example.com",
            status=DomainStatus.ACTIVE,
            expires_at=None,
        )
        after = datetime.now(timezone.utc)
        assert before <= result.checked_at <= after

    def test_explicit_checked_at_preserved(self):
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        result = CheckResult(
            domain_name="example.com",
            status=DomainStatus.PENDING_DELETE,
            expires_at=None,
            checked_at=ts,
        )
        assert result.checked_at == ts

    def test_fields_stored_correctly(self):
        result = CheckResult(
            domain_name="drop.com",
            status=DomainStatus.PENDING_DELETE,
            expires_at=None,
        )
        assert result.domain_name == "drop.com"
        assert result.status == DomainStatus.PENDING_DELETE
