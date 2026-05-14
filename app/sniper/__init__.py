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
import xml.etree.ElementTree as ET
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


# ─── GoDaddy client ───────────────────────────────────────────────────────────

class GoDaddyClient(_BaseHTTPRegistrarClient):
    """
    Registrar client for `GoDaddy <https://developer.godaddy.com/>`_.

    Provides domain availability checks, real-time appraisal, and domain
    purchase via the GoDaddy REST API (v1).

    Authentication uses a ``sso-key {apiKey}:{apiSecret}`` header.

    Usage::

        client = GoDaddyClient()
        print(client.check_available("example.com"))
        print(client.appraise("example.com"))
        result = client.register("example.com")
    """

    BASE_URL = "https://api.godaddy.com/v1"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        proxy_url: str | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url, breaker=breaker)
        self._api_key = api_key or settings.godaddy_api_key
        self._api_secret = api_secret or settings.godaddy_api_secret

    @property
    def name(self) -> str:
        return "godaddy"

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"sso-key {self._api_key}:{self._api_secret}"}

    def check_available(self, domain: str) -> bool:
        """
        Return ``True`` if *domain* is available for registration.

        Raises
        ------
        httpx.HTTPStatusError
            On a non-2xx HTTP response.
        """
        logger.info("sniper[godaddy]: checking availability of %s", domain)
        response = self._request(
            "GET",
            f"{self.BASE_URL}/domains/available",
            params={"domain": domain},
            headers=self._auth_header(),
        )
        data = response.json()
        return bool(data.get("available", False))

    def appraise(self, domain: str) -> float:
        """
        Return GoDaddy's estimated value for *domain* in USD.

        Falls back to ``0.0`` if the API response cannot be parsed.

        Raises
        ------
        httpx.HTTPStatusError
            On a non-2xx HTTP response.
        """
        logger.info("sniper[godaddy]: appraising %s", domain)
        response = self._request(
            "GET",
            f"{self.BASE_URL}/appraisal/{domain}",
            headers=self._auth_header(),
        )
        data = response.json()
        try:
            return float(data.get("govalue", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def register(self, domain: str) -> RegistrationResult:
        """
        Purchase *domain* via the GoDaddy Domain Purchase API.

        The request uses a 1-year registration with default privacy settings.
        ``agreedAt`` is set to the current UTC timestamp in ISO-8601 format and
        ``agreedBy`` is populated from the API key so GoDaddy can identify the
        consenting party.  Both fields are required by the GoDaddy API.
        """
        agreed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        logger.info("sniper[godaddy]: purchasing %s", domain)
        try:
            response = self._request(
                "POST",
                f"{self.BASE_URL}/domains/purchase",
                json={
                    "domain": domain,
                    "period": 1,
                    "renewAuto": False,
                    "privacy": False,
                    "consent": {
                        "agreedAt": agreed_at,
                        "agreedBy": self._api_key,
                        "agreementKeys": ["DNRA"],
                    },
                },
                headers={**self._auth_header(), "Content-Type": "application/json"},
            )
            if response.status_code in (200, 201):
                return RegistrationResult(
                    domain_name=domain,
                    registrar=self.name,
                    result=SnipeResult.SUCCESS,
                )
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.FAILURE,
                error_message=response.text,
            )
        except Exception as exc:
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.FAILURE,
                error_message=str(exc),
            )


# ─── Namecheap client ─────────────────────────────────────────────────────────

class NamecheapClient(_BaseHTTPRegistrarClient):
    """
    Registrar client for `Namecheap <https://www.namecheap.com/support/api/>`_.

    Supports domain availability checking and registration via the Namecheap
    XML API.  All requests require the caller's IP to be whitelisted in the
    Namecheap control panel.

    Usage::

        client = NamecheapClient()
        print(client.check_available("example.com"))
        result = client.register("example.com")
    """

    BASE_URL = "https://api.namecheap.com/xml.response"

    def __init__(
        self,
        api_key: str | None = None,
        api_user: str | None = None,
        client_ip: str | None = None,
        proxy_url: str | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url, breaker=breaker)
        self._api_key = api_key or settings.namecheap_api_key
        self._api_user = api_user or settings.namecheap_api_user
        self._client_ip = client_ip or settings.namecheap_client_ip

    @property
    def name(self) -> str:
        return "namecheap"

    def _base_params(self, command: str) -> dict[str, str]:
        return {
            "ApiUser": self._api_user,
            "ApiKey": self._api_key,
            "UserName": self._api_user,
            "ClientIp": self._client_ip,
            "Command": command,
        }

    def check_available(self, domain: str) -> bool:
        """
        Return ``True`` if *domain* is available for registration.

        Raises
        ------
        httpx.HTTPStatusError
            On a non-2xx HTTP response.
        ValueError
            If the API response XML cannot be parsed.
        """
        logger.info("sniper[namecheap]: checking availability of %s", domain)
        params = {**self._base_params("namecheap.domains.check"), "DomainList": domain}
        response = self._request("GET", self.BASE_URL, params=params)
        return self._parse_availability(response.text, domain)

    def register(self, domain: str) -> RegistrationResult:
        """
        Register *domain* via the Namecheap ``domains.create`` command.

        Registrant contact fields are read from the ``NAMECHEAP_REGISTRANT_*``
        environment variables (via :class:`~app.config.Settings`).  If required
        fields (first name, last name, email) are not set, registration is
        aborted and a descriptive error is returned rather than submitting an
        invalid request to the API.
        """
        first = settings.namecheap_registrant_first_name
        last = settings.namecheap_registrant_last_name
        email = settings.namecheap_registrant_email
        phone = settings.namecheap_registrant_phone
        address = settings.namecheap_registrant_address
        city = settings.namecheap_registrant_city
        state = settings.namecheap_registrant_state
        postal = settings.namecheap_registrant_postal_code
        country = settings.namecheap_registrant_country or "US"

        missing = [
            name for name, val in (
                ("NAMECHEAP_REGISTRANT_FIRST_NAME", first),
                ("NAMECHEAP_REGISTRANT_LAST_NAME", last),
                ("NAMECHEAP_REGISTRANT_EMAIL", email),
                ("NAMECHEAP_REGISTRANT_PHONE", phone),
            )
            if not (val or "").strip()
        ]
        if missing:
            msg = "Namecheap registration requires contact info. Missing: " + ", ".join(missing)
            logger.error("sniper[namecheap]: %s", msg)
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.FAILURE,
                error_message=msg,
            )

        logger.info("sniper[namecheap]: registering %s", domain)
        # Use rsplit to correctly handle multi-label domains: "sub.example.com" →
        # sld="sub.example", tld="com".  Namecheap expects the full domain name
        # minus the last label as DomainName, and the last label as TLD.
        parts = domain.rsplit(".", 1)
        if len(parts) != 2:
            msg = f"Namecheap requires a domain with a TLD (e.g., 'example.com'), got {domain!r}"
            logger.error("sniper[namecheap]: %s", msg)
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.FAILURE,
                error_message=msg,
            )
        sld = parts[0]
        tld = parts[1]

        # Build the same contact block for all four roles required by Namecheap
        contact = {
            "FirstName": first,
            "LastName": last,
            "Address1": address,
            "City": city,
            "StateProvince": state,
            "PostalCode": postal,
            "Country": country,
            "Phone": phone,
            "EmailAddress": email,
        }
        params = {
            **self._base_params("namecheap.domains.create"),
            "DomainName": sld,
            "TLD": tld,
            "Years": "1",
            **{f"Registrant{k}": v for k, v in contact.items()},
            **{f"Tech{k}": v for k, v in contact.items()},
            **{f"Admin{k}": v for k, v in contact.items()},
            **{f"AuxBilling{k}": v for k, v in contact.items()},
        }
        try:
            response = self._request("GET", self.BASE_URL, params=params)
            if self._parse_success(response.text):
                return RegistrationResult(
                    domain_name=domain,
                    registrar=self.name,
                    result=SnipeResult.SUCCESS,
                )
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.FAILURE,
                error_message=self._parse_error(response.text),
            )
        except Exception as exc:
            return RegistrationResult(
                domain_name=domain,
                registrar=self.name,
                result=SnipeResult.FAILURE,
                error_message=str(exc),
            )

    # ── Private XML helpers ────────────────────────────────────────────────────

    @staticmethod
    def _parse_availability(xml_text: str, domain: str) -> bool:
        """Return True if the Namecheap XML response shows the domain is available."""
        root = ET.fromstring(xml_text)
        ns = {"nc": "http://api.namecheap.com/xml.response"}
        # Extract the full domain to search for an exact match in the XML response
        domain_lower = domain.lower()
        for check in root.findall(".//nc:DomainCheckResult", ns):
            name = check.get("Domain", "").lower()
            if name == domain_lower:
                return check.get("Available", "false").lower() == "true"
        return False

    @staticmethod
    def _parse_success(xml_text: str) -> bool:
        """Return True if the Namecheap API returned a successful status."""
        root = ET.fromstring(xml_text)
        return root.get("Status", "").upper() == "OK"

    @staticmethod
    def _parse_error(xml_text: str) -> str | None:
        """Extract the first error message from a Namecheap XML response."""
        root = ET.fromstring(xml_text)
        ns = {"nc": "http://api.namecheap.com/xml.response"}
        error = root.find(".//nc:Error", ns)
        if error is not None and error.text:
            return error.text.strip()
        return root.get("Status")


def _build_registrar_clients() -> list[RegistrarClient]:
    if settings.sniper_dry_run:
        return [MockRegistrarClient()]

    clients: list[RegistrarClient] = []
    if settings.dynadot_api_key.strip():
        clients.append(DynadotClient())
    if settings.namejet_api_key.strip() and settings.namejet_api_secret.strip():
        clients.append(NamejetClient())
    if settings.godaddy_api_key.strip() and settings.godaddy_api_secret.strip():
        clients.append(GoDaddyClient())
    if (
        settings.namecheap_api_key.strip()
        and settings.namecheap_api_user.strip()
        and settings.namecheap_client_ip.strip()
    ):
        clients.append(NamecheapClient())
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
