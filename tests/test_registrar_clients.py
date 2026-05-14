"""
tests/test_registrar_clients.py

Unit tests for the GoDaddy and Namecheap registrar clients.
All tests use monkeypatching – no external HTTP calls are made.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

import app.sniper as sniper_module
from app.database import SnipeResult
from app.sniper import (
    GoDaddyClient,
    NamecheapClient,
    RegistrationResult,
    _build_registrar_clients,
)


# ─── GoDaddy ──────────────────────────────────────────────────────────────────

class TestGoDaddyClient:
    """GoDaddy client unit tests (no network calls)."""

    def test_name_is_godaddy(self):
        client = GoDaddyClient(api_key="k", api_secret="s")
        assert client.name == "godaddy"

    def test_auth_header_format(self):
        client = GoDaddyClient(api_key="mykey", api_secret="mysecret")
        header = client._auth_header()
        assert header == {"Authorization": "sso-key mykey:mysecret"}

    def test_check_available_returns_true_when_api_says_available(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"available": True, "domain": "example.com"}

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            GoDaddyClient,
            "_request",
            lambda self, *a, **kw: FakeResponse(),
        )
        client = GoDaddyClient(api_key="k", api_secret="s")
        assert client.check_available("example.com") is True

    def test_check_available_returns_false_when_api_says_unavailable(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"available": False, "domain": "taken.com"}

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            GoDaddyClient,
            "_request",
            lambda self, *a, **kw: FakeResponse(),
        )
        client = GoDaddyClient(api_key="k", api_secret="s")
        assert client.check_available("taken.com") is False

    def test_appraise_extracts_govalue(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"govalue": 1500.0, "domain": "brand.com"}

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            GoDaddyClient,
            "_request",
            lambda self, *a, **kw: FakeResponse(),
        )
        client = GoDaddyClient(api_key="k", api_secret="s")
        assert client.appraise("brand.com") == 1500.0

    def test_appraise_falls_back_to_zero_on_missing_key(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {}

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            GoDaddyClient,
            "_request",
            lambda self, *a, **kw: FakeResponse(),
        )
        client = GoDaddyClient(api_key="k", api_secret="s")
        assert client.appraise("unknown.com") == 0.0

    def test_register_returns_success_on_201(self, monkeypatch):
        class FakeResponse:
            status_code = 201
            text = ""

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            GoDaddyClient,
            "_request",
            lambda self, *a, **kw: FakeResponse(),
        )
        client = GoDaddyClient(api_key="k", api_secret="s")
        result = client.register("newdomain.com")
        assert result.result == SnipeResult.SUCCESS
        assert result.registrar == "godaddy"

    def test_register_returns_failure_on_exception(self, monkeypatch):
        def raise_exc(self, *a, **kw):
            raise RuntimeError("network error")

        monkeypatch.setattr(GoDaddyClient, "_request", raise_exc)
        client = GoDaddyClient(api_key="k", api_secret="s")
        result = client.register("fail.com")
        assert result.result == SnipeResult.FAILURE
        assert "network error" in (result.error_message or "")


# ─── Namecheap ────────────────────────────────────────────────────────────────

class TestNamecheapClient:
    """Namecheap client unit tests (no network calls)."""

    def test_name_is_namecheap(self):
        client = NamecheapClient(api_key="k", api_user="u", client_ip="1.2.3.4")
        assert client.name == "namecheap"

    def test_base_params_contain_required_fields(self):
        client = NamecheapClient(api_key="mykey", api_user="myuser", client_ip="1.2.3.4")
        params = client._base_params("namecheap.domains.check")
        assert params["ApiKey"] == "mykey"
        assert params["ApiUser"] == "myuser"
        assert params["ClientIp"] == "1.2.3.4"
        assert params["Command"] == "namecheap.domains.check"

    def _make_availability_xml(self, domain: str, available: bool) -> str:
        avail = "true" if available else "false"
        return (
            '<?xml version="1.0"?>'
            '<ApiResponse Status="OK" xmlns="http://api.namecheap.com/xml.response">'
            "<CommandResponse>"
            f'<DomainCheckResult Domain="{domain}" Available="{avail}" />'
            "</CommandResponse>"
            "</ApiResponse>"
        )

    def test_parse_availability_available(self):
        xml = self._make_availability_xml("example.com", True)
        assert NamecheapClient._parse_availability(xml, "example.com") is True

    def test_parse_availability_not_available(self):
        xml = self._make_availability_xml("taken.com", False)
        assert NamecheapClient._parse_availability(xml, "taken.com") is False

    def test_parse_success_ok(self):
        xml = '<?xml version="1.0"?><ApiResponse Status="OK" xmlns="http://api.namecheap.com/xml.response"></ApiResponse>'
        assert NamecheapClient._parse_success(xml) is True

    def test_parse_success_error(self):
        xml = '<?xml version="1.0"?><ApiResponse Status="ERROR" xmlns="http://api.namecheap.com/xml.response"></ApiResponse>'
        assert NamecheapClient._parse_success(xml) is False

    def test_parse_error_extracts_message(self):
        xml = (
            '<?xml version="1.0"?>'
            '<ApiResponse Status="ERROR" xmlns="http://api.namecheap.com/xml.response">'
            "<Errors>"
            "<Error Number=\"2030166\">Domain is not available</Error>"
            "</Errors>"
            "</ApiResponse>"
        )
        msg = NamecheapClient._parse_error(xml)
        assert msg == "Domain is not available"

    def test_check_available_calls_correct_command(self, monkeypatch):
        called_params = {}

        class FakeResponse:
            status_code = 200

            @property
            def text(self):
                return self._make_xml()

            def raise_for_status(self):
                pass

            def _make_xml(self):
                return (
                    '<?xml version="1.0"?>'
                    '<ApiResponse Status="OK" xmlns="http://api.namecheap.com/xml.response">'
                    "<CommandResponse>"
                    '<DomainCheckResult Domain="free.com" Available="true" />'
                    "</CommandResponse>"
                    "</ApiResponse>"
                )

        def fake_request(self, method, url, params=None, **kwargs):
            called_params.update(params or {})
            return FakeResponse()

        monkeypatch.setattr(NamecheapClient, "_request", fake_request)
        client = NamecheapClient(api_key="k", api_user="u", client_ip="1.2.3.4")
        result = client.check_available("free.com")
        assert result is True
        assert called_params.get("Command") == "namecheap.domains.check"

    def test_register_returns_success_on_ok_response(self, monkeypatch):
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_first_name", "John")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_last_name", "Doe")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_email", "john@example.com")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_phone", "+1.2125551234")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_address", "123 Main St")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_city", "New York")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_state", "NY")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_postal_code", "10001")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_country", "US")

        class FakeResponse:
            status_code = 200
            text = (
                '<?xml version="1.0"?>'
                '<ApiResponse Status="OK" xmlns="http://api.namecheap.com/xml.response">'
                "</ApiResponse>"
            )

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            NamecheapClient,
            "_request",
            lambda self, *a, **kw: FakeResponse(),
        )
        client = NamecheapClient(api_key="k", api_user="u", client_ip="1.2.3.4")
        result = client.register("newdomain.com")
        assert result.result == SnipeResult.SUCCESS
        assert result.registrar == "namecheap"

    def test_register_returns_failure_when_contact_info_missing(self, monkeypatch):
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_first_name", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_last_name", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_email", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_phone", "")

        client = NamecheapClient(api_key="k", api_user="u", client_ip="1.2.3.4")
        result = client.register("newdomain.com")
        assert result.result == SnipeResult.FAILURE
        assert "contact info" in (result.error_message or "").lower()

    def test_register_returns_failure_on_error_response(self, monkeypatch):
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_first_name", "John")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_last_name", "Doe")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_email", "john@example.com")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_phone", "+1.2125551234")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_address", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_city", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_state", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_postal_code", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_registrant_country", "US")

        class FakeResponse:
            status_code = 200
            text = (
                '<?xml version="1.0"?>'
                '<ApiResponse Status="ERROR" xmlns="http://api.namecheap.com/xml.response">'
                "<Errors>"
                "<Error Number=\"2030166\">Domain is not available</Error>"
                "</Errors>"
                "</ApiResponse>"
            )

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            NamecheapClient,
            "_request",
            lambda self, *a, **kw: FakeResponse(),
        )
        client = NamecheapClient(api_key="k", api_user="u", client_ip="1.2.3.4")
        result = client.register("taken.com")
        assert result.result == SnipeResult.FAILURE
        assert "not available" in (result.error_message or "").lower()


# ─── _build_registrar_clients with new registrars ────────────────────────────

class TestBuildRegistrarClientsExtended:
    """Verify GoDaddy and Namecheap are picked up when keys are configured."""

    def test_godaddy_included_when_both_keys_set(self, monkeypatch):
        monkeypatch.setattr(sniper_module.settings, "sniper_dry_run", False)
        monkeypatch.setattr(sniper_module.settings, "dynadot_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "namejet_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "namejet_api_secret", "")
        monkeypatch.setattr(sniper_module.settings, "godaddy_api_key", "gd-key")
        monkeypatch.setattr(sniper_module.settings, "godaddy_api_secret", "gd-secret")
        monkeypatch.setattr(sniper_module.settings, "namecheap_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_api_user", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_client_ip", "")

        clients = _build_registrar_clients()

        assert len(clients) == 1
        assert isinstance(clients[0], GoDaddyClient)

    def test_namecheap_included_when_all_credentials_set(self, monkeypatch):
        monkeypatch.setattr(sniper_module.settings, "sniper_dry_run", False)
        monkeypatch.setattr(sniper_module.settings, "dynadot_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "namejet_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "namejet_api_secret", "")
        monkeypatch.setattr(sniper_module.settings, "godaddy_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "godaddy_api_secret", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_api_key", "nc-key")
        monkeypatch.setattr(sniper_module.settings, "namecheap_api_user", "nc-user")
        monkeypatch.setattr(sniper_module.settings, "namecheap_client_ip", "1.2.3.4")

        clients = _build_registrar_clients()

        assert len(clients) == 1
        assert isinstance(clients[0], NamecheapClient)

    def test_all_four_registrars_included_when_all_keys_set(self, monkeypatch):
        monkeypatch.setattr(sniper_module.settings, "sniper_dry_run", False)
        monkeypatch.setattr(sniper_module.settings, "dynadot_api_key", "dy-key")
        monkeypatch.setattr(sniper_module.settings, "namejet_api_key", "nj-key")
        monkeypatch.setattr(sniper_module.settings, "namejet_api_secret", "nj-secret")
        monkeypatch.setattr(sniper_module.settings, "godaddy_api_key", "gd-key")
        monkeypatch.setattr(sniper_module.settings, "godaddy_api_secret", "gd-secret")
        monkeypatch.setattr(sniper_module.settings, "namecheap_api_key", "nc-key")
        monkeypatch.setattr(sniper_module.settings, "namecheap_api_user", "nc-user")
        monkeypatch.setattr(sniper_module.settings, "namecheap_client_ip", "1.2.3.4")

        clients = _build_registrar_clients()

        assert len(clients) == 4
        registrar_names = {c.name for c in clients}
        assert registrar_names == {"dynadot", "namejet", "godaddy", "namecheap"}

    def test_namecheap_excluded_when_client_ip_missing(self, monkeypatch):
        monkeypatch.setattr(sniper_module.settings, "sniper_dry_run", False)
        monkeypatch.setattr(sniper_module.settings, "dynadot_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "namejet_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "namejet_api_secret", "")
        monkeypatch.setattr(sniper_module.settings, "godaddy_api_key", "")
        monkeypatch.setattr(sniper_module.settings, "godaddy_api_secret", "")
        monkeypatch.setattr(sniper_module.settings, "namecheap_api_key", "nc-key")
        monkeypatch.setattr(sniper_module.settings, "namecheap_api_user", "nc-user")
        monkeypatch.setattr(sniper_module.settings, "namecheap_client_ip", "")

        clients = _build_registrar_clients()
        assert clients == []
