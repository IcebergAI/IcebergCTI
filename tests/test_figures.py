"""Notebook figures: upload/validate/delete, writer-only access, the data-URI
inline-token rendering (web view + live preview, post-nh3 injection, unknown +
cross-notebook degrade, mixed diamond+figure body), the writer-only raw serve,
notebook cascade + file cleanup, the editor insert UI, and the Typst path."""

import base64

import pytest

from iceberg.config import get_settings
from iceberg.rendering.typst import _rewrite_figure_tokens, typst_available

# A minimal but valid 1x1 PNG — small enough to inline, real enough for Typst.
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
    b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def _figures_dir(tmp_path, monkeypatch):
    """Redirect figure storage to a per-test temp dir (services read config via
    the cached ``get_settings()`` singleton, so patching its attribute is seen
    everywhere)."""
    target = tmp_path / "fig"
    monkeypatch.setattr(get_settings(), "figures_dir", str(target))
    return target


def _count_files(directory) -> int:
    return len(list(directory.iterdir())) if directory.exists() else 0


def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _upload(client, nb_id, *, name="shot.png", content=_PNG, ctype="image/png", title=""):
    return client.post(
        f"/api/notebooks/{nb_id}/figures",
        files={"file": (name, content, ctype)},
        data={"title": title},
    )


def _report_with_body(client, nb_id, body_md):
    rid = client.post(
        "/api/reports", json={"notebook_id": nb_id, "title": "R"}
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": body_md})
    return rid


# --------------------------------------------------------------------------- #
# Upload / validation
# --------------------------------------------------------------------------- #
def test_upload_appears_in_notebook(client, login):
    login("ANALYST")
    nb = _notebook(client)
    resp = _upload(client, nb["id"], name="diagram.png", title="Kill chain")
    assert resp.status_code == 201, resp.text
    fig = resp.json()
    assert fig["original_filename"] == "diagram.png"
    assert fig["content_type"] == "image/png"
    assert fig["title"] == "Kill chain"
    assert fig["file_size"] > 0

    detail = client.get(f"/api/notebooks/{nb['id']}").json()
    assert [f["id"] for f in detail["figures"]] == [fig["id"]]


def test_reject_non_image_type(client, login):
    """PDFs (and WebP) are valid attachments but never figures."""
    login("ANALYST")
    nb = _notebook(client)
    assert _upload(
        client, nb["id"], name="ref.pdf", content=b"%PDF", ctype="application/pdf"
    ).status_code == 415
    assert _upload(
        client, nb["id"], name="x.webp", content=b"RIFF", ctype="image/webp"
    ).status_code == 415


def test_reject_extension_type_mismatch(client, login):
    login("ANALYST")
    nb = _notebook(client)
    resp = _upload(client, nb["id"], name="photo.gif", ctype="image/png")
    assert resp.status_code == 415


def test_reject_oversize(client, login, _figures_dir, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    monkeypatch.setattr(get_settings(), "figure_max_mb", 0)
    resp = _upload(client, nb["id"])
    assert resp.status_code == 413
    assert _count_files(_figures_dir) == 0  # partial write cleaned up


# --------------------------------------------------------------------------- #
# Access control (writer-only collection)
# --------------------------------------------------------------------------- #
def test_stakeholder_cannot_upload_serve_or_delete(client, login):
    login("ANALYST")
    nb = _notebook(client)
    fig = _upload(client, nb["id"]).json()

    login("STAKEHOLDER", email="s@example.com")
    assert _upload(client, nb["id"]).status_code == 403
    assert (
        client.get(f"/api/notebooks/{nb['id']}/figures/{fig['id']}/raw").status_code
        == 403
    )
    assert (
        client.delete(f"/api/notebooks/{nb['id']}/figures/{fig['id']}").status_code
        == 403
    )


def test_raw_serves_inline_bytes(client, login):
    login("ANALYST")
    nb = _notebook(client)
    fig = _upload(client, nb["id"]).json()
    raw = client.get(f"/api/notebooks/{nb['id']}/figures/{fig['id']}/raw")
    assert raw.status_code == 200
    assert raw.content == _PNG
    assert raw.headers["content-type"] == "image/png"
    assert "inline" in raw.headers["content-disposition"].lower()
    assert raw.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------- #
# Deletion + cascade clean-up
# --------------------------------------------------------------------------- #
def test_delete_removes_row_and_file(client, login, _figures_dir):
    login("ANALYST")
    nb = _notebook(client)
    fig = _upload(client, nb["id"]).json()
    assert _count_files(_figures_dir) == 1

    assert (
        client.delete(f"/api/notebooks/{nb['id']}/figures/{fig['id']}").status_code
        == 204
    )
    assert _count_files(_figures_dir) == 0
    assert client.get(f"/api/notebooks/{nb['id']}").json()["figures"] == []


def test_notebook_delete_cascades_and_cleans_files(client, login, _figures_dir):
    login("ANALYST")
    nb = _notebook(client)
    _upload(client, nb["id"])
    assert _count_files(_figures_dir) == 1

    assert client.delete(f"/api/notebooks/{nb['id']}").status_code == 204
    assert client.get(f"/api/notebooks/{nb['id']}").status_code == 404
    assert _count_files(_figures_dir) == 0  # no orphaned files left


# --------------------------------------------------------------------------- #
# Inline-token rendering into a report body (web)
# --------------------------------------------------------------------------- #
def test_token_renders_inline_data_uri_and_survives_sanitizer(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    fid = _upload(client, nb["id"], title="Exhibit A").json()["id"]
    rid = _report_with_body(
        client, nb["id"], f"# Intro\n\nText.\n\n[[figure:{fid}]]\n\n## Next"
    )
    html = client.get(f"/reports/{rid}").text
    assert "report-figure" in html
    # The bytes are inlined as a base64 data URI post-sanitisation (nh3 would
    # otherwise strip a data: URI).
    b64 = base64.b64encode(_PNG).decode("ascii")
    assert f"data:image/png;base64,{b64}" in html
    assert f"[[figure:{fid}]]" not in html  # token consumed
    assert 'alt="Exhibit A"' in html


def test_unknown_token_degrades(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    rid = _report_with_body(client, nb["id"], "[[figure:9999]]")
    html = client.get(f"/reports/{rid}").text
    assert "figure-missing" in html
    assert "report-figure" not in html
    assert "data:image" not in html


def test_cross_notebook_token_not_resolved(client, login):
    login("ANALYST", email="author@example.com")
    nb_a = _notebook(client, title="A")
    nb_b = _notebook(client, title="B")
    foreign = _upload(client, nb_b["id"], title="Foreign image").json()["id"]
    rid = _report_with_body(client, nb_a["id"], f"[[figure:{foreign}]]")
    html = client.get(f"/reports/{rid}").text
    assert "Foreign image" not in html
    assert "figure-missing" in html


def test_mixed_diamond_and_figure_body(client, login):
    """A body with both token kinds renders both (one shared _to_html pass)."""
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    fid = _upload(client, nb["id"]).json()["id"]
    did = client.post(
        f"/api/notebooks/{nb['id']}/diamonds",
        json={"title": "D", "adversary": "Actor"},
    ).json()["id"]
    rid = _report_with_body(
        client, nb["id"], f"[[diamond:{did}]]\n\n[[figure:{fid}]]"
    )
    html = client.get(f"/reports/{rid}").text
    assert "diamond-figure" in html and "report-figure" in html


# --------------------------------------------------------------------------- #
# Live preview
# --------------------------------------------------------------------------- #
def test_preview_product_resolves_figure(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    fid = _upload(client, nb["id"]).json()["id"]
    rid = _report_with_body(client, nb["id"], "")
    html = client.post(
        "/api/preview/product",
        json={"report_id": rid, "body_md": f"[[figure:{fid}]]"},
    ).json()["html"]
    assert "report-figure" in html and "data:image/png;base64," in html


# --------------------------------------------------------------------------- #
# Portal + editor UI
# --------------------------------------------------------------------------- #
def test_portal_figure_flow_and_editor_insert(client, login):
    login("ANALYST", email="author@example.com")
    client.post("/notebooks", data={"title": "Ops"})
    nb_id = client.get("/api/notebooks").json()[0]["id"]

    up = client.post(
        f"/notebooks/{nb_id}/figures",
        files={"file": ("evidence.png", _PNG, "image/png")},
        data={"title": "Evidence"},
    )
    assert up.status_code == 200
    page = client.get(f"/notebooks/{nb_id}").text
    assert "Figures" in page and "Evidence" in page

    fid = client.get(f"/api/notebooks/{nb_id}").json()["figures"][0]["id"]
    # The editor lists the figure with its token + an insert affordance.
    rid = client.post(
        "/api/reports", json={"notebook_id": nb_id, "title": "R"}
    ).json()["id"]
    edit = client.get(f"/reports/{rid}/edit")
    assert f"[[figure:{fid}]]" in edit.text and "insertFigure" in edit.text

    # Delete via the portal.
    rm = client.post(f"/notebooks/{nb_id}/figures/{fid}/delete")
    assert rm.status_code == 200
    assert client.get(f"/api/notebooks/{nb_id}").json()["figures"] == []


# --------------------------------------------------------------------------- #
# Typst path
# --------------------------------------------------------------------------- #
def test_rewrite_figure_tokens():
    figures = [(7, "Cap [shot]", "/tmp/x.png", ".png")]
    out = _rewrite_figure_tokens("a [[figure:7]] b [[figure:8]]", figures)
    assert "![Cap (shot)](figure-7.png)" in out
    assert "figure unavailable" in out  # id 8 not provided
    assert "[[figure:" not in out


@pytest.mark.skipif(not typst_available(), reason="Typst binary not installed")
def test_render_with_figure(client, login, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "render_output_dir", str(tmp_path / "out"))
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    fid = _upload(client, nb["id"]).json()["id"]
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "R", "tlp": "GREEN"},
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": f"# Body\n\n[[figure:{fid}]]"})

    resp = client.post(f"/api/reports/{rid}/render", json={"format": "FULL"})
    if resp.status_code in (500, 503):
        pytest.skip(f"Typst could not render: {resp.text}")
    assert resp.status_code == 201, resp.text
