"""Source reliability grading.

The grader is intentionally conservative and fully offline. A local heuristic
infers reliability from the source identity (publisher domain / named authority)
and credibility from the analyst's summary text; credibility stays "cannot be
judged" when there is no readable claim content. Analysts can always override the
grade manually.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import HTTPException, status

from ..models import (
    Source,
    SourceCredibility,
    SourceGradingOrigin,
    SourceReliability,
)


@dataclass
class GradeResult:
    reliability: SourceReliability
    credibility: SourceCredibility
    engine: str
    rationale: str
    error: str = ""


@dataclass
class AutoGradeOutcome:
    source: Source
    applied: bool
    reason: str = ""


_OFFICIAL_DOMAINS = {
    "cisa.gov",
    "nvd.nist.gov",
    "nist.gov",
    "cve.org",
    "ncsc.gov.uk",
    "cert.europa.eu",
    "enisa.europa.eu",
    "cyber.gov.au",
    "cyber.gc.ca",
    "cert.govt.nz",
    "bsi.bund.de",
    "ssi.gouv.fr",
    "ncsc.nl",
}

_VENDOR_DOMAINS = {
    "microsoft.com",
    "msrc.microsoft.com",
    "google.com",
    "cloud.google.com",
    "mandiant.com",
    "crowdstrike.com",
    "paloaltonetworks.com",
    "unit42.paloaltonetworks.com",
    "talosintelligence.com",
    "secureworks.com",
    "recordedfuture.com",
    "proofpoint.com",
    "sentinelone.com",
    "trendmicro.com",
    "welivesecurity.com",
    "kaspersky.com",
    "sophos.com",
    "fortinet.com",
}

_NEWS_DOMAINS = {
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "theguardian.com",
    "wired.com",
    "theregister.com",
    "darkreading.com",
    "bleepingcomputer.com",
    "cyberscoop.com",
}

_SOCIAL_OR_PASTE_DOMAINS = {
    "x.com",
    "twitter.com",
    "reddit.com",
    "pastebin.com",
    "gist.github.com",
    "github.com",
    "t.me",
    "telegram.me",
    "discord.com",
    "medium.com",
    "substack.com",
}

_CONFIRMED_RE = re.compile(
    r"\b(confirmed|advisory|bulletin|observed|telemetry|patch|cve-\d{4}-\d+|"
    r"exploited in the wild|indicators? of compromise|ioc)\b",
    re.IGNORECASE,
)
_WEAK_RE = re.compile(
    r"\b(alleged|unverified|rumou?r|claim(?:ed)?|could|might|possibly|"
    r"speculat(?:e|ion|ive)|reportedly)\b",
    re.IGNORECASE,
)
_FALSE_RE = re.compile(r"\b(false|hoax|debunked|improbable)\b", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _host_matches(host: str, domain: str) -> bool:
    host = host.lower().strip(".")
    domain = domain.lower().strip(".")
    return host == domain or host.endswith(f".{domain}")


def _domain_in(host: str, domains: set[str]) -> bool:
    return any(_host_matches(host, domain) for domain in domains)


def _is_official_host(host: str) -> bool:
    host = host.lower().strip(".")
    return (
        _domain_in(host, _OFFICIAL_DOMAINS)
        or host.endswith(".gov")
        or host.endswith(".mil")
        or ".gov." in host
        or host.endswith(".gov.au")
        or host.endswith(".gov.uk")
    )


def _source_category(source: Source) -> tuple[str, str]:
    reference = (source.reference or "").strip()
    parsed = urlparse(reference)
    host = (parsed.hostname or "").lower().strip(".")
    haystack = " ".join([source.title, source.reference, source.summary]).lower()
    if host and _is_official_host(host):
        return "official", host
    if host and _domain_in(host, _VENDOR_DOMAINS):
        return "vendor", host
    if host and (host.endswith(".edu") or host.endswith(".ac.uk")):
        return "academic", host
    if host and _domain_in(host, _NEWS_DOMAINS):
        return "news", host
    if host and _domain_in(host, _SOCIAL_OR_PASTE_DOMAINS):
        return "social", host
    if any(token in haystack for token in ("cisa", "ncsc", "cert", "nvd", "cve")):
        return "official", host or "named authority"
    if host:
        return "unknown_web", host
    return "unknown", ""


def _credibility_from_text(category: str, text: str, has_readable_content: bool) -> SourceCredibility:
    if not has_readable_content:
        return SourceCredibility.CANNOT_BE_JUDGED
    if _FALSE_RE.search(text):
        return SourceCredibility.IMPROBABLE
    if _WEAK_RE.search(text):
        return SourceCredibility.DOUBTFULLY_TRUE
    if category in {"official", "vendor"} and _CONFIRMED_RE.search(text):
        return SourceCredibility.CONFIRMED
    if category in {"official", "vendor"}:
        return SourceCredibility.PROBABLY_TRUE
    if category == "social":
        return SourceCredibility.DOUBTFULLY_TRUE
    return SourceCredibility.POSSIBLY_TRUE


def heuristic_grade(source: Source) -> GradeResult | None:
    category, host = _source_category(source)
    text = " ".join(part for part in (source.title, source.summary, source.content_md) if part)
    has_readable_content = bool(source.summary.strip() or source.content_md.strip())

    reliability_by_category = {
        "official": SourceReliability.B,
        "vendor": SourceReliability.B,
        "academic": SourceReliability.B,
        "news": SourceReliability.C,
        "unknown_web": SourceReliability.C,
        "social": SourceReliability.D,
    }
    reliability = reliability_by_category.get(category)
    if not reliability:
        return None

    credibility = _credibility_from_text(category, text, has_readable_content)
    detail = "analyst-provided content" if has_readable_content else "source identity only"
    subject = host or "the source reference"
    rationale = (
        f"Recognized {subject} as {category.replace('_', ' ')}; "
        f"graded from {detail}."
    )
    if credibility == SourceCredibility.CANNOT_BE_JUDGED:
        rationale += " Credibility cannot be judged without readable claim content."
    return GradeResult(
        reliability=reliability,
        credibility=credibility,
        engine="heuristic:v1",
        rationale=rationale,
    )


def _apply_grade(
    source: Source,
    *,
    reliability: SourceReliability | None,
    credibility: SourceCredibility | None,
    origin: SourceGradingOrigin,
    engine: str = "",
    rationale: str = "",
    error: str = "",
) -> Source:
    source.reliability = reliability
    source.credibility = credibility
    source.grading_origin = origin
    source.grading_engine = engine
    source.grading_rationale = rationale.strip()
    source.grading_error = error.strip()
    source.graded_at = _now() if reliability and credibility else None
    return source


def set_manual_grade(
    source: Source,
    *,
    reliability: SourceReliability | None,
    credibility: SourceCredibility | None,
    rationale: str = "",
) -> Source:
    if bool(reliability) != bool(credibility):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Reliability and credibility must be set together",
        )
    if not reliability and not credibility:
        return _apply_grade(
            source,
            reliability=None,
            credibility=None,
            origin=SourceGradingOrigin.UNGRADED,
        )
    return _apply_grade(
        source,
        reliability=reliability,
        credibility=credibility,
        origin=SourceGradingOrigin.MANUAL,
        engine="manual",
        rationale=rationale or "Manually graded by analyst.",
    )


def auto_grade(source: Source) -> AutoGradeOutcome:
    """Grade a source with the offline heuristic (no network, no LLM)."""
    result = heuristic_grade(source)
    if not result:
        _apply_grade(
            source,
            reliability=None,
            credibility=None,
            origin=SourceGradingOrigin.UNGRADED,
        )
        return AutoGradeOutcome(source=source, applied=False, reason="No grade available")

    _apply_grade(
        source,
        reliability=result.reliability,
        credibility=result.credibility,
        origin=SourceGradingOrigin.AUTO,
        engine=result.engine,
        rationale=result.rationale,
        error=result.error,
    )
    return AutoGradeOutcome(source=source, applied=True, reason=result.error)


def regrade_source(source: Source) -> AutoGradeOutcome:
    return auto_grade(source)
