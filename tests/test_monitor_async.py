"""Tests for the async monitor path."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.database import DomainStatus
import app.monitor as monitor_module
from app.monitor import AsyncDomainChecker


class FakeWhoisClient:
    def __init__(self, mapping: dict[str, dict]) -> None:
        self.mapping = mapping

    async def lookup(self, domain_name: str) -> dict:
        return self.mapping.get(domain_name, {})

    async def aclose(self) -> None:
        return None


def _payload_for(days_from_now: int) -> dict:
    expires_at = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return {
        "WhoisRecord": {
            "registryData": {
                "expiresDate": expires_at.isoformat().replace("+00:00", "Z"),
            }
        }
    }


class TestAsyncDomainChecker:
    def test_checks_many_domains_concurrently(self, monkeypatch):
        monkeypatch.setattr(monitor_module, "_enqueue_for_sniping", lambda domain_name: None)

        client = FakeWhoisClient(
            {
                "soon.com": _payload_for(10),
                "pending.com": _payload_for(-10),
            }
        )
        checker = AsyncDomainChecker(concurrency=20, client=client)

        results = asyncio.run(checker.check_domains(["soon.com", "pending.com"]))

        assert [result.domain_name for result in results] == ["soon.com", "pending.com"]
        assert results[0].status == DomainStatus.EXPIRING_SOON
        assert results[1].status == DomainStatus.PENDING_DELETE