"""Source reliability grading.

The grader is intentionally conservative. When a configured LLM provider can
read source content it may assess both reliability and credibility; when content
cannot be fetched, the local heuristic only grades what it can infer from the
source identity and leaves credibility as "cannot be judged".
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

import httpx2 as httpx
from fastapi import HTTPException, status
from sqlmodel import Session

from ..config import get_settings
from ..models import (
    Source,
    SourceCredibility,
    SourceGradingOrigin,
    SourceReliability,
)


class SourceFetchError(RuntimeError):
    """Non-fatal fetch failure; callers fall back to heuristic grading."""


@dataclass
class FetchedSource:
    final_url: str
    title: str
    text: str


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


_GRADE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reliability": {
            "type": "string",
            "enum": ["A", "B", "C", "D", "E", "F"],
            "description": "Admiralty/NATO source reliability rating.",
        },
        "credibility": {
            "type": "string",
            "enum": ["1", "2", "3", "4", "5", "6"],
            "description": "Admiralty/NATO information credibility rating.",
        },
        "rationale": {
            "type": "string",
            "description": "Short explanation grounded only in supplied source data.",
        },
    },
    "required": ["reliability", "credibility", "rationale"],
}

_SYSTEM_PROMPT = """You grade cyber threat intelligence sources using the Admiralty/NATO system.

Reliability grades the source/publisher:
A completely reliable; B usually reliable; C fairly reliable; D not usually reliable; E unreliable; F cannot be judged.

Credibility grades the information/claim:
1 confirmed; 2 probably true; 3 possibly true; 4 doubtfully true; 5 improbable; 6 cannot be judged.

Use only the source data provided by Iceberg. If page content is unavailable or too thin to assess the specific claim, use credibility 6. Keep the rationale under 240 characters."""

_USER_TEMPLATE = """Grade this source. Return only the structured grade.

Source data:
{payload}
"""

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


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if not self._skip_depth and not self._in_title:
            self.parts.append(text)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        return " ".join(self.parts).strip()


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


@dataclass
class _PinnedTarget:
    """A validated URL pinned to a resolved public IP.

    ``connect_url`` carries the IP literal so httpx connects to exactly the
    address we validated; ``host_header`` / ``sni_hostname`` preserve the real
    hostname for routing and TLS verification.
    """

    connect_url: str
    host_header: str
    sni_hostname: str
    scheme: str


def _resolve_pinned(raw_url: str) -> _PinnedTarget:
    """Validate a public HTTP(S) URL and pin it to a resolved public IP.

    DNS is resolved *once* here and every returned address is required to be
    globally routable; the request then connects to that IP literal rather than
    re-resolving the hostname. This closes the DNS-rebinding (TOCTOU) gap where a
    name could resolve to a public address at validation time and a private one
    when httpx opened the connection.
    """
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SourceFetchError("Only public HTTP(S) source URLs can be fetched")

    host = parsed.hostname
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceFetchError("Could not resolve source host") from exc
    if not infos:
        raise SourceFetchError("Could not resolve source host")

    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise SourceFetchError("Source URL resolves to a non-public network address")
    pinned_ip = infos[0][4][0]

    ip_for_url = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    explicit_port = parsed.port not in (None, default_port)
    netloc = f"{ip_for_url}:{parsed.port}" if explicit_port else ip_for_url
    connect_url = urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, "")
    )
    host_header = f"{host}:{parsed.port}" if explicit_port else host
    return _PinnedTarget(connect_url, host_header, host, parsed.scheme)


def _extract_text(content_type: str, body: bytes, max_chars: int) -> tuple[str, str]:
    text = body.decode("utf-8", errors="replace")
    lower_type = content_type.split(";", 1)[0].strip().lower()
    if lower_type in {"text/html", "application/xhtml+xml", ""} or "<html" in text[:500].lower():
        parser = _TextExtractor()
        parser.feed(text)
        return parser.title[:240], parser.text[:max_chars]
    if lower_type.startswith("text/"):
        return "", " ".join(text.split())[:max_chars]
    raise SourceFetchError("Source content is not readable text or HTML")


def fetch_source_content(reference: str) -> FetchedSource:
    settings = get_settings()
    timeout = httpx.Timeout(settings.source_grader_fetch_timeout)
    base_headers = {"User-Agent": "IcebergSourceGrader/1.0"}
    max_bytes = settings.source_grader_fetch_max_bytes
    logical_url = reference.strip()

    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        for _ in range(4):
            target = _resolve_pinned(logical_url)
            headers = {**base_headers, "Host": target.host_header}
            extensions = (
                {"sni_hostname": target.sni_hostname}
                if target.scheme == "https"
                else {}
            )
            # Stream so the byte cap bounds memory even when the server omits a
            # Content-Length (the header check alone can't catch a chunked body).
            with client.stream(
                "GET", target.connect_url, headers=headers, extensions=extensions
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceFetchError("Source redirect did not include a target")
                    logical_url = urljoin(logical_url, location)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise SourceFetchError("Source content is too large to auto-grade")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise SourceFetchError("Source content is too large to auto-grade")
                title, text = _extract_text(
                    response.headers.get("content-type", ""),
                    bytes(body),
                    settings.source_grader_extract_max_chars,
                )
                if len(text) < 120:
                    raise SourceFetchError("Source content was too sparse to judge credibility")
                return FetchedSource(final_url=logical_url, title=title, text=text)
        raise SourceFetchError("Source followed too many redirects")


def _grade_payload(source: Source, fetched: FetchedSource | None) -> dict[str, str]:
    return {
        "title": source.title,
        "reference": source.reference,
        "analyst_summary": source.summary,
        "fetched_url": fetched.final_url if fetched else "",
        "fetched_title": fetched.title if fetched else "",
        "fetched_text": fetched.text if fetched else "",
    }


def _validate_grade_payload(payload: dict, engine: str) -> GradeResult:
    try:
        reliability = SourceReliability(payload["reliability"])
        credibility = SourceCredibility(str(payload["credibility"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Provider returned an invalid source grade") from exc
    rationale = str(payload.get("rationale") or "").strip()
    if not rationale:
        raise ValueError("Provider returned an empty source-grade rationale")
    return GradeResult(
        reliability=reliability,
        credibility=credibility,
        engine=engine,
        rationale=rationale[:500],
    )


def _json_from_text(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    return json.loads(text)


def _llm_grade_openai(source: Source, fetched: FetchedSource | None) -> GradeResult:
    settings = get_settings()
    base_url = settings.source_grader_base_url or "https://api.openai.com/v1"
    if not settings.source_grader_api_key or not settings.source_grader_model:
        raise RuntimeError("OpenAI source grader is not configured")
    response = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.source_grader_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.source_grader_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        payload=json.dumps(_grade_payload(source, fetched), ensure_ascii=True)
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "source_grade",
                    "strict": True,
                    "schema": _GRADE_SCHEMA,
                },
            },
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    engine = f"openai:{settings.source_grader_model}"
    return _validate_grade_payload(_json_from_text(content), engine)


def _llm_grade_anthropic(source: Source, fetched: FetchedSource | None) -> GradeResult:
    settings = get_settings()
    base_url = settings.source_grader_base_url or "https://api.anthropic.com/v1"
    if not settings.source_grader_api_key or not settings.source_grader_model:
        raise RuntimeError("Anthropic source grader is not configured")
    response = httpx.post(
        f"{base_url.rstrip('/')}/messages",
        headers={
            "x-api-key": settings.source_grader_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.source_grader_model,
            "max_tokens": 700,
            "temperature": 0,
            "system": _SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        payload=json.dumps(_grade_payload(source, fetched), ensure_ascii=True)
                    ),
                }
            ],
            "tools": [
                {
                    "name": "submit_source_grade",
                    "description": "Submit the Admiralty/NATO source grade.",
                    "input_schema": _GRADE_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": "submit_source_grade"},
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_source_grade":
            engine = f"anthropic:{settings.source_grader_model}"
            return _validate_grade_payload(block.get("input") or {}, engine)
    raise ValueError("Anthropic response did not include a source-grade tool result")


def _llm_grade(source: Source, fetched: FetchedSource | None) -> GradeResult:
    provider = get_settings().source_grader_provider.lower().strip()
    if provider in {"openai", "openai_compatible"}:
        return _llm_grade_openai(source, fetched)
    if provider in {"anthropic", "claude"}:
        return _llm_grade_anthropic(source, fetched)
    raise RuntimeError("No external source grader configured")


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


def heuristic_grade(source: Source, fetched: FetchedSource | None = None) -> GradeResult | None:
    category, host = _source_category(source)
    text = " ".join(
        part for part in (source.title, source.summary, fetched.title if fetched else "", fetched.text if fetched else "") if part
    )
    has_readable_content = bool((fetched and fetched.text) or source.summary.strip())

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
    if has_readable_content:
        detail = "readable source content" if fetched and fetched.text else "analyst summary"
    else:
        detail = "source identity only"
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
    settings = get_settings()
    provider = settings.source_grader_provider.lower().strip()
    fetched: FetchedSource | None = None
    warning = ""

    if source.reference.strip().lower().startswith(("http://", "https://")):
        try:
            fetched = fetch_source_content(source.reference)
        except (httpx.HTTPError, SourceFetchError, OSError, ValueError) as exc:
            warning = (
                "Could not read source content; reliability was estimated from "
                f"the reference and credibility may be marked cannot be judged. ({exc})"
            )

    result: GradeResult | None = None
    if provider != "heuristic" and (fetched or source.summary.strip()):
        try:
            result = _llm_grade(source, fetched)
        except (httpx.HTTPError, KeyError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            warning = f"LLM grading unavailable; used heuristic fallback. ({exc})"

    if not result and settings.source_grader_fallback.lower().strip() == "heuristic":
        result = heuristic_grade(source, fetched)

    if not result:
        _apply_grade(
            source,
            reliability=None,
            credibility=None,
            origin=SourceGradingOrigin.UNGRADED,
            error=warning,
        )
        return AutoGradeOutcome(source=source, applied=False, reason=warning or "No grade available")

    _apply_grade(
        source,
        reliability=result.reliability,
        credibility=result.credibility,
        origin=SourceGradingOrigin.AUTO,
        engine=result.engine,
        rationale=result.rationale,
        error=result.error or warning,
    )
    return AutoGradeOutcome(source=source, applied=True, reason=result.error or warning)


def regrade_source(source: Source) -> AutoGradeOutcome:
    return auto_grade(source)


def needs_online_grading(source: Source) -> bool:
    """Whether auto-grading this source would touch the network — i.e. fetch a
    page (http(s) reference) or call an LLM provider. Used to decide whether to
    defer grading to a background task instead of blocking the create request."""
    provider = get_settings().source_grader_provider.lower().strip()
    has_http_ref = source.reference.strip().lower().startswith(("http://", "https://"))
    has_text = bool(source.summary.strip())
    return has_http_ref or (provider != "heuristic" and has_text)


def grade_source_async(source_id: int) -> None:
    """Grade a source out-of-band (a FastAPI background task). Opens its own
    session because the request session is closed by the time this runs."""
    from .. import db  # access db.engine dynamically so tests can repoint it

    with Session(db.engine) as session:
        source = session.get(Source, source_id)
        if source is None:
            return
        auto_grade(source)
        session.add(source)
        session.commit()
