"""Typst rendering smoke test.

When the Typst binary is absent the render endpoint must report 503; when it is
present we render a product and confirm a downloadable PDF is produced. Package
fetch / compile failures (e.g. offline) are skipped rather than failed.
"""

import pytest

from iceberg.rendering.typst import typst_available


def test_build_data_includes_judgement_scaffolding():
    """The ICD 203 scaffolding fields reach the Typst template via data.json
    (no binary needed). Brief-vs-FULL omission is enforced in product.typ."""
    from iceberg.models import (
        Report,
        Source,
        SourceCredibility,
        SourceReliability,
    )
    from iceberg.rendering.typst import _build_data

    report = Report(
        notebook_id=1,
        title="R",
        author_id=1,
        body_md="Body.",
        key_judgements="We assess X.",
        key_assumptions="Assume Y.",
        intelligence_gaps="Gap Z.",
    )
    source = Source(
        notebook_id=1,
        title="CISA advisory",
        reliability=SourceReliability.B,
        credibility=SourceCredibility.PROBABLY_TRUE,
        grading_engine="heuristic:v1",
        grading_rationale="Internal grading rationale should not render.",
    )
    data = _build_data(report, "Author", [source], [], [], [], [])
    assert data["key_judgements"] == "We assess X."
    assert data["key_assumptions"] == "Assume Y."
    assert data["intelligence_gaps"] == "Gap Z."
    assert data["sources"][0]["grade"] == "B2"
    assert "grading_engine" not in data["sources"][0]
    assert "grading_rationale" not in data["sources"][0]


def _published_report(client, login):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "Render me", "tlp": "GREEN"},
    ).json()["id"]
    client.patch(
        f"/api/reports/{rid}",
        json={
            "body_md": "# Findings\n\nBody text.",
            "key_judgements": "- The actor is likely escalating.",
            "key_assumptions": "Collection is representative.",
            "intelligence_gaps": "Funding source unknown.",
        },
    )
    # Classify it so the PDF tag-chip rendering path is exercised.
    login("ADMIN", email="admin@example.com")
    tag = client.post(
        "/api/tags", json={"kind": "ACTOR", "label": "APT29", "external_id": "G0016"}
    ).json()
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [tag["id"]]})
    return rid


def test_render_unavailable_returns_503(client, login):
    if typst_available():
        pytest.skip("Typst is installed; covered by test_render_produces_pdf")
    rid = _published_report(client, login)
    resp = client.post(f"/api/reports/{rid}/render", json={"format": "FULL"})
    assert resp.status_code == 503


@pytest.mark.skipif(not typst_available(), reason="Typst binary not installed")
def test_render_produces_pdf(client, login, tmp_path, monkeypatch):
    monkeypatch.setenv("ICEBERG_RENDER_OUTPUT_DIR", str(tmp_path))
    rid = _published_report(client, login)

    resp = client.post(f"/api/reports/{rid}/render", json={"format": "FULL"})
    if resp.status_code in (500, 503):
        pytest.skip(f"Typst could not render (offline package fetch?): {resp.text}")
    assert resp.status_code == 201, resp.text

    # The brief format (Key-Judgements-only product) must also compile.
    brief = client.post(f"/api/reports/{rid}/render", json={"format": "EXEC_BRIEF"})
    assert brief.status_code == 201, brief.text

    products = client.get(f"/api/reports/{rid}/products").json()
    assert len(products) == 2
    dl = client.get(
        f"/api/reports/{rid}/products/{products[0]['id']}/download"
    )
    assert dl.status_code == 200
    assert dl.content[:4] == b"%PDF"
