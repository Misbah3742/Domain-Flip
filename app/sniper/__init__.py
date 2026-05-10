"""
sniper – High-frequency domain registration executor.

Public interface
----------------
RegistrarClient     Abstract base class for registrar API clients.
DynadotClient       Concrete client for the Dynadot API.
NamejetClient       Concrete client for the Namejet API.
attempt_registration()  RQ job: tries to register a domain the moment it drops.
"""

from __future__ import annotations

import abc
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.config import settings
from app.database import SnipeResult

logger = logging.getLogger(__name__)


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class RegistrationResult:
    domain_name: str
    registrar: str
    result: SnipeResult
    error_message: Optional[str] = None
    attempted_at: Optional[datetime] = None

    def __post_init__(self):
        if self.attempted_at is None:
            self.attempted_at = datetime.now(timezone.utc)


@dataclass
class DomainValuationResult:
    domain_name: str
    estimated_value_usd: float
    source: str = "heuristic"
    raw: dict[str, Any] | None = None


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_seconds: int = settings.registrar_cooldown_seconds
    failure_count: int = 0
    opened_until: float = 0.0

    def allow(self) -> bool:
        return time.monotonic() >= self.opened_until

    def remaining_seconds(self) -> float:
        return max(0.0, self.opened_until - time.monotonic())

    def record_success(self) -> None:
        self.failure_count = 0
        self.opened_until = 0.0

    def record_rate_limit(self, retry_after_seconds: float | None = None) -> float:
        self.failure_count += 1
        wait_seconds = max(
            retry_after_seconds or 0.0,
            float(self.cooldown_seconds),
        )
        if self.failure_count >= self.failure_threshold:
            self.opened_until = time.monotonic() + wait_seconds
        return wait_seconds


class DomainValuationClient:
    """Queries a valuation API and falls back to a local heuristic when absent."""

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self._api_key = api_key or settings.valuation_api_key
        self._api_url = api_url or settings.valuation_api_url
        self._http = httpx.Client(
            timeout=timeout_seconds or settings.notification_timeout_seconds,
        )

    def evaluate(self, domain_name: str) -> DomainValuationResult:
        if not self._api_key or not self._api_url:
            return DomainValuationResult(
                domain_name=domain_name,
                estimated_value_usd=self._heuristic_value(domain_name),
                source="heuristic",
            )

        try:
            response = self._http.get(
                self._api_url,
                params={"apiKey": self._api_key, "domain": domain_name},
            )
            response.raise_for_status()
            payload = response.json()
            value = self._extract_estimated_value(payload)
            if value is None:
                value = self._heuristic_value(domain_name)
                source = "heuristic"
            else:
                source = "api"
            return DomainValuationResult(
                domain_name=domain_name,
                estimated_value_usd=value,
                source=source,
                raw=payload,
            )
        except httpx.HTTPError as exc:
            logger.warning("sniper: valuation lookup failed for %s: %s", domain_name, exc)
            return DomainValuationResult(
                domain_name=domain_name,
                estimated_value_usd=self._heuristic_value(domain_name),
                source="heuristic",
            )

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _extract_estimated_value(payload: dict[str, Any]) -> float | None:
        candidates: list[Any] = []

        for key in ("estimated_value_usd", "estimatedValueUsd", "estimatedValue", "value"):
            if key in payload:
                candidates.append(payload[key])

        for container_key in ("data", "valuation", "result"):
            container = payload.get(container_key)
            if isinstance(container, dict):
                for key in ("estimated_value_usd", "estimatedValueUsd", "estimatedValue", "value"):
                    if key in container:
                        candidates.append(container[key])

        for candidate in candidates:
            value = DomainValuationClient._coerce_float(candidate)
            if value is not None:
                return value
        return None

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.strip().replace("$", "").replace(",", "")
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    @staticmethod
    def _heuristic_value(domain_name: str) -> float:
        sld = domain_name.split(".", 1)[0].lower()
        tld = domain_name.rsplit(".", 1)[-1].lower() if "." in domain_name else ""

        length_score = max(0.0, 28.0 - float(len(sld))) * 18.0
        tld_bonus = {
            "com": 220.0,
            "io": 160.0,
            "ai": 180.0,
            "net": 90.0,
            "org": 60.0,
        }.get(tld, 25.0)
        punctuation_penalty = 70.0 if "-" in sld else 0.0
        digit_penalty = 45.0 if any(char.isdigit() for char in sld) else 0.0

        return round(
            max(25.0, tld_bonus + length_score - punctuation_penalty - digit_penalty),
            2,
        )


class NotificationClient:
    """Sends success alerts to Discord and/or Telegram when configured."""

    def __init__(
        self,
        discord_webhook_url: str | None = None,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self._discord_webhook_url = discord_webhook_url or settings.discord_webhook_url
        self._telegram_bot_token = telegram_bot_token or settings.telegram_bot_token
        self._telegram_chat_id = telegram_chat_id or settings.telegram_chat_id
        self._http = httpx.Client(
            timeout=timeout_seconds or settings.notification_timeout_seconds,
        )

    def notify_success(
        self,
        result: RegistrationResult,
        valuation: DomainValuationResult | None = None,
    ) -> list[str]:
        message = self._build_message(result, valuation)
        delivered: list[str] = []

        if self._discord_webhook_url:
            self._post_discord(message)
            delivered.append("discord")

        if self._telegram_bot_token and self._telegram_chat_id:
            self._post_telegram(message)
            delivered.append("telegram")

        return delivered

    def close(self) -> None:
        self._http.close()

    def _build_message(
        self,
        result: RegistrationResult,
        valuation: DomainValuationResult | None,
    ) -> str:
        parts = [
            f"Sniped {result.domain_name} via {result.registrar}",
            f"result={result.result.value}",
        ]
        if valuation is not None:
            parts.append(f"estimated_value=${valuation.estimated_value_usd:,.2f}")
            parts.append(f"valuation_source={valuation.source}")
        return " | ".join(parts)

    def _post_discord(self, message: str) -> None:
        try:
            response = self._http.post(self._discord_webhook_url, json={"content": message})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("sniper: Discord webhook failed: %s", exc)

    def _post_telegram(self, message: str) -> None:
        try:
            response = self._http.post(
                f"https://api.telegram.org/bot{self._telegram_bot_token}/sendMessage",
                json={
                    "chat_id": self._telegram_chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("sniper: Telegram alert failed: %s", exc)


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    retry_after = headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


class _BaseHTTPRegistrarClient:
    def __init__(
        self,
        proxy_url: str | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._proxy_url = proxy_url or settings.registrar_proxy_url or None
        self._breaker = breaker or CircuitBreaker()
        self._using_proxy = False
        self._http = self._build_http_client()

    def _build_http_client(self, use_proxy: bool = False) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": 10}
        if use_proxy and self._proxy_url:
            kwargs["proxy"] = self._proxy_url
        return httpx.Client(**kwargs)

    def _switch_to_proxy(self) -> None:
        if not self._proxy_url or self._using_proxy:
            return
        logger.warning("sniper[%s]: switching to proxy after rate limit", self.name)
        self._http.close()
        self._http = self._build_http_client(use_proxy=True)
        self._using_proxy = True

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        while True:
            if not self._breaker.allow():
                wait_seconds = self._breaker.remaining_seconds()
                logger.warning(
                    "sniper[%s]: circuit open, waiting %.1fs",
                    self.name,
                    wait_seconds,
                )
                time.sleep(wait_seconds)

            response = self._http.request(method, url, **kwargs)
            if response.status_code == 429:
                retry_after = _retry_after_seconds(response.headers)
                wait_seconds = self._breaker.record_rate_limit(retry_after)
                if self._proxy_url and not self._using_proxy:
                    self._switch_to_proxy()
                    continue

                logger.warning(
                    "sniper[%s]: 429 received, backing off for %.1fs",
                    self.name,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            self._breaker.record_success()
            response.raise_for_status()
            return response

    def close(self) -> None:
        self._http.close()


# ─── Abstract registrar client ────────────────────────────────────────────────

class RegistrarClient(abc.ABC):
    """
    Common interface every registrar adapter must implement.

    Concrete subclasses handle authentication, request signing, and
    API-specific error parsing.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short identifier for this registrar, e.g. ``"dynadot"``."""

    @abc.abstractmethod
    def register(self, domain: str) -> RegistrationResult:
        """
        Attempt to register *domain*.

        Returns
        -------
        RegistrationResult
            Whether the registration succeeded, failed, or was already taken.

        Raises
        ------
        httpx.HTTPStatusError
            On unrecoverable HTTP errors (caller handles retry logic).
        """


# ─── Dynadot client ───────────────────────────────────────────────────────────

class DynadotClient(_BaseHTTPRegistrarClient):
    """
    Registrar client for `Dynadot <https://www.dynadot.com/domain/api2.html>`_.

    The Dynadot API is REST-based and uses an API key for authentication.
    Registration is submitted via a GET request to their ``register`` command.

    Usage::

        client = DynadotClient()
        result = client.register("example.com")
    """

    BASE_URL = "https://api.dynadot.com/api3.json"

    def __init__(
        self,
        api_key: str | None = None,
        proxy_url: str | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url, breaker=breaker)
        self._api_key = api_key or settings.dynadot_api_key

    @property
    def name(self) -> str:
        return "dynadot"

    def register(self, domain: str) -> RegistrationResult:
        logger.info("sniper[dynadot]: attempting to register %s", domain)
        response = self._request(
            "GET",
            self.BASE_URL,
            params={
                "key": self._api_key,
                "command": "register",
                "domain": domain,
                "duration": 1,
            },
        )
        data = response.json()

        # Dynadot returns {"RegisterResponse": {"ResponseCode": "0", ...}}
        register_response = data.get("RegisterResponse", {})
        code = str(register_response.get("ResponseCode", "-1"))

        if code == "0":
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.SUCCESS,
            )
        if code == "3":
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.ALREADY_REGISTERED,
                error_message=register_response.get("ResponseMsg"),
            )
        return RegistrationResult(
            domain_name=domain,
            registrar=self.name,
            result=SnipeResult.FAILURE,
            error_message=register_response.get("ResponseMsg"),
        )


# ─── Namejet client ───────────────────────────────────────────────────────────

class NamejetClient(_BaseHTTPRegistrarClient):
    """
    Registrar client for `Namejet <https://www.namejet.com/>`_.

    Namejet is a back-order platform; "registration" here means placing a
    back-order so their system attempts the drop-catch on our behalf.

    Usage::

        client = NamejetClient()
        result = client.register("example.com")
    """

    BASE_URL = "https://api.namejet.com/api"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        proxy_url: str | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url, breaker=breaker)
        self._api_key = api_key or settings.namejet_api_key
        self._api_secret = api_secret or settings.namejet_api_secret

    @property
    def name(self) -> str:
        return "namejet"

    def register(self, domain: str) -> RegistrationResult:
        logger.info("sniper[namejet]: placing back-order for %s", domain)
        response = self._request(
            "POST",
            f"{self.BASE_URL}/backorder",
            json={"domain": domain},
            headers={
                "X-Api-Key": self._api_key,
                "X-Api-Secret": self._api_secret,
            },
        )
        data = response.json()

        if data.get("success"):
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.SUCCESS,
            )
        return RegistrationResult(
            domain_name=domain,
            registrar=self.name,
            result=SnipeResult.FAILURE,
            error_message=data.get("message"),
        )


class MockRegistrarClient(RegistrarClient):
    """Local test registrar that never calls external APIs."""

    @property
    def name(self) -> str:
        return "mock"

    def register(self, domain: str) -> RegistrationResult:
        logger.info("sniper[mock]: simulated successful registration for %s", domain)
        return RegistrationResult(
            domain_name=domain,
            registrar=self.name,
            result=SnipeResult.SUCCESS,
        )


def _build_registrar_clients() -> list[RegistrarClient]:
    if settings.sniper_dry_run:
        return [MockRegistrarClient()]

    clients: list[RegistrarClient] = []
    if settings.dynadot_api_key.strip():
        clients.append(DynadotClient())
    if settings.namejet_api_key.strip() and settings.namejet_api_secret.strip():
        clients.append(NamejetClient())
    return clients


# ─── RQ job entry point ───────────────────────────────────────────────────────

def attempt_registration(domain_name: str) -> RegistrationResult:
    """
    Top-level function dispatched as an RQ job.

    Strategy
    --------
    1. Wait until *lead_time* seconds before the predicted drop.
    2. Poll every *poll_interval_ms* milliseconds.
    3. Try Dynadot first; fall back to Namejet if Dynadot fails.
    4. Skip domains valued below the configured minimum.
    5. Persist the result to the ``snipe_attempts`` table.
    """
    logger.info("sniper: starting watch for %s", domain_name)
    valuation_client = DomainValuationClient()
    notifier = NotificationClient()
    clients = _build_registrar_clients()

    try:
        valuation = valuation_client.evaluate(domain_name)
        logger.info(
            "sniper: valuation for %s is $%.2f from %s",
            domain_name,
            valuation.estimated_value_usd,
            valuation.source,
        )

        if valuation.estimated_value_usd < settings.valuation_min_usd:
            result = RegistrationResult(
                domain_name=domain_name,
                registrar="valuation",
                result=SnipeResult.SKIPPED_LOW_VALUE,
                error_message=(
                    f"Estimated value ${valuation.estimated_value_usd:.2f} is below "
                    f"the ${settings.valuation_min_usd:.2f} threshold"
                ),
            )
            _persist_result(result)
            return result

        if not clients:
            result = RegistrationResult(
                domain_name=domain_name,
                registrar="none",
                result=SnipeResult.FAILURE,
                error_message=(
                    "No registrar clients configured. Set SNIPER_DRY_RUN=true for local "
                    "testing or provide registrar API keys."
                ),
            )
            _persist_result(result)
            return result

        poll_interval = settings.sniper_poll_interval_ms / 1000.0

        result: RegistrationResult = RegistrationResult(
            domain_name=domain_name,
            registrar="none",
            result=SnipeResult.FAILURE,
            error_message="No registrar clients attempted",
        )

        for client in clients:
            result = _poll_and_register(client, domain_name, poll_interval)
            if result.result == SnipeResult.SUCCESS:
                _persist_result(result)
                notifier.notify_success(result, valuation)
                return result
            logger.warning(
                "sniper[%s]: failed for %s (%s), trying next registrar",
                client.name, domain_name, result.result,
            )

        # All registrars failed – record the last attempt
        _persist_result(result)
        return result
    finally:
        valuation_client.close()
        notifier.close()
        for client in clients:
            with contextlib.suppress(Exception):
                client.close()


def _poll_and_register(
    client: RegistrarClient,
    domain_name: str,
    poll_interval: float,
) -> RegistrationResult:
    """Poll until registration succeeds or the domain is already taken."""
    while True:
        result = client.register(domain_name)
        if result.result in (SnipeResult.SUCCESS, SnipeResult.ALREADY_REGISTERED):
            return result
        time.sleep(poll_interval)


def _persist_result(result: RegistrationResult) -> None:
    """Write a :class:`~app.database.SnipeAttempt` row to PostgreSQL."""
    # TODO: implement DB write via SQLAlchemy session
    logger.info(
        "sniper: persisting result for %s: %s", result.domain_name, result.result
    )
