"""Tests for the upgraded sniper engine."""

from __future__ import annotations

from app.database import SnipeResult
from app.sniper import (
    CircuitBreaker,
    DomainValuationResult,
    DomainValuationClient,
    attempt_registration,
)
import app.sniper as sniper_module


class TestDomainValuationClient:
    def test_extracts_nested_numeric_value(self):
        payload = {"data": {"estimatedValue": "$1,250.50"}}
        value = DomainValuationClient._extract_estimated_value(payload)
        assert value == 1250.5

    def test_heuristic_is_positive_for_short_com_domain(self):
        value = DomainValuationClient._heuristic_value("brandable.com")
        assert value > 0


class TestCircuitBreaker:
    def test_rate_limit_opens_and_recovers(self):
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30)

        assert breaker.allow() is True
        wait_seconds = breaker.record_rate_limit(10)
        assert wait_seconds == 30.0
        assert breaker.allow() is True

        breaker.record_rate_limit(5)
        assert breaker.allow() is False
        breaker.record_success()
        assert breaker.allow() is True


class TestValuationGate:
    def test_skips_low_value_domains_before_registering(self, monkeypatch):
        captured = []

        class FakeValuator:
            def evaluate(self, domain_name: str) -> DomainValuationResult:
                return DomainValuationResult(
                    domain_name=domain_name,
                    estimated_value_usd=100.0,
                    source="api",
                )

            def close(self) -> None:
                return None

        class FakeClient:
            def __init__(self, *args, **kwargs):
                return None

            def register(self, domain_name: str):
                raise AssertionError("register() should not be called for low-value domains")

            def close(self) -> None:
                return None

        class FakeNotifier:
            def __init__(self, *args, **kwargs):
                return None

            def notify_success(self, *args, **kwargs):
                raise AssertionError("notify_success() should not be called for skipped domains")

            def close(self) -> None:
                return None

        monkeypatch.setattr(sniper_module, "DomainValuationClient", FakeValuator)
        monkeypatch.setattr(sniper_module, "DynadotClient", FakeClient)
        monkeypatch.setattr(sniper_module, "NamejetClient", FakeClient)
        monkeypatch.setattr(sniper_module, "NotificationClient", FakeNotifier)
        monkeypatch.setattr(sniper_module, "_persist_result", lambda result: captured.append(result))

        result = attempt_registration("tiny-example.com")

        assert result.result == SnipeResult.SKIPPED_LOW_VALUE
        assert len(captured) == 1
        assert captured[0].result == SnipeResult.SKIPPED_LOW_VALUE