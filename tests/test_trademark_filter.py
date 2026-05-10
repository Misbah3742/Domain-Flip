"""
tests/test_trademark_filter.py

Unit tests for the trademark_filter module.
These tests run entirely in-process with no external dependencies.
"""

import pytest

from app.trademark_filter import FilterResult, TrademarkFilter


class TestTrademarkFilterBasic:
    """Basic trademark detection behaviour."""

    def setup_method(self):
        self.f = TrademarkFilter(strict_mode=True)

    def test_clean_domain_not_flagged(self):
        result = self.f.check("example.com")
        assert not result.is_flagged
        assert result.matched_terms == []

    def test_exact_trademark_match_flagged(self):
        result = self.f.check("google.com")
        assert result.is_flagged
        assert "google" in result.matched_terms

    def test_partial_trademark_match_flagged_in_strict_mode(self):
        result = self.f.check("googledeal.com")
        assert result.is_flagged

    def test_case_insensitive(self):
        result = self.f.check("APPLE.com")
        assert result.is_flagged
        assert "apple" in result.matched_terms

    def test_multiple_trademarks_flagged(self):
        result = self.f.check("googlepaypal.com")
        assert result.is_flagged
        assert len(result.matched_terms) >= 2

    def test_result_contains_reason(self):
        result = self.f.check("microsoft.io")
        assert result.is_flagged
        assert result.reason is not None
        assert "microsoft" in result.reason.lower()


class TestTrademarkFilterExtraTerms:
    """Custom extra terms are respected."""

    def test_extra_term_flagged(self):
        f = TrademarkFilter(extra_terms=frozenset({"acmecorp"}))
        result = f.check("acmecorp.com")
        assert result.is_flagged
        assert "acmecorp" in result.matched_terms

    def test_extra_term_does_not_affect_clean_domain(self):
        f = TrademarkFilter(extra_terms=frozenset({"acmecorp"}))
        result = f.check("example.com")
        assert not result.is_flagged


class TestIsSafeHelper:
    """TrademarkFilter.is_safe() returns bool correctly."""

    def setup_method(self):
        self.f = TrademarkFilter()

    def test_safe_domain(self):
        assert self.f.is_safe("mybusiness.com") is True

    def test_unsafe_domain(self):
        assert self.f.is_safe("netflix.co") is False


class TestFilterResultDataclass:
    """FilterResult dataclass behaves as expected."""

    def test_unflagged_result(self):
        r = FilterResult(domain_name="clean.com", is_flagged=False)
        assert r.matched_terms == []
        assert r.reason is None

    def test_flagged_result(self):
        r = FilterResult(
            domain_name="fake-google.com",
            is_flagged=True,
            matched_terms=["google"],
            reason="Matches 'google'",
        )
        assert r.is_flagged
        assert "google" in r.matched_terms
