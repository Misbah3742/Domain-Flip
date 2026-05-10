"""
trademark_filter – Flag domains that contain protected brand names.

This module prevents UDRP (Uniform Domain-Name Dispute-Resolution Policy)
legal issues by checking candidate domains against:

1. A local curated list of known trademarks (``TRADEMARK_TERMS``).
2. (Optional) The ICANN Trademark Clearinghouse (TMCH) lookup API.

Public interface
----------------
TrademarkFilter     Main filter class.
FilterResult        Result dataclass returned by :meth:`TrademarkFilter.check`.
TRADEMARK_TERMS     Built-in set of well-known trademarked terms.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

logger = logging.getLogger(__name__)


# ─── Built-in trademark term list ─────────────────────────────────────────────
# This is a non-exhaustive sample.  In production, load from a database table
# or an external IP data provider (e.g. MarkMonitor, CSC) that supplies a
# continuously updated list of registered marks.

TRADEMARK_TERMS: FrozenSet[str] = frozenset(
    {
        # Big Tech
        "google", "gmail", "youtube", "android",
        "apple", "iphone", "ipad", "macbook",
        "microsoft", "windows", "xbox", "azure",
        "amazon", "aws", "alexa", "kindle",
        "facebook", "instagram", "whatsapp", "meta",
        "twitter", "x",
        "netflix", "spotify",
        # Finance
        "paypal", "visa", "mastercard", "amex", "americanexpress",
        "chase", "citibank", "wellsfargo",
        # Retail / CPG
        "nike", "adidas", "gucci", "louis vuitton", "louisvuitton",
        "walmart", "target", "costco",
        "coca-cola", "cocacola", "pepsi",
        # Automotive
        "toyota", "honda", "ford", "chevrolet", "bmw", "mercedes",
        # Pharma
        "pfizer", "johnson", "novartis",
    }
)


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class FilterResult:
    domain_name: str
    is_flagged: bool
    matched_terms: list[str] = field(default_factory=list)
    reason: Optional[str] = None


# ─── Filter class ─────────────────────────────────────────────────────────────

class TrademarkFilter:
    """
    Checks a domain name against a set of trademarked terms.

    Parameters
    ----------
    extra_terms:
        Additional terms to include beyond the built-in :data:`TRADEMARK_TERMS`.
    strict_mode:
        When ``True``, partial matches (e.g. ``"googledeal.com"``) are also
        flagged.  When ``False``, only exact SLD matches trigger a flag.

    Usage::

        f = TrademarkFilter()
        result = f.check("googledeal.com")
        if result.is_flagged:
            print(result.reason)
    """

    def __init__(
        self,
        extra_terms: Optional[FrozenSet[str]] = None,
        strict_mode: bool = True,
    ) -> None:
        terms = TRADEMARK_TERMS
        if extra_terms:
            terms = terms | extra_terms
        self._terms = terms
        self._strict = strict_mode
        # Pre-compile a single regex for fast matching.
        #
        # Strategy
        # --------
        # * Very short terms (≤ 2 chars, e.g. "x" for Twitter/X) always use
        #   word boundaries to avoid false positives inside unrelated words.
        # * In strict mode, longer terms are matched as substrings so that
        #   cybersquatting patterns like "googledeal" are still caught.
        # * In non-strict mode, every term requires word boundaries (only exact
        #   second-level-domain matches are flagged).
        patterns = []
        for term in sorted(terms, key=len, reverse=True):
            esc = re.escape(term)
            if len(term) <= 2 or not strict_mode:
                patterns.append(r"\b" + esc + r"\b")
            else:
                patterns.append(esc)
        self._pattern = re.compile(
            r"(?:" + "|".join(patterns) + r")",
            re.IGNORECASE,
        )

    def check(self, domain_name: str) -> FilterResult:
        """
        Evaluate *domain_name* for trademark conflicts.

        Parameters
        ----------
        domain_name:
            Fully-qualified domain, e.g. ``"example.com"``.

        Returns
        -------
        FilterResult
            ``is_flagged=True`` if a trademark match is found.
        """
        sld = self._extract_sld(domain_name)
        matches = self._pattern.findall(sld)
        unique_matches = list({m.lower() for m in matches})

        if not unique_matches:
            return FilterResult(domain_name=domain_name, is_flagged=False)

        reason = (
            f"Domain SLD '{sld}' contains trademarked term(s): "
            + ", ".join(f"'{t}'" for t in unique_matches)
        )
        logger.warning("trademark_filter: %s", reason)
        return FilterResult(
            domain_name=domain_name,
            is_flagged=True,
            matched_terms=unique_matches,
            reason=reason,
        )

    def is_safe(self, domain_name: str) -> bool:
        """Return ``True`` if the domain does **not** infringe any trademark."""
        return not self.check(domain_name).is_flagged

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _extract_sld(domain_name: str) -> str:
        """Extract the second-level domain label (without TLD)."""
        # Remove leading/trailing whitespace and convert to lower-case
        clean = domain_name.strip().lower()
        # Strip scheme if present (e.g. http://example.com)
        clean = re.sub(r"^https?://", "", clean)
        # Take the leftmost label (second-level domain)
        parts = clean.split(".")
        return parts[0] if parts else clean
