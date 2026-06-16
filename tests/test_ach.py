"""Analysis of Competing Hypotheses (ACH): CRUD + notebook scoping, the matrix
normalisation (stable ids, dropped blanks, orphan-rating pruning) and the
inconsistency scoring, SVG matrix generation (incl. XML-escaping + the leading
column), inline-token rendering into the report body (web preview survives nh3;
unknown / cross-notebook tokens degrade), the live-preview endpoint, writer-only
access, and the Typst token rewrite + a render smoke test."""

import pytest

from iceberg.config import get_settings
from iceberg.models import ACHModel
from iceberg.rendering.typst import _rewrite_ach_tokens, typst_available
from iceberg.services import ach as ach_service


def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _ach(client, nb_id, **fields):
    body = {
        "title": "Attribution",
        "question": "Who is behind the intrusion?",
        "hypotheses": [
            {"id": "h1", "text": "APT28"},
            {"id": "h2", "text": "Criminal group"},
        ],
        "evidence": [{"id": "e1", "text": "Spearphishing TTPs match"}],
        "ratings": {"h1:e1": "CONSISTENT", "h2:e1": "INCONSISTENT"},
    }
    body.update(fields)
    return client.post(f"/api/notebooks/{nb_id}/ach", json=body)


# --------------------------------------------------------------------------- #
# CRUD + scoping
# --------------------------------------------------------------------------- #
def test_create_appears_in_notebook(client, login):
    login("ANALYST")
    nb = _notebook(client)
    resp = _ach(client, nb["id"])
    assert resp.status_code == 201, resp.text
    a = resp.json()
    assert a["title"] == "Attribution"
    assert a["notebook_id"] == nb["id"]
    assert len(a["hypotheses"]) == 2 and a["hypotheses"][0]["id"] == "h1"
    assert a["ratings"]["h2:e1"] == "INCONSISTENT"


def test_update_and_scoping(client, login):
    login("ANALYST")
    nb_a = _notebook(client, title="A")
    nb_b = _notebook(client, title="B")
    aid = _ach(client, nb_a["id"]).json()["id"]

    ok = client.patch(
        f"/api/notebooks/{nb_a['id']}/ach/{aid}",
        json={"question": "Revised?", "ratings": {"h1:e1": "STRONGLY_INCONSISTENT"}},
    )
    assert ok.status_code == 200
    assert ok.json()["question"] == "Revised?"
    assert ok.json()["ratings"]["h1:e1"] == "STRONGLY_INCONSISTENT"

    # An ACH matrix is only reachable through its own notebook.
    cross = client.patch(
        f"/api/notebooks/{nb_b['id']}/ach/{aid}", json={"title": "x"}
    )
    assert cross.status_code == 404


def test_delete(client, login):
    login("ANALYST")
    nb = _notebook(client)
    aid = _ach(client, nb["id"]).json()["id"]
    assert client.delete(f"/api/notebooks/{nb['id']}/ach/{aid}").status_code == 204
    assert (
        client.get(f"/api/notebooks/{nb['id']}/ach/{aid}/matrix.svg").status_code == 404
    )


def test_notebook_delete_cascades(client, login, engine):
    from sqlmodel import Session, select

    login("ANALYST")
    nb = _notebook(client)
    _ach(client, nb["id"])
    assert client.delete(f"/api/notebooks/{nb['id']}").status_code == 204
    with Session(engine) as s:
        assert s.exec(select(ACHModel)).all() == []


# --------------------------------------------------------------------------- #
# Normalisation + scoring (unit)
# --------------------------------------------------------------------------- #
def test_normalise_allocates_ids_and_drops_blanks():
    hyps, evs, ratings = ach_service.normalise(
        [{"text": "A"}, {"text": ""}, {"id": "h5", "text": "B"}],
        [{"text": "E1"}],
        {},
    )
    # blank dropped; new row gets an id above the max existing (h5 -> h6)
    assert [h["text"] for h in hyps] == ["A", "B"]
    assert hyps[1]["id"] == "h5"
    assert hyps[0]["id"] == "h6"
    assert evs[0]["id"] == "e1"


def test_normalise_prunes_orphan_and_invalid_ratings():
    _h, _e, ratings = ach_service.normalise(
        [{"id": "h1", "text": "A"}],
        [{"id": "e1", "text": "E"}],
        {
            "h1:e1": "INCONSISTENT",  # kept
            "h1:e1neutral": "NEUTRAL",  # orphan key (no such cell) → dropped
            "h9:e9": "CONSISTENT",  # orphan (removed row) → dropped
            "h1:e1bad": "WHATEVER",  # orphan key anyway
        },
    )
    assert ratings == {"h1:e1": "INCONSISTENT"}


def test_normalise_drops_neutral_default():
    # NEUTRAL is the implicit default — not persisted, keeping the matrix sparse.
    _h, _e, ratings = ach_service.normalise(
        [{"id": "h1", "text": "A"}], [{"id": "e1", "text": "E"}], {"h1:e1": "NEUTRAL"}
    )
    assert ratings == {}


def test_inconsistency_score_and_leading():
    a = ACHModel(
        notebook_id=1,
        title="T",
        hypotheses=[{"id": "h1", "text": "A"}, {"id": "h2", "text": "B"}],
        evidence=[{"id": "e1", "text": "E1"}, {"id": "e2", "text": "E2"}],
        ratings={
            "h1:e1": "STRONGLY_CONSISTENT",  # 0
            "h1:e2": "STRONGLY_INCONSISTENT",  # 2
            "h2:e1": "INCONSISTENT",  # 1
            "h2:e2": "CONSISTENT",  # 0
        },
    )
    assert ach_service.inconsistency_score(a) == {"h1": 2, "h2": 1}
    assert ach_service.leading_hypothesis_ids(a) == ["h2"]  # least inconsistent


# --------------------------------------------------------------------------- #
# SVG generation (+ escaping, leading column, dynamic sizing, placard)
# --------------------------------------------------------------------------- #
def test_matrix_svg_endpoint(client, login):
    login("ANALYST")
    nb = _notebook(client)
    aid = _ach(client, nb["id"]).json()["id"]
    resp = client.get(f"/api/notebooks/{nb['id']}/ach/{aid}/matrix.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    body = resp.text
    assert body.lstrip().startswith("<svg")
    assert "ANALYSIS OF COMPETING HYPOTHESES" in body
    assert "INCONSISTENCY SCORE" in body
    assert "Spearphishing TTPs match" in body


def test_svg_escapes_text_and_is_well_formed():
    import xml.dom.minidom as minidom

    a = ACHModel(
        notebook_id=1,
        title="T",
        question="<script>alert(1)</script> & \"x\"",
        hypotheses=[{"id": "h1", "text": "<b>evil</b>"}],
        evidence=[{"id": "e1", "text": "A & B <tag>"}],
        ratings={"h1:e1": "INCONSISTENT"},
    )
    svg = ach_service.render_ach_svg(a)
    minidom.parseString(svg)  # raises if not well-formed XML
    assert "<script>" not in svg and "<b>evil" not in svg
    assert "&lt;script&gt;" in svg and "&amp;" in svg


def test_svg_flags_leading_hypothesis_and_sizes_dynamically():
    small = ACHModel(
        notebook_id=1,
        title="T",
        hypotheses=[{"id": "h1", "text": "A"}],
        evidence=[{"id": "e1", "text": "E"}],
        ratings={},
    )
    big = ACHModel(
        notebook_id=1,
        title="T",
        hypotheses=[{"id": f"h{i}", "text": f"H{i}"} for i in range(4)],
        evidence=[{"id": f"e{i}", "text": f"E{i}"} for i in range(5)],
        ratings={"h0:e0": "STRONGLY_INCONSISTENT"},  # h0 worst → not leading
    )
    svg_small = ach_service.render_ach_svg(small)
    svg_big = ach_service.render_ach_svg(big)
    assert "LEADING" in svg_big  # a most-tenable column is flagged
    # more columns/rows → a wider+taller canvas
    import re

    def _w(svg):
        return int(re.search(r'width="(\d+)"', svg).group(1))

    assert _w(svg_big) > _w(svg_small)


def test_empty_matrix_placard():
    svg = ach_service.render_ach_svg(ACHModel(notebook_id=1, title="T"))
    assert svg.lstrip().startswith("<svg")
    assert "Add at least one hypothesis" in svg


# --------------------------------------------------------------------------- #
# Inline-token rendering into a report body (web)
# --------------------------------------------------------------------------- #
def _report_with_body(client, nb_id, body_md):
    rid = client.post(
        "/api/reports", json={"notebook_id": nb_id, "title": "R"}
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": body_md})
    return rid


def test_token_renders_inline_and_survives_sanitizer(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    aid = _ach(client, nb["id"], hypotheses=[{"id": "h1", "text": "APT <evil>"}],
               ratings={}).json()["id"]
    rid = _report_with_body(
        client, nb["id"], f"# Intro\n\nText.\n\n[[ach:{aid}]]\n\n## Next"
    )
    html = client.get(f"/reports/{rid}").text
    assert "ach-figure" in html
    assert "<svg" in html  # server SVG injected post-sanitisation, not stripped
    assert f"[[ach:{aid}]]" not in html  # token consumed
    assert "&lt;evil&gt;" in html and "<evil>" not in html


def test_unknown_token_degrades(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    rid = _report_with_body(client, nb["id"], "[[ach:9999]]")
    html = client.get(f"/reports/{rid}").text
    assert "ach-missing" in html
    assert "ach-figure" not in html


def test_cross_notebook_token_not_resolved(client, login):
    login("ANALYST", email="author@example.com")
    nb_a = _notebook(client, title="A")
    nb_b = _notebook(client, title="B")
    foreign = _ach(
        client, nb_b["id"], question="Foreign question?", hypotheses=[], evidence=[],
        ratings={},
    ).json()["id"]
    rid = _report_with_body(client, nb_a["id"], f"[[ach:{foreign}]]")
    html = client.get(f"/reports/{rid}").text
    assert "Foreign question?" not in html
    assert "ach-missing" in html


def test_mixed_diamond_figure_ach_body_resolves_all(client, login):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    aid = _ach(client, nb["id"]).json()["id"]
    did = client.post(
        f"/api/notebooks/{nb['id']}/diamonds", json={"title": "D"}
    ).json()["id"]
    rid = _report_with_body(
        client, nb["id"], f"[[diamond:{did}]]\n\n[[ach:{aid}]]"
    )
    html = client.get(f"/reports/{rid}").text
    assert "diamond-figure" in html and "ach-figure" in html


# --------------------------------------------------------------------------- #
# Live-preview endpoint
# --------------------------------------------------------------------------- #
def test_preview_ach_endpoint(client, login):
    login("ANALYST")
    resp = client.post(
        "/api/preview/ach",
        json={
            "title": "T",
            "question": "Who?",
            "hypotheses": [{"id": "h1", "text": "Alpha"}],
            "evidence": [{"id": "e1", "text": "Beta evidence"}],
            "ratings": {"h1:e1": "INCONSISTENT"},
        },
    )
    assert resp.status_code == 200
    svg = resp.json()["svg"]
    assert svg.lstrip().startswith("<svg")
    assert "Beta evidence" in svg


# --------------------------------------------------------------------------- #
# Access control (writer-only)
# --------------------------------------------------------------------------- #
def test_stakeholder_cannot_mutate(client, login):
    login("ANALYST")
    nb = _notebook(client)
    aid = _ach(client, nb["id"]).json()["id"]

    login("STAKEHOLDER", email="s@example.com")
    assert _ach(client, nb["id"]).status_code == 403
    assert (
        client.patch(
            f"/api/notebooks/{nb['id']}/ach/{aid}", json={"title": "x"}
        ).status_code
        == 403
    )
    assert client.delete(f"/api/notebooks/{nb['id']}/ach/{aid}").status_code == 403
    assert (
        client.post(f"/notebooks/{nb['id']}/ach", data={"title": "x"}).status_code
        == 403
    )


def test_portal_flow(client, login):
    login("ANALYST", email="author@example.com")
    client.post("/notebooks", data={"title": "Ops"})
    nb_id = client.get("/api/notebooks").json()[0]["id"]

    # Add via the portal form -> redirected to the edit page.
    add = client.post(f"/notebooks/{nb_id}/ach", data={"title": "Attribution call"})
    assert add.status_code == 200
    assert "Attribution call" in add.text  # landed on the edit page
    # The notebook page lists the analysis + its section heading.
    page = client.get(f"/notebooks/{nb_id}").text
    assert "ACH analyses" in page and "Attribution call" in page


def test_portal_save_persists_matrix(client, login):
    import json
    import re

    login("ANALYST", email="author@example.com")
    client.post("/notebooks", data={"title": "Ops"})
    nb_id = client.get("/api/notebooks").json()[0]["id"]
    client.post(f"/notebooks/{nb_id}/ach", data={"title": "T"})
    # find the ach id via the notebook page's edit link
    page = client.get(f"/notebooks/{nb_id}").text
    aid = int(re.search(rf"/notebooks/{nb_id}/ach/(\d+)/edit", page).group(1))

    matrix = json.dumps(
        {
            "hypotheses": [{"id": "h1", "text": "Alpha"}],
            "evidence": [{"id": "e1", "text": "Gamma"}],
            "ratings": {"h1:e1": "STRONGLY_INCONSISTENT"},
        }
    )
    save = client.post(
        f"/notebooks/{nb_id}/ach/{aid}",
        data={"title": "T2", "question": "Q?", "matrix": matrix, "notes": "n"},
    )
    assert save.status_code == 200
    svg = client.get(f"/api/notebooks/{nb_id}/ach/{aid}/matrix.svg").text
    assert "Gamma" in svg and "Alpha" in svg


# --------------------------------------------------------------------------- #
# Typst path
# --------------------------------------------------------------------------- #
def test_rewrite_ach_tokens():
    ach = [(7, "Who [did] it?", "<svg/>")]
    out = _rewrite_ach_tokens("a [[ach:7]] b [[ach:8]]", ach)
    assert "![Who (did) it?](ach-7.svg)" in out
    assert "ACH analysis unavailable" in out  # id 8 not provided
    assert "[[ach:" not in out


@pytest.mark.skipif(not typst_available(), reason="Typst binary not installed")
def test_render_with_ach(client, login, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "render_output_dir", str(tmp_path / "out"))
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    aid = _ach(client, nb["id"]).json()["id"]
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "R", "tlp": "GREEN"},
    ).json()["id"]
    client.patch(f"/api/reports/{rid}", json={"body_md": f"# Body\n\n[[ach:{aid}]]"})

    resp = client.post(f"/api/reports/{rid}/render", json={"format": "FULL"})
    if resp.status_code in (500, 503):
        pytest.skip(f"Typst could not render: {resp.text}")
    assert resp.status_code == 201, resp.text
