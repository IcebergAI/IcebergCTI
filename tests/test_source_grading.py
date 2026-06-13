"""Source reliability grading: auto, manual, fallback and fetch safety."""

import socket

import httpx
import pytest

from iceberg.models import (
    Source,
    SourceCredibility,
    SourceGradingOrigin,
    SourceReliability,
)
from iceberg.services import source_grading
from iceberg.services.source_grading import FetchedSource, GradeResult, SourceFetchError


def _make_notebook(client):
    return client.post("/api/notebooks", json={"title": "Source grading"}).json()


def test_auto_grade_with_llm_success(client, login, monkeypatch):
    login("ANALYST")
    nb = _make_notebook(client)

    def fake_fetch(_reference):
        return FetchedSource(
            final_url="https://vendor.example/advisory",
            title="Advisory",
            text="Vendor advisory confirmed observed exploitation in telemetry.",
        )

    def fake_llm(source, fetched):
        assert source.title == "Vendor advisory"
        assert "telemetry" in fetched.text
        return GradeResult(
            reliability=SourceReliability.B,
            credibility=SourceCredibility.PROBABLY_TRUE,
            engine="openai:test-model",
            rationale="Vendor advisory with confirmed observed telemetry.",
        )

    monkeypatch.setenv("ICEBERG_SOURCE_GRADER_PROVIDER", "openai")
    monkeypatch.setenv("ICEBERG_SOURCE_GRADER_MODEL", "test-model")
    monkeypatch.setenv("ICEBERG_SOURCE_GRADER_API_KEY", "test-key")
    source_grading.get_settings.cache_clear()
    monkeypatch.setattr(source_grading, "fetch_source_content", fake_fetch)
    monkeypatch.setattr(source_grading, "_llm_grade", fake_llm)

    resp = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={"title": "Vendor advisory", "reference": "https://vendor.example/advisory"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reliability"] == "B"
    assert body["credibility"] == "2"
    assert body["grading_origin"] == "AUTO"
    assert body["grading_engine"] == "openai:test-model"
    assert body["grading_error"] == ""
    source_grading.get_settings.cache_clear()


def test_fetch_failure_uses_url_heuristic_and_marks_credibility_unknown(client, login, monkeypatch):
    login("ANALYST")
    nb = _make_notebook(client)

    def fail_fetch(_reference):
        raise SourceFetchError("network timeout")

    monkeypatch.setattr(source_grading, "fetch_source_content", fail_fetch)
    resp = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={
            "title": "CISA advisory",
            "reference": "https://www.cisa.gov/news-events/cybersecurity-advisories/test",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reliability"] == "B"
    assert body["credibility"] == "6"
    assert body["grading_engine"] == "heuristic:v1"
    assert "Could not read source content" in body["grading_error"]


def test_unknown_source_remains_ungraded(client, login):
    login("ANALYST")
    nb = _make_notebook(client)

    resp = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={"title": "Untethered note"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reliability"] is None
    assert body["credibility"] is None
    assert body["grading_origin"] == "UNGRADED"


def test_manual_grade_and_clear(client, login):
    login("ANALYST")
    nb = _make_notebook(client)
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources", json={"title": "Manual source"}
    ).json()

    graded = client.put(
        f"/api/notebooks/{nb['id']}/sources/{src['id']}/grade",
        json={
            "reliability": "C",
            "credibility": "3",
            "grading_rationale": "Analyst reviewed the source directly.",
        },
    )
    assert graded.status_code == 200, graded.text
    body = graded.json()
    assert body["reliability"] == "C"
    assert body["credibility"] == "3"
    assert body["grading_origin"] == "MANUAL"
    assert body["grading_engine"] == "manual"

    cleared = client.put(
        f"/api/notebooks/{nb['id']}/sources/{src['id']}/grade",
        json={"reliability": None, "credibility": None},
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["reliability"] is None
    assert body["credibility"] is None
    assert body["grading_origin"] == "UNGRADED"


def test_partial_manual_grade_is_rejected(client, login):
    login("ANALYST")
    nb = _make_notebook(client)
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources", json={"title": "Partial source"}
    ).json()

    resp = client.put(
        f"/api/notebooks/{nb['id']}/sources/{src['id']}/grade",
        json={"reliability": "B"},
    )

    assert resp.status_code == 422


def test_regrade_endpoint_updates_source(client, login, monkeypatch):
    login("ANALYST")
    nb = _make_notebook(client)
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources", json={"title": "Regrade me"}
    ).json()

    def fake_auto(source: Source):
        source.reliability = SourceReliability.D
        source.credibility = SourceCredibility.DOUBTFULLY_TRUE
        source.grading_origin = SourceGradingOrigin.AUTO
        source.grading_engine = "heuristic:test"
        source.grading_rationale = "Test regrade."
        return source_grading.AutoGradeOutcome(source=source, applied=True)

    monkeypatch.setattr(source_grading, "regrade_source", fake_auto)
    resp = client.post(f"/api/notebooks/{nb['id']}/sources/{src['id']}/auto-grade")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["source"]["reliability"] == "D"
    assert body["source"]["credibility"] == "4"


def test_fetch_rejects_private_network_url():
    with pytest.raises(SourceFetchError):
        source_grading.fetch_source_content("http://127.0.0.1/admin")


def test_heuristic_grades_confirmed_official_source():
    # Regression: an official source whose content confirms an event should reach
    # credibility 1 (CONFIRMED) — the confirmation branch previously fell through
    # to PROBABLY_TRUE, so the heuristic could never assign 1.
    src = Source(
        notebook_id=1, title="CISA advisory", reference="https://www.cisa.gov/advisory"
    )
    fetched = FetchedSource(
        final_url="https://www.cisa.gov/advisory",
        title="Advisory",
        text="CISA confirmed CVE-2024-1234 exploited in the wild; apply the patch now.",
    )
    result = source_grading.heuristic_grade(src, fetched)
    assert result is not None
    assert result.reliability == SourceReliability.B
    assert result.credibility == SourceCredibility.CONFIRMED


def test_heuristic_official_without_confirmation_stays_probably_true():
    src = Source(
        notebook_id=1, title="Gov page", reference="https://www.cisa.gov/about"
    )
    fetched = FetchedSource(
        final_url="https://www.cisa.gov/about",
        title="About",
        text="General background about the agency and its mission for the public sector.",
    )
    result = source_grading.heuristic_grade(src, fetched)
    assert result is not None
    assert result.credibility == SourceCredibility.PROBABLY_TRUE


def test_resolve_pinned_uses_validated_ip_and_keeps_hostname(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(source_grading.socket, "getaddrinfo", fake_getaddrinfo)
    target = source_grading._resolve_pinned("https://example.com/path?q=1")
    assert target.connect_url == "https://93.184.216.34/path?q=1"
    assert target.host_header == "example.com"
    assert target.sni_hostname == "example.com"


def test_resolve_pinned_rejects_dns_rebinding_to_private_ip(monkeypatch):
    # A public hostname that resolves to a private/link-local address must be
    # rejected: the pin validates the *resolved* IP, not just literal-IP URLs.
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(source_grading.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SourceFetchError):
        source_grading.fetch_source_content("https://innocent.example/")


def test_fetch_caps_oversized_chunked_body(monkeypatch):
    # Regression: a body with no Content-Length must still be bounded by the byte
    # cap (streamed), and the request must connect to the pinned IP with the real
    # Host preserved.
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(source_grading.socket, "getaddrinfo", fake_getaddrinfo)

    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.headers.get("host")
        seen["url_host"] = request.url.host

        def chunks():
            for _ in range(400):
                yield b"a" * 1000  # 400 KB total, no Content-Length

        return httpx.Response(200, headers={"content-type": "text/plain"}, content=chunks())

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        source_grading.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(SourceFetchError) as exc:
        source_grading.fetch_source_content("https://example.com/big")
    assert "too large" in str(exc.value)
    assert seen["url_host"] == "93.184.216.34"
    assert seen["host"] == "example.com"


def test_portal_renders_grade_chip_and_warning(client, login, monkeypatch):
    login("ANALYST")
    nb = _make_notebook(client)

    def fail_fetch(_reference):
        raise SourceFetchError("blocked")

    monkeypatch.setattr(source_grading, "fetch_source_content", fail_fetch)
    client.post(
        f"/notebooks/{nb['id']}/sources",
        data={
            "title": "CISA advisory",
            "reference": "https://www.cisa.gov/news-events/cybersecurity-advisories/test",
        },
    )

    page = client.get(f"/notebooks/{nb['id']}")
    assert page.status_code == 200
    assert "B6" in page.text
    assert "heuristic:v1" not in page.text
    assert "<textarea name=\"grading_rationale\"" in page.text
    assert "Recognized www.cisa.gov as official" in page.text
    assert "Could not read source content" not in page.text
    assert "Regrade" in page.text

    source_id = client.get(f"/api/notebooks/{nb['id']}").json()["sources"][0]["id"]
    page = client.post(f"/notebooks/{nb['id']}/sources/{source_id}/auto-grade")
    assert page.status_code == 200
    assert "Could not read source content; heuristic fallback used." in page.text
    assert (
        'class="source-inline-editor source-row-editor" open'
        in page.text
    )
