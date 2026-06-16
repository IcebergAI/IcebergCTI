"""Entity relationships — the knowledge graph (roadmap 2c).

Covers admin-only CRUD, loose source/target-kind scoping, the inbound + outbound
profile rendering, FK cascade on tag delete, and SVG escaping. Mirrors the
Diamond Model + tag test patterns (in-memory SQLite + dev-login fixture)."""

from iceberg.models import RelationType, Tag, TagKind
from iceberg.services import relationships as rel_service


def _tag(client, login, *, kind="ACTOR", label="APT28"):
    login("ADMIN", email="admin@example.com")
    resp = client.post("/api/tags", json={"kind": kind, "label": label})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _rel(client, source_id, target_id, verb="uses"):
    return client.post(
        "/api/relationships",
        json={
            "source_tag_id": source_id,
            "target_tag_id": target_id,
            "relation_type": verb,
        },
    )


# --------------------------------------------------------------------------- #
# Authz
# --------------------------------------------------------------------------- #
def test_create_relationship_is_admin_only(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    malware = _tag(client, login, kind="MALWARE", label="X-Agent")
    login("ANALYST", email="analyst@example.com")
    resp = _rel(client, actor, malware)
    assert resp.status_code == 403, resp.text


def test_delete_relationship_is_admin_only(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    malware = _tag(client, login, kind="MALWARE", label="X-Agent")
    rid = _rel(client, actor, malware).json()["id"]
    login("ANALYST", email="analyst@example.com")
    resp = client.delete(f"/api/relationships/{rid}")
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# Round-trip + loose validation / scoping
# --------------------------------------------------------------------------- #
def test_relationship_roundtrips(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    malware = _tag(client, login, kind="MALWARE", label="X-Agent")
    resp = _rel(client, actor, malware, "uses")
    assert resp.status_code == 201, resp.text
    rid = resp.json()["id"]

    listing = client.get("/api/relationships").json()
    assert any(r["id"] == rid and r["relation_type"] == "uses" for r in listing)

    assert client.delete(f"/api/relationships/{rid}").status_code == 204
    assert client.get("/api/relationships").json() == []


def test_actor_targets_sector_allowed(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    sector = _tag(client, login, kind="SECTOR", label="Energy")
    assert _rel(client, actor, sector, "targets").status_code == 201


def test_reject_non_named_source(client, login):
    # SECTOR/TECHNIQUE/TOPIC cannot be a relationship *source*.
    sector = _tag(client, login, kind="SECTOR", label="Energy")
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    assert _rel(client, sector, actor).status_code == 400


def test_reject_non_targetable_target(client, login):
    # A TECHNIQUE/TOPIC cannot be a relationship target (only named-threat + SECTOR).
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    technique = _tag(client, login, kind="TECHNIQUE", label="Phishing")
    assert _rel(client, actor, technique).status_code == 400


def test_reject_self_reference(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    assert _rel(client, actor, actor, "related-to").status_code == 400


def test_reject_duplicate_triple(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    malware = _tag(client, login, kind="MALWARE", label="X-Agent")
    assert _rel(client, actor, malware, "uses").status_code == 201
    assert _rel(client, actor, malware, "uses").status_code == 409
    # a different verb between the same pair is allowed
    assert _rel(client, actor, malware, "related-to").status_code == 201


def test_missing_tag_404(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    resp = client.post(
        "/api/relationships",
        json={"source_tag_id": actor, "target_tag_id": 9999, "relation_type": "uses"},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Profile rendering — both endpoints (acceptance criterion)
# --------------------------------------------------------------------------- #
def test_profile_renders_inbound_and_outbound(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    malware = _tag(client, login, kind="MALWARE", label="Cobalt Strike")
    assert _rel(client, actor, malware, "uses").status_code == 201

    # outbound on the source's profile
    src_page = client.get(f"/tags/{actor}")
    assert src_page.status_code == 200
    assert "Relationship" in src_page.text
    assert "uses" in src_page.text
    assert "Cobalt Strike" in src_page.text

    # inbound on the target's profile
    tgt_page = client.get(f"/tags/{malware}")
    assert tgt_page.status_code == 200
    assert "uses" in tgt_page.text
    assert "APT28" in tgt_page.text


# --------------------------------------------------------------------------- #
# Cascade
# --------------------------------------------------------------------------- #
def test_tag_delete_cascades_relationships(client, login, engine):
    actor = _tag(client, login, kind="ACTOR", label="APT28")
    malware = _tag(client, login, kind="MALWARE", label="X-Agent")
    assert _rel(client, actor, malware, "uses").status_code == 201

    login("ADMIN", email="admin@example.com")
    assert client.delete(f"/api/tags/{actor}").status_code == 204
    assert client.get("/api/relationships").json() == []


# --------------------------------------------------------------------------- #
# SVG generation + escaping
# --------------------------------------------------------------------------- #
def test_graph_svg_escapes_labels():
    centre = Tag(id=1, kind=TagKind.ACTOR, label="APT <evil> & co", slug="apt")
    other = Tag(id=2, kind=TagKind.MALWARE, label="<script>x</script>", slug="m")
    edge = rel_service.Edge(RelationType.USES, other, 1)
    svg = rel_service.render_relationship_graph_svg(centre, [edge], [])
    assert svg.lstrip().startswith("<svg")
    assert "<script" not in svg
    assert "&lt;evil&gt;" in svg and "&amp;" in svg
    assert "uses" in svg


def test_graph_svg_empty_when_no_edges():
    centre = Tag(id=1, kind=TagKind.ACTOR, label="APT28", slug="apt28")
    assert rel_service.render_relationship_graph_svg(centre, [], []) == ""
