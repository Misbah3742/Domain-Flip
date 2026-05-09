"""
tests/test_zone_parser.py

Unit tests for the zone_parser module.
Uses in-memory byte streams – no file I/O, no network calls.
"""

import gzip
import io
from datetime import datetime, timezone

import pytest

from app.zone_parser import DomainRecord, WhoisXMLClient, ZoneFileParser


# ─── ZoneFileParser ───────────────────────────────────────────────────────────

def _make_zone_bytes(lines: list[str], compressed: bool = False) -> io.BytesIO:
    """Helper: build a BytesIO zone file from a list of text lines."""
    content = "\n".join(lines).encode()
    if compressed:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(content)
        buf.seek(0)
        return buf
    return io.BytesIO(content)


class TestZoneFileParserPlainText:
    """Parse a plain-text (uncompressed) zone file."""

    def setup_method(self):
        self.parser = ZoneFileParser(tld="com")

    def test_single_ns_record_yields_domain(self):
        zone = _make_zone_bytes(["example 3600 IN NS ns1.dns.com."])
        records = list(self.parser.parse(zone))
        assert len(records) == 1
        assert records[0].name == "example.com"
        assert records[0].tld == "com"

    def test_comments_ignored(self):
        zone = _make_zone_bytes([
            "; this is a comment",
            "example 3600 IN NS ns1.dns.com.",
        ])
        records = list(self.parser.parse(zone))
        assert len(records) == 1

    def test_duplicate_domains_deduplicated(self):
        zone = _make_zone_bytes([
            "example 3600 IN NS ns1.dns.com.",
            "example 3600 IN NS ns2.dns.com.",
        ])
        records = list(self.parser.parse(zone))
        assert len(records) == 1

    def test_multiple_unique_domains(self):
        zone = _make_zone_bytes([
            "alpha 3600 IN NS ns1.dns.com.",
            "beta  3600 IN NS ns1.dns.com.",
            "gamma 3600 IN NS ns1.dns.com.",
        ])
        names = [r.name for r in self.parser.parse(zone)]
        assert set(names) == {"alpha.com", "beta.com", "gamma.com"}

    def test_empty_file_yields_nothing(self):
        zone = _make_zone_bytes([])
        records = list(self.parser.parse(zone))
        assert records == []


class TestZoneFileParserGzip:
    """Parse a gzip-compressed zone file."""

    def test_gzip_file_parsed_correctly(self):
        parser = ZoneFileParser(tld="net")
        zone = _make_zone_bytes(["mysite 3600 IN NS ns.example.com."], compressed=True)
        records = list(parser.parse(zone))
        assert len(records) == 1
        assert records[0].name == "mysite.net"


# ─── WhoisXMLClient._parse_response ──────────────────────────────────────────

class TestWhoisXMLClientParseResponse:
    """Test the static response parser without making HTTP calls."""

    def _make_payload(
        self,
        expires: str | None = None,
        registrar: str | None = None,
    ) -> dict:
        return {
            "WhoisRecord": {
                "registryData": {
                    "expiresDate": expires,
                    "registrarName": registrar,
                }
            }
        }

    def test_expiry_date_parsed(self):
        payload = self._make_payload(expires="2025-12-31T00:00:00Z")
        record = WhoisXMLClient._parse_response("example.com", payload)
        assert record.expires_at is not None
        assert record.expires_at.year == 2025
        assert record.expires_at.month == 12

    def test_missing_expiry_is_none(self):
        payload = self._make_payload(expires=None)
        record = WhoisXMLClient._parse_response("example.com", payload)
        assert record.expires_at is None

    def test_registrar_populated(self):
        payload = self._make_payload(registrar="Example Registrar Inc.")
        record = WhoisXMLClient._parse_response("example.com", payload)
        assert record.registrar == "Example Registrar Inc."

    def test_tld_extracted(self):
        payload = self._make_payload()
        record = WhoisXMLClient._parse_response("example.org", payload)
        assert record.tld == "org"

    def test_domain_name_preserved(self):
        payload = self._make_payload()
        record = WhoisXMLClient._parse_response("hello.io", payload)
        assert record.name == "hello.io"
