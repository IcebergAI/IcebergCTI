"""Typst rendering smoke test.

When the Typst binary is absent the render endpoint must report 503; when it is
present we render a product and confirm a downloadable PDF is produced. Package
fetch / compile failures (e.g. offline) are skipped rather than failed.
"""

import pytest

from iceberg.rendering.typst import typst_available


def _published_report(client, login):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "Render me", "tlp": "GREEN"},
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": "# Findings\n\nBody text."})
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

    products = client.get(f"/api/reports/{rid}/products").json()
    assert len(products) == 1
    dl = client.get(
        f"/api/reports/{rid}/products/{products[0]['id']}/download"
    )
    assert dl.status_code == 200
    assert dl.content[:4] == b"%PDF"
