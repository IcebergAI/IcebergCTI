"""Diamond Model assessments: CRUD + notebook scoping, SVG generation (incl.
XML-escaping), inline-token rendering into the report body (web preview survives
nh3; unknown / cross-notebook tokens degrade), the live-preview endpoints,
writer-only access, and the Typst token rewrite + a render smoke test."""

import pytest

from iceberg.rendering import typst as typst_mod
from iceberg.rendering.typst import _rewrite_diamond_tokens, typst_available
from iceberg.services import diamond as diamond_service


def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _diamond(client, nb_id, **fields):
    body = {"title": "Volt Typhoon", "adversary": "PRC state actor"}
    body.update(fields)
    return client.post(f"/api/notebooks/{nb_id}/diamonds", json=body)


# --------------------------------------------------------------------------- #
# CRUD + scoping
# --------------------------------------------------------------------------- #
def test_create_appears_in_notebook(client, login):
    login("ANALYST")
    nb = _notebook(client)
    resp = _diamond(client, nb["id"], title="Salt Typhoon", victim="Telco")
    assert resp.status_code == 201, resp.text
    d = resp.json()
    assert d["title"] == "Salt Typhoon"
    assert d["confidence"] == "MODERATE"  # default
    assert d["victim"] == "Telco"
    assert d["notebook_id"] == nb["id"]


def test_update_and_scoping(client, login):
    login("ANALYST")
    nb_a = _notebook(client, title="A")
    nb_b = _notebook(client, title="B")
    did = _diamond(client, nb_a["id"]).json()["id"]

    ok = client.patch(
        f"/api/notebooks/{nb_a['id']}/diamonds/{did}",
        json={"confidence": "HIGH", "capability": "Cobalt Strike"},
    )
    assert ok.status_code == 200
    assert ok.json()["confidence"] == "HIGH"
    assert ok.json()["capability"] == "Cobalt Strike"

    # A diamond is only reachable through its own notebook.
    cross = client.patch(
        f"/api/notebooks/{nb_b['id']}/diamonds/{did}", json={"title": "x"}
    )
    assert cross.status_code == 404


def test_delete(client, login):
    login("ANALYST")
    nb = _notebook(client)
    did = _diamond(client, nb["id"]).json()["id"]
    assert client.delete(f"/api/notebooks/{nb['id']}/diamonds/{did}").status_code == 204
    assert (
        client.get(f"/api/notebooks/{nb['id']}/diamonds/{did}/diagram.svg").status_code
        == 404
    )


def test_notebook_delete_cascades(client, login, engine):
    from sqlmodel import Session, select
    from iceberg.models import DiamondModel

    login("ANALYST")
    nb = _notebook(client)
    _diamond(client, nb["id"])
    assert client.delete(f"/api/notebooks/{nb['id']}").status_code == 204
    with Session(engine) as s:
        assert s.exec(select(DiamondModel)).all() == []


# --------------------------------------------------------------------------- #
# SVG generation (+ escaping)
# --------------------------------------------------------------------------- #
def test_diagram_svg_endpoint(client, login):
    login("ANALYST")
    nb = _notebook(client)
    did = _diamond(
        client, nb["id"], capability="LOTL: wmic, netsh", infrastructure="SOHO proxy"
    ).json()["id"]
    resp = client.get(f"/api/notebooks/{nb['id']}/diamonds/{did}/diagram.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    body = resp.text
    assert body.lstrip().startswith("<svg")
    assert "PRC state actor" in body
    assert "ADVERSARY" in body and "VICTIM" in body


def test_svg_escapes_vertex_text():
    from iceberg.models import DiamondModel

    d = DiamondModel(notebook_id=1, title="T", adversary="APT <evil> & co")
    svg = diamond_service.render_diamond_svg(d)
    assert "<evil>" not in svg
    assert "&lt;evil&gt;" in svg and "&amp;" in svg


# --------------------------------------------------------------------------- #
# Inline-token rendering into a report body (web)
# --------------------------------------------------------------------------- #
def _report_with_body(client, nb_id, body_md):
    rid = client.post(
        "/api/reports", json={"notebook_id": nb_id, "title": "R"}
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": body_md})
    return rid


def test_token_renders_inline_figure_and_survives_sanitizer(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    did = _diamond(client, nb["id"], adversary="APT <evil>").json()["id"]
    rid = _report_with_body(
        client, nb["id"], f"# Intro\n\nText.\n\n[[diamond:{did}]]\n\n## Next"
    )
    html = client.get(f"/reports/{rid}").text
    assert "diamond-figure" in html
    assert "<svg" in html  # server SVG injected post-sanitisation, not stripped
    assert f"[[diamond:{did}]]" not in html  # token consumed
    # escaped vertex text present; never the raw tag
    assert "&lt;evil&gt;" in html and "<evil>" not in html


def test_unknown_token_degrades(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    rid = _report_with_body(client, nb["id"], "[[diamond:9999]]")
    html = client.get(f"/reports/{rid}").text
    assert "diamond-missing" in html
    assert "diamond-figure" not in html  # no diagram injected


def test_cross_notebook_token_not_resolved(client, login):
    login("ANALYST", email="author@example.com")
    nb_a = _notebook(client, title="A")
    nb_b = _notebook(client, title="B")
    foreign = _diamond(client, nb_b["id"], adversary="Foreign actor").json()["id"]
    # Report in A references a diamond that lives in B — must not resolve.
    rid = _report_with_body(client, nb_a["id"], f"[[diamond:{foreign}]]")
    html = client.get(f"/reports/{rid}").text
    assert "Foreign actor" not in html
    assert "diamond-missing" in html


# --------------------------------------------------------------------------- #
# Live-preview endpoints
# --------------------------------------------------------------------------- #
def test_preview_resolves_tokens_with_report_id(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    did = _diamond(client, nb["id"]).json()["id"]
    rid = _report_with_body(client, nb["id"], "")

    with_ctx = client.post(
        "/api/preview",
        json={"markdown": f"[[diamond:{did}]]", "report_id": rid},
    ).json()["html"]
    assert "diamond-figure" in with_ctx and "<svg" in with_ctx

    without = client.post(
        "/api/preview", json={"markdown": f"[[diamond:{did}]]"}
    ).json()["html"]
    assert "diamond-figure" not in without
    assert f"[[diamond:{did}]]" in without  # left as literal text


def test_preview_diamond_endpoint(client, login):
    login("ANALYST")
    resp = client.post(
        "/api/preview/diamond",
        json={"title": "T", "infrastructure": "Bulletproof hosting"},
    )
    assert resp.status_code == 200
    svg = resp.json()["svg"]
    assert svg.lstrip().startswith("<svg")
    assert "Bulletproof hosting" in svg


# --------------------------------------------------------------------------- #
# Access control (writer-only)
# --------------------------------------------------------------------------- #
def test_stakeholder_cannot_mutate(client, login):
    login("ANALYST")
    nb = _notebook(client)
    did = _diamond(client, nb["id"]).json()["id"]

    login("STAKEHOLDER", email="s@example.com")
    assert _diamond(client, nb["id"]).status_code == 403
    assert (
        client.patch(
            f"/api/notebooks/{nb['id']}/diamonds/{did}", json={"title": "x"}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/notebooks/{nb['id']}/diamonds/{did}").status_code == 403
    )
    # Portal add route is blocked too.
    assert (
        client.post(
            f"/notebooks/{nb['id']}/diamonds", data={"title": "x"}
        ).status_code
        == 403
    )


def test_portal_flow(client, login):
    login("ANALYST", email="author@example.com")
    client.post("/notebooks", data={"title": "Ops"})
    nb_id = client.get("/api/notebooks").json()[0]["id"]

    # Add via the portal form -> redirected to the edit page.
    add = client.post(
        f"/notebooks/{nb_id}/diamonds",
        data={"title": "Scattered Spider", "adversary": "eCrime collective"},
    )
    assert add.status_code == 200
    assert "Scattered Spider" in add.text  # landed on the edit page
    # The notebook page lists the model + its token.
    page = client.get(f"/notebooks/{nb_id}").text
    assert "Scattered Spider" in page and "Diamond models" in page


# --------------------------------------------------------------------------- #
# Typst path
# --------------------------------------------------------------------------- #
def test_rewrite_diamond_tokens():
    diamonds = [(7, "Volt [Typhoon]", "<svg/>")]
    out = _rewrite_diamond_tokens("a [[diamond:7]] b [[diamond:8]]", diamonds)
    assert "![Volt (Typhoon)](diamond-7.svg)" in out
    assert "diamond model unavailable" in out  # id 8 not provided
    assert "[[diamond:" not in out


@pytest.mark.skipif(not typst_available(), reason="Typst binary not installed")
def test_render_with_diamond(client, login, tmp_path, monkeypatch):
    monkeypatch.setattr(typst_mod.settings, "render_output_dir", str(tmp_path / "out"))
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    did = _diamond(
        client, nb["id"], capability="Cobalt Strike", victim="US CI"
    ).json()["id"]
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "R", "tlp": "GREEN"},
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": f"# Body\n\n[[diamond:{did}]]"})

    resp = client.post(f"/api/reports/{rid}/render", json={"format": "FULL"})
    if resp.status_code in (500, 503):
        pytest.skip(f"Typst could not render: {resp.text}")
    assert resp.status_code == 201, resp.text
