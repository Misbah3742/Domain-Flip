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
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

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

class DynadotClient(RegistrarClient):
    """
    Registrar client for `Dynadot <https://www.dynadot.com/domain/api2.html>`_.

    The Dynadot API is REST-based and uses an API key for authentication.
    Registration is submitted via a GET request to their ``register`` command.

    Usage::

        client = DynadotClient()
        result = client.register("example.com")
    """

    BASE_URL = "https://api.dynadot.com/api3.json"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.dynadot_api_key
        self._http = httpx.Client(timeout=10)

    @property
    def name(self) -> str:
        return "dynadot"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
        reraise=True,
    )
    def register(self, domain: str) -> RegistrationResult:
        logger.info("sniper[dynadot]: attempting to register %s", domain)
        try:
            response = self._http.get(
                self.BASE_URL,
                params={
                    "key": self._api_key,
                    "command": "register",
                    "domain": domain,
                    "duration": 1,
                },
            )
            response.raise_for_status()
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
            elif code == "3":
                return RegistrationResult(
                    domain_name=domain,
                    registrar=self.name,
                    result=SnipeResult.ALREADY_REGISTERED,
                    error_message=register_response.get("ResponseMsg"),
                )
            else:
                return RegistrationResult(
                    domain_name=domain,
                    registrar=self.name,
                    result=SnipeResult.FAILURE,
                    error_message=register_response.get("ResponseMsg"),
                )

        except httpx.HTTPStatusError as exc:
            logger.warning("sniper[dynadot]: HTTP error for %s: %s", domain, exc)
            raise

    def close(self) -> None:
        self._http.close()


# ─── Namejet client ───────────────────────────────────────────────────────────

class NamejetClient(RegistrarClient):
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
    ) -> None:
        self._api_key = api_key or settings.namejet_api_key
        self._api_secret = api_secret or settings.namejet_api_secret
        self._http = httpx.Client(timeout=10)

    @property
    def name(self) -> str:
        return "namejet"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
        reraise=True,
    )
    def register(self, domain: str) -> RegistrationResult:
        logger.info("sniper[namejet]: placing back-order for %s", domain)
        try:
            response = self._http.post(
                f"{self.BASE_URL}/backorder",
                json={"domain": domain},
                headers={
                    "X-Api-Key": self._api_key,
                    "X-Api-Secret": self._api_secret,
                },
            )
            response.raise_for_status()
            data = response.json()

            if data.get("success"):
                return RegistrationResult(
                    domain_name=domain,
                    registrar=self.name,
                    result=SnipeResult.SUCCESS,
                )
            else:
                return RegistrationResult(
                    domain_name=domain,
                    registrar=self.name,
                    result=SnipeResult.FAILURE,
                    error_message=data.get("message"),
                )

        except httpx.HTTPStatusError as exc:
            logger.warning("sniper[namejet]: HTTP error for %s: %s", domain, exc)
            raise

    def close(self) -> None:
        self._http.close()


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
    4. Persist the result to the ``snipe_attempts`` table.
    """
    logger.info("sniper: starting watch for %s", domain_name)

    clients = _build_registrar_clients()

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
            return result
        logger.warning(
            "sniper[%s]: failed for %s (%s), trying next registrar",
            client.name, domain_name, result.result,
        )

    # All registrars failed – record the last attempt
    _persist_result(result)
    return result


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
