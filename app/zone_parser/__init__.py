"""
zone_parser – Discover expiring domains from ICANN Zone Files and/or WhoisXML.

Public interface
----------------
ZoneFileParser      Reads a gzipped ICANN zone file and yields domain names.
WhoisXMLClient      Thin wrapper around the WhoisXML Domain Research API.
DomainRecord        Plain dataclass returned by both sources.
"""

from __future__ import annotations

import gzip
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generator, Optional

import httpx

from app.config import settings


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class DomainRecord:
    """Minimal domain information returned by parsers."""
    name: str                              # e.g. "example.com"
    tld: str                               # e.g. "com"
    expires_at: Optional[datetime] = None
    registrar: Optional[str] = None
    raw: dict = field(default_factory=dict)


# ─── ICANN Zone File Parser ───────────────────────────────────────────────────

class ZoneFileParser:
    """
    Parses a gzipped ICANN zone file and yields domain names found in NS records.

    Zone files are available from ICANN's Centralized Zone Data Service (CZDS).
    Each line representing an NS record contains the second-level domain.

    Usage::

        parser = ZoneFileParser(tld="com")
        with open("com.zone.gz", "rb") as fh:
            for record in parser.parse(fh):
                print(record.name)
    """

    def __init__(self, tld: str) -> None:
        self.tld = tld.lstrip(".")

    def parse(self, file_obj: io.RawIOBase) -> Generator[DomainRecord, None, None]:
        """
        Yield one :class:`DomainRecord` per unique second-level domain found.

        Parameters
        ----------
        file_obj:
            A binary file-like object containing a (possibly gzipped) zone file.
        """
        seen: set[str] = set()
        opener = gzip.open if self._is_gzip(file_obj) else lambda f, m: f
        with opener(file_obj, "rb") as fh:
            for raw_line in fh:
                line = raw_line.decode("utf-8", errors="replace").strip()
                domain = self._extract_domain(line)
                if domain and domain not in seen:
                    seen.add(domain)
                    yield DomainRecord(name=domain, tld=self.tld)

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _is_gzip(file_obj: io.RawIOBase) -> bool:
        header = file_obj.read(2)
        file_obj.seek(0)
        return header == b"\x1f\x8b"

    def _extract_domain(self, line: str) -> Optional[str]:
        """Return the FQDN if the line is an NS record, else None."""
        if not line or line.startswith(";"):
            return None
        parts = line.split()
        # Zone file NS record: <name> [<ttl>] [<class>] NS <nameserver>
        if len(parts) >= 4 and "NS" in parts:
            raw_name = parts[0].rstrip(".")
            if raw_name and "." not in raw_name:
                # second-level domain relative to zone root
                return f"{raw_name}.{self.tld}"
            if raw_name.endswith(f".{self.tld}"):
                return raw_name
        return None


# ─── WhoisXML API Client ──────────────────────────────────────────────────────

class WhoisXMLClient:
    """
    Thin, synchronous wrapper around the WhoisXML Domain Research API.

    Relevant endpoints used
    -----------------------
    * WHOIS lookup  – retrieve expiry date for a single domain.
    * Registrant Search – find domains by expiry date range (requires plan).

    The client raises :class:`httpx.HTTPStatusError` on non-2xx responses and
    relies on :func:`tenacity.retry` at the call-site for back-off logic.

    Usage::

        client = WhoisXMLClient()
        record = client.lookup("example.com")
        print(record.expires_at)
    """

    BASE_URL = "https://www.whoisxmlapi.com/whoisserver/WhoisService"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.whoisxml_api_key
        self._http = httpx.Client(timeout=15)

    # ── Public interface ───────────────────────────────────────────────────────

    def lookup(self, domain: str) -> DomainRecord:
        """
        Fetch WHOIS data for *domain* and return a :class:`DomainRecord`.

        Raises
        ------
        httpx.HTTPStatusError
            On a non-2xx HTTP response.
        KeyError
            If the API response does not contain the expected fields.
        """
        payload = self._request(domain)
        return self._parse_response(domain, payload)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "WhoisXMLClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Private helpers ────────────────────────────────────────────────────────

    def _request(self, domain: str) -> dict:
        response = self._http.get(
            self.BASE_URL,
            params={
                "apiKey": self._api_key,
                "domainName": domain,
                "outputFormat": "JSON",
            },
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse_response(domain: str, payload: dict) -> DomainRecord:
        whois_record = payload.get("WhoisRecord", {})
        registry_data = whois_record.get("registryData", {})

        expires_raw = (
            registry_data.get("expiresDate")
            or whois_record.get("expiresDate")
        )
        expires_at: Optional[datetime] = None
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(
                    expires_raw.replace("Z", "+00:00")
                )
            except ValueError:
                expires_at = None

        tld = domain.rsplit(".", 1)[-1] if "." in domain else ""

        return DomainRecord(
            name=domain,
            tld=tld,
            expires_at=expires_at,
            registrar=registry_data.get("registrarName"),
            raw=payload,
        )
