"""Source reliability grading: offline heuristic auto-grade, manual override/clear, regrade."""

from iceberg.models import (
    Source,
    SourceCredibility,
    SourceGradingOrigin,
    SourceReliability,
)
from iceberg.services import source_grading


def _make_notebook(client):
    return client.post("/api/notebooks", json={"title": "Source grading"}).json()


def test_auto_grade_official_source_inline(client, login):
    login("ANALYST")
    nb = _make_notebook(client)

    resp = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={
            "title": "CISA advisory",
            "reference": "https://www.cisa.gov/news-events/cybersecurity-advisories/test",
        },
    )

    assert resp.status_code == 201, resp.text
    created = resp.json()
    # Grading is inline and offline — no PENDING / background task any more.
    assert created["grading_origin"] == "AUTO"
    assert created["reliability"] == "B"
    # No readable claim content (no summary) → credibility cannot be judged.
    assert created["credibility"] == "6"
    assert created["grading_engine"] == "heuristic:v1"


def test_auto_grade_uses_summary_for_credibility(client, login):
    login("ANALYST")
    nb = _make_notebook(client)

    resp = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={
            "title": "CISA advisory",
            "reference": "https://www.cisa.gov/advisory",
            "summary": "CISA confirmed CVE-2024-1234 exploited in the wild; apply the patch now.",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["grading_origin"] == "AUTO"
    assert body["reliability"] == "B"
    assert body["credibility"] == "1"


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


def test_heuristic_grades_confirmed_official_source():
    # An official source whose summary confirms an event reaches credibility 1.
    src = Source(
        notebook_id=1,
        title="CISA advisory",
        reference="https://www.cisa.gov/advisory",
        summary="CISA confirmed CVE-2024-1234 exploited in the wild; apply the patch now.",
    )
    result = source_grading.heuristic_grade(src)
    assert result is not None
    assert result.reliability == SourceReliability.B
    assert result.credibility == SourceCredibility.CONFIRMED


def test_heuristic_official_without_confirmation_stays_probably_true():
    src = Source(
        notebook_id=1,
        title="Gov page",
        reference="https://www.cisa.gov/about",
        summary="General background about the agency and its mission for the public sector.",
    )
    result = source_grading.heuristic_grade(src)
    assert result is not None
    assert result.credibility == SourceCredibility.PROBABLY_TRUE


def test_heuristic_official_without_content_cannot_judge_credibility():
    src = Source(
        notebook_id=1, title="CISA advisory", reference="https://www.cisa.gov/advisory"
    )
    result = source_grading.heuristic_grade(src)
    assert result is not None
    assert result.reliability == SourceReliability.B
    assert result.credibility == SourceCredibility.CANNOT_BE_JUDGED


def test_portal_renders_grade_chip(client, login):
    login("ANALYST")
    nb = _make_notebook(client)

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
    assert "<textarea name=\"grading_rationale\"" in page.text
    assert "Recognized www.cisa.gov as official" in page.text
    assert "Regrade" in page.text
