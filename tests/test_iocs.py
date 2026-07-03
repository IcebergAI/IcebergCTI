"""IOC (indicator of compromise) notebook entities: CRUD + scoping + writer-only
access, report IOC citation (own-notebook only + publish immutability), the
Indicators appendix in the report view, and the portal notebook management."""

from sqlmodel import Session, select

from iceberg.models import IOC, Report, ReportStatus


def _notebook(client, title="nb"):
    return client.post("/api/notebooks", json={"title": title}).json()


def _ioc(client, nb_id, *, ioc_type="domain", value="evil.example", description="", source_id=None):
    body = {"ioc_type": ioc_type, "value": value, "description": description}
    if source_id is not None:
        body["source_id"] = source_id
    return client.post(f"/api/notebooks/{nb_id}/iocs", json=body)


def _report(client, nb_id, title="R"):
    return client.post(
        "/api/reports", json={"notebook_id": nb_id, "title": title}
    ).json()


# --------------------------------------------------------------------------- #
# CRUD + scoping
# --------------------------------------------------------------------------- #
def test_create_and_list_ioc(client, login):
    login("ANALYST")
    nb = _notebook(client)
    resp = _ioc(client, nb["id"], ioc_type="ip-src", value="198.51.100.4", description="C2")
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["ioc_type"] == "ip-src"
    assert created["value"] == "198.51.100.4"

    listed = client.get(f"/api/notebooks/{nb['id']}/iocs").json()
    assert [i["value"] for i in listed] == ["198.51.100.4"]


def test_blank_value_rejected(client, login):
    login("ANALYST")
    nb = _notebook(client)
    resp = _ioc(client, nb["id"], value="   ")
    assert resp.status_code == 400


def test_update_and_delete_ioc(client, login):
    login("ANALYST")
    nb = _notebook(client)
    ioc = _ioc(client, nb["id"]).json()
    upd = client.patch(
        f"/api/notebooks/{nb['id']}/iocs/{ioc['id']}",
        json={"description": "updated", "value": "evil2.example"},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["description"] == "updated"
    assert upd.json()["value"] == "evil2.example"

    dele = client.delete(f"/api/notebooks/{nb['id']}/iocs/{ioc['id']}")
    assert dele.status_code == 204
    assert client.get(f"/api/notebooks/{nb['id']}/iocs").json() == []


def test_ioc_notebook_scoped(client, login):
    login("ANALYST")
    nb1 = _notebook(client, "one")
    nb2 = _notebook(client, "two")
    ioc = _ioc(client, nb1["id"]).json()
    # The IOC belongs to nb1 — accessing it under nb2 is a 404.
    assert client.patch(
        f"/api/notebooks/{nb2['id']}/iocs/{ioc['id']}", json={"description": "x"}
    ).status_code == 404
    assert client.delete(
        f"/api/notebooks/{nb2['id']}/iocs/{ioc['id']}"
    ).status_code == 404


def test_source_provenance_must_be_same_notebook(client, login):
    login("ANALYST")
    nb1 = _notebook(client, "one")
    nb2 = _notebook(client, "two")
    src = client.post(
        f"/api/notebooks/{nb2['id']}/sources", json={"title": "s"}
    ).json()
    # A cross-notebook source id is silently dropped (provenance can't cross).
    ioc = _ioc(client, nb1["id"], source_id=src["id"]).json()
    assert ioc["source_id"] is None
    # A same-notebook source is kept.
    src1 = client.post(
        f"/api/notebooks/{nb1['id']}/sources", json={"title": "s1"}
    ).json()
    ioc2 = _ioc(client, nb1["id"], value="other.example", source_id=src1["id"]).json()
    assert ioc2["source_id"] == src1["id"]


def test_update_ioc_provenance_clear_and_scope(client, login):
    """#158: an explicit null clears provenance; a cross-notebook source_id
    resolves to None (consistent with create); an omitted source_id is untouched."""
    login("ANALYST")
    nb1 = _notebook(client, "one")
    nb2 = _notebook(client, "two")
    src1 = client.post(
        f"/api/notebooks/{nb1['id']}/sources", json={"title": "s1"}
    ).json()
    ioc = _ioc(client, nb1["id"], source_id=src1["id"]).json()
    assert ioc["source_id"] == src1["id"]

    # Omitting source_id leaves provenance untouched.
    upd = client.patch(
        f"/api/notebooks/{nb1['id']}/iocs/{ioc['id']}", json={"description": "x"}
    ).json()
    assert upd["source_id"] == src1["id"]

    # An explicit null clears provenance (previously impossible).
    upd = client.patch(
        f"/api/notebooks/{nb1['id']}/iocs/{ioc['id']}", json={"source_id": None}
    ).json()
    assert upd["source_id"] is None

    # A cross-notebook source_id resolves to None, matching create (previously
    # the old value was silently retained).
    src2 = client.post(
        f"/api/notebooks/{nb2['id']}/sources", json={"title": "s2"}
    ).json()
    client.patch(
        f"/api/notebooks/{nb1['id']}/iocs/{ioc['id']}", json={"source_id": src1["id"]}
    )
    upd = client.patch(
        f"/api/notebooks/{nb1['id']}/iocs/{ioc['id']}", json={"source_id": src2["id"]}
    ).json()
    assert upd["source_id"] is None


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def test_iocs_writer_only(client, login):
    login("ANALYST")
    nb = _notebook(client)
    login("STAKEHOLDER")
    assert client.get(f"/api/notebooks/{nb['id']}/iocs").status_code == 403
    assert _ioc(client, nb["id"]).status_code == 403


# --------------------------------------------------------------------------- #
# Report IOC citations
# --------------------------------------------------------------------------- #
def test_report_ioc_citation_own_notebook_only(client, login):
    login("ANALYST")
    nb = _notebook(client)
    other = _notebook(client, "other")
    ioc = _ioc(client, nb["id"], value="cited.example").json()
    foreign = _ioc(client, other["id"], value="foreign.example").json()
    report = _report(client, nb["id"])
    resp = client.put(
        f"/api/reports/{report['id']}/ioc-citations",
        json={"ioc_ids": [ioc["id"], foreign["id"]]},
    )
    assert resp.status_code == 200, resp.text
    cited = resp.json()["cited_iocs"]
    # Only the own-notebook IOC is accepted; the foreign one is filtered out.
    assert [i["value"] for i in cited] == ["cited.example"]


def test_ioc_citations_immutable_after_publish(client, login, engine):
    login("ANALYST")
    nb = _notebook(client)
    ioc = _ioc(client, nb["id"]).json()
    report = _report(client, nb["id"])
    with Session(engine) as session:
        r = session.get(Report, report["id"])
        r.status = ReportStatus.PUBLISHED
        session.add(r)
        session.commit()
    resp = client.put(
        f"/api/reports/{report['id']}/ioc-citations", json={"ioc_ids": [ioc["id"]]}
    )
    assert resp.status_code == 409


def test_indicators_appendix_in_report_view(client, login):
    login("ANALYST")
    nb = _notebook(client)
    ioc = _ioc(client, nb["id"], ioc_type="sha256", value="abc123", description="dropper").json()
    report = _report(client, nb["id"])
    client.put(
        f"/api/reports/{report['id']}/ioc-citations", json={"ioc_ids": [ioc["id"]]}
    )
    html = client.get(f"/reports/{report['id']}").text
    assert "Indicators" in html
    assert "abc123" in html
    assert "dropper" in html


# --------------------------------------------------------------------------- #
# Portal management
# --------------------------------------------------------------------------- #
def test_portal_add_and_delete_ioc(client, login, engine):
    login("ANALYST")
    nb = _notebook(client)
    resp = client.post(
        f"/notebooks/{nb['id']}/iocs",
        data={"ioc_type": "url", "value": "http://bad.example/x", "description": "phish"},
    )
    assert resp.status_code in (200, 303)
    with Session(engine) as session:
        rows = list(session.exec(select(IOC)).all())
    assert any(i.value == "http://bad.example/x" for i in rows)
    ioc_id = rows[0].id
    delete = client.post(f"/notebooks/{nb['id']}/iocs/{ioc_id}/delete")
    assert delete.status_code in (200, 303)


def test_notebook_cascade_deletes_iocs(client, login, engine):
    login("ANALYST")
    nb = _notebook(client)
    _ioc(client, nb["id"])
    client.delete(f"/api/notebooks/{nb['id']}")
    with Session(engine) as session:
        from sqlmodel import select

        assert list(session.exec(select(IOC)).all()) == []
