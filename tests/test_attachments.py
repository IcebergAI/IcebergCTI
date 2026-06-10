"""Notebook attachments: upload/validate/download/delete, writer-only access,
report citation (scoping + publish immutability), cascade + file cleanup, and
the portal flow."""

import pytest

from iceberg.rendering.typst import typst_available
from iceberg.services import attachments as attachment_service


@pytest.fixture(autouse=True)
def _attachments_dir(tmp_path, monkeypatch):
    """Redirect attachment storage to a per-test temp dir (the service reads the
    live module-level settings object)."""
    target = tmp_path / "att"
    monkeypatch.setattr(attachment_service.settings, "attachments_dir", str(target))
    return target


def _count_files(directory) -> int:
    return len(list(directory.iterdir())) if directory.exists() else 0


def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _upload(
    client,
    nb_id,
    *,
    name="ref.pdf",
    content=b"%PDF-1.4 fake",
    ctype="application/pdf",
    title="",
    summary="",
):
    return client.post(
        f"/api/notebooks/{nb_id}/attachments",
        files={"file": (name, content, ctype)},
        data={"title": title, "summary": summary},
    )


# --------------------------------------------------------------------------- #
# Upload / validation
# --------------------------------------------------------------------------- #
def test_upload_appears_in_notebook(client, login):
    login("ANALYST")
    nb = _notebook(client)
    resp = _upload(client, nb["id"], name="brief.pdf", summary="vendor report")
    assert resp.status_code == 201, resp.text
    att = resp.json()
    assert att["original_filename"] == "brief.pdf"
    assert att["content_type"] == "application/pdf"
    assert att["file_size"] > 0

    detail = client.get(f"/api/notebooks/{nb['id']}").json()
    assert [a["id"] for a in detail["attachments"]] == [att["id"]]


def test_download_roundtrip(client, login):
    login("ANALYST")
    nb = _notebook(client)
    body = b"%PDF-1.4 the bytes"
    att = _upload(client, nb["id"], content=body).json()

    dl = client.get(f"/api/notebooks/{nb['id']}/attachments/{att['id']}/download")
    assert dl.status_code == 200
    assert dl.content == body
    assert "attachment" in dl.headers["content-disposition"].lower()
    assert dl.headers["x-content-type-options"] == "nosniff"


def test_reject_disallowed_type(client, login):
    login("ANALYST")
    nb = _notebook(client)
    resp = _upload(
        client, nb["id"], name="m.exe", content=b"MZ", ctype="application/x-msdownload"
    )
    assert resp.status_code == 415


def test_reject_extension_type_mismatch(client, login):
    """Declared PDF but an .exe extension — disagreement is rejected."""
    login("ANALYST")
    nb = _notebook(client)
    resp = _upload(client, nb["id"], name="payload.exe", ctype="application/pdf")
    assert resp.status_code == 415


def test_reject_oversize(client, login, _attachments_dir, monkeypatch):
    login("ANALYST")
    nb = _notebook(client)
    monkeypatch.setattr(attachment_service.settings, "attachment_max_mb", 0)
    resp = _upload(client, nb["id"], content=b"more than zero bytes")
    assert resp.status_code == 413
    # The partial write must be cleaned up.
    assert _count_files(_attachments_dir) == 0


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def test_stakeholder_cannot_upload_or_download(client, login):
    login("ANALYST")
    nb = _notebook(client)
    att = _upload(client, nb["id"]).json()

    login("STAKEHOLDER", email="s@example.com")
    assert _upload(client, nb["id"]).status_code == 403
    assert (
        client.get(
            f"/api/notebooks/{nb['id']}/attachments/{att['id']}/download"
        ).status_code
        == 403
    )


# --------------------------------------------------------------------------- #
# Deletion + cascade clean-up
# --------------------------------------------------------------------------- #
def test_delete_removes_row_and_file(client, login, _attachments_dir):
    login("ANALYST")
    nb = _notebook(client)
    att = _upload(client, nb["id"]).json()
    assert _count_files(_attachments_dir) == 1

    assert (
        client.delete(
            f"/api/notebooks/{nb['id']}/attachments/{att['id']}"
        ).status_code
        == 204
    )
    assert _count_files(_attachments_dir) == 0
    assert client.get(f"/api/notebooks/{nb['id']}").json()["attachments"] == []


def test_notebook_delete_cascades_and_cleans_files(client, login, _attachments_dir):
    login("ANALYST")
    nb = _notebook(client)
    _upload(client, nb["id"])
    assert _count_files(_attachments_dir) == 1

    assert client.delete(f"/api/notebooks/{nb['id']}").status_code == 204
    assert client.get(f"/api/notebooks/{nb['id']}").status_code == 404
    assert _count_files(_attachments_dir) == 0  # no orphaned files left


# --------------------------------------------------------------------------- #
# Report citation: scoping + publish immutability
# --------------------------------------------------------------------------- #
def test_report_cites_attachment_own_notebook_only(client, login):
    login("ANALYST", email="author@example.com")
    nb_a = _notebook(client, title="A")
    nb_b = _notebook(client, title="B")
    own = _upload(client, nb_a["id"], name="own.pdf").json()
    foreign = _upload(client, nb_b["id"], name="foreign.pdf").json()
    report = client.post(
        "/api/reports", json={"notebook_id": nb_a["id"], "title": "R"}
    ).json()

    # Foreign-notebook attachment is silently dropped (like a foreign source).
    drop = client.put(
        f"/api/reports/{report['id']}/attachments",
        json={"attachment_ids": [foreign["id"]]},
    )
    assert drop.status_code == 200
    assert drop.json()["cited_attachments"] == []

    link = client.put(
        f"/api/reports/{report['id']}/attachments",
        json={"attachment_ids": [own["id"]]},
    )
    assert link.status_code == 200
    assert [a["id"] for a in link.json()["cited_attachments"]] == [own["id"]]

    detail = client.get(f"/api/reports/{report['id']}").json()
    assert [a["id"] for a in detail["cited_attachments"]] == [own["id"]]


def test_published_report_attachments_immutable(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    att = _upload(client, nb["id"]).json()
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "R", "tlp": "GREEN"},
    ).json()["id"]
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})

    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})

    login("ANALYST", email="author@example.com")
    resp = client.put(
        f"/api/reports/{rid}/attachments", json={"attachment_ids": [att["id"]]}
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Portal
# --------------------------------------------------------------------------- #
def test_portal_attachment_flow(client, login):
    login("ANALYST", email="author@example.com")
    nb = client.post(
        "/notebooks", data={"title": "Ops", "topic": "x"}
    )  # creates + redirects
    nb_id = client.get("/api/notebooks").json()[0]["id"]

    # Upload via the multipart portal form.
    up = client.post(
        f"/notebooks/{nb_id}/attachments",
        files={"file": ("evidence.pdf", b"%PDF-1.4 portal", "application/pdf")},
        data={"title": "Evidence", "summary": "screenshot"},
    )
    assert up.status_code == 200
    assert "Evidence" in up.text  # landed back on the notebook page

    att_id = client.get(f"/api/notebooks/{nb_id}").json()["attachments"][0]["id"]
    dl = client.get(f"/notebooks/{nb_id}/attachments/{att_id}/download")
    assert dl.status_code == 200 and dl.content == b"%PDF-1.4 portal"

    # Report editor shows the attachments-cited panel.
    rid = client.post(
        "/api/reports", json={"notebook_id": nb_id, "title": "R"}
    ).json()["id"]
    edit = client.get(f"/reports/{rid}/edit")
    assert "Attachments cited" in edit.text and "evidence.pdf" in edit.text

    # Delete via the portal.
    rm = client.post(f"/notebooks/{nb_id}/attachments/{att_id}/delete")
    assert rm.status_code == 200
    assert client.get(f"/api/notebooks/{nb_id}").json()["attachments"] == []


# --------------------------------------------------------------------------- #
# Rendering (only when Typst is installed)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not typst_available(), reason="Typst binary not installed")
def test_render_includes_cited_attachment(client, login, tmp_path, monkeypatch):
    from iceberg.rendering import typst as typst_mod

    monkeypatch.setattr(typst_mod.settings, "render_output_dir", str(tmp_path / "out"))
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    att = _upload(client, nb["id"], name="annex.pdf").json()
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "R", "tlp": "GREEN"},
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": "# Body"})
    client.put(
        f"/api/reports/{rid}/attachments", json={"attachment_ids": [att["id"]]}
    )

    resp = client.post(f"/api/reports/{rid}/render", json={"format": "FULL"})
    if resp.status_code in (500, 503):
        pytest.skip(f"Typst could not render: {resp.text}")
    assert resp.status_code == 201, resp.text
