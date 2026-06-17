"""Backlog A: ATT&CK Navigator layer export + technique-coverage matrix.

Pure derivation over TECHNIQUE tags (no new model) — so the tests cover the
service maths (layer schema, occurrence scoring, tactic grouping) plus the API's
access scoping and the portal surfaces.
"""

from iceberg.models import Report, Tag, TagKind
from iceberg.services import attack


def _tech(label, code, tactic):
    return Tag(kind=TagKind.TECHNIQUE, label=label, slug=code.lower(),
               external_id=code, description=tactic)


def _make_report(rid, tags):
    """An in-memory report — the service only reads .tags/.title/.id, so no
    persistence (and no FK wrangling) is needed."""
    report = Report(notebook_id=1, title=f"R{rid}", author_id=1)
    report.tags = tags
    return report


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _report(client, login, title, body="body", author="author@example.com"):
    login("ANALYST", email=author)
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    return client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": title, "body_md": body},
    ).json()["id"]


def _publish(client, login, rid):
    login("ANALYST", email="author@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})


def _tag(client, login, kind="TECHNIQUE", label="Phishing", external_id="T1566",
         description="Initial Access"):
    login("ADMIN", email="admin@example.com")
    return client.post(
        "/api/tags",
        json={
            "kind": kind,
            "label": label,
            "external_id": external_id,
            "description": description,
        },
    ).json()


def _set_tags(client, login, rid, tag_ids):
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{rid}/tags", json={"tag_ids": tag_ids})


# --------------------------------------------------------------------------- #
# Service: layer schema + scoring
# --------------------------------------------------------------------------- #
def test_report_layer_is_schema_conformant_with_expected_codes():
    report = _make_report(1, [
        _tech("Phishing", "T1566", "Initial Access"),
        _tech("Valid Accounts", "T1078", "Initial Access"),
    ])
    layer = attack.report_layer(report)

    assert layer["domain"] == "enterprise-attack"
    assert layer["versions"]["layer"] == "4.5"
    codes = {t["techniqueID"] for t in layer["techniques"]}
    assert codes == {"T1566", "T1078"}
    assert all(t["score"] == 1 for t in layer["techniques"])
    assert layer["gradient"]["maxValue"] >= 1


def test_entity_layer_scores_by_occurrence():
    """A technique on two reports scores 2; one report scores 1."""
    phish = _tech("Phishing", "T1566", "Initial Access")
    exfil = _tech("Exfil", "T1041", "Exfiltration")
    actor = Tag(kind=TagKind.ACTOR, label="APT29", slug="apt29")
    r1 = _make_report(1, [phish, exfil])
    r2 = _make_report(2, [phish])
    layer = attack.entity_layer(actor, [r1, r2])

    scores = {t["techniqueID"]: t["score"] for t in layer["techniques"]}
    assert scores == {"T1566": 2, "T1041": 1}
    assert layer["gradient"]["maxValue"] == 2


def test_non_technique_and_codeless_tags_excluded():
    report = _make_report(1, [
        _tech("Phishing", "T1566", "Initial Access"),
        Tag(kind=TagKind.TECHNIQUE, label="Custom", slug="custom",
            external_id="", description="Execution"),
        Tag(kind=TagKind.ACTOR, label="APT29", slug="apt29", external_id="G0016"),
    ])
    layer = attack.report_layer(report)

    assert {t["techniqueID"] for t in layer["techniques"]} == {"T1566"}


# --------------------------------------------------------------------------- #
# Service: coverage matrix
# --------------------------------------------------------------------------- #
def test_coverage_matrix_groups_by_tactic_in_order():
    report = _make_report(1, [
        _tech("Exfil", "T1041", "Exfiltration"),
        _tech("Phishing", "T1566", "Initial Access"),
        _tech("Mystery", "T9999", "Not A Tactic"),
    ])
    matrix = attack.coverage_matrix([report])

    tactics = [c["tactic"] for c in matrix["tactics"]]
    # Initial Access precedes Exfiltration (kill-chain order), Uncategorised last.
    assert tactics == ["Initial Access", "Exfiltration", "Uncategorised"]
    assert matrix["total"] == 3


def test_coverage_matrix_empty():
    matrix = attack.coverage_matrix([])
    assert matrix == {"tactics": [], "max_count": 0, "total": 0}


def test_normalise_tactic_case_insensitive_and_fallback():
    assert attack.normalise_tactic("initial access") == "Initial Access"
    assert attack.normalise_tactic("  Execution ") == "Execution"
    assert attack.normalise_tactic("nonsense") == "Uncategorised"
    assert attack.normalise_tactic("") == "Uncategorised"


# --------------------------------------------------------------------------- #
# API: report layer
# --------------------------------------------------------------------------- #
def test_report_layer_endpoint_returns_attachment(client, login):
    rid = _report(client, login, "Intrusion")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("ANALYST", email="author@example.com")
    resp = client.get(f"/api/attack/reports/{rid}/layer")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert {t["techniqueID"] for t in resp.json()["techniques"]} == {"T1566"}


def test_report_layer_unpublished_hidden_from_stakeholder(client, login):
    rid = _report(client, login, "Draft intrusion")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("STAKEHOLDER", email="stake@example.com")
    assert client.get(f"/api/attack/reports/{rid}/layer").status_code == 404


def test_report_layer_published_visible_to_stakeholder(client, login):
    rid = _report(client, login, "Published intrusion")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    _publish(client, login, rid)
    login("STAKEHOLDER", email="stake@example.com")
    resp = client.get(f"/api/attack/reports/{rid}/layer")
    assert resp.status_code == 200
    assert {t["techniqueID"] for t in resp.json()["techniques"]} == {"T1566"}


# --------------------------------------------------------------------------- #
# API: entity layer
# --------------------------------------------------------------------------- #
def test_entity_layer_endpoint_aggregates(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT29", external_id="G0016",
                 description="")
    tech = _tag(client, login)
    r1 = _report(client, login, "Op A")
    r2 = _report(client, login, "Op B")
    _set_tags(client, login, r1, [actor["id"], tech["id"]])
    _set_tags(client, login, r2, [actor["id"], tech["id"]])
    login("ANALYST", email="author@example.com")
    resp = client.get(f"/api/attack/tags/{actor['id']}/layer")
    assert resp.status_code == 200
    scores = {t["techniqueID"]: t["score"] for t in resp.json()["techniques"]}
    assert scores == {"T1566": 2}


def test_entity_layer_rejects_non_named_kind(client, login):
    sector = _tag(client, login, kind="SECTOR", label="Energy", external_id="",
                  description="")
    login("ANALYST", email="author@example.com")
    assert client.get(f"/api/attack/tags/{sector['id']}/layer").status_code == 404
    assert client.get("/api/attack/tags/99999/layer").status_code == 404


def test_entity_layer_stakeholder_counts_published_only(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT29", external_id="G0016",
                 description="")
    tech = _tag(client, login)
    pub = _report(client, login, "Published")
    draft = _report(client, login, "Draft")
    _set_tags(client, login, pub, [actor["id"], tech["id"]])
    _set_tags(client, login, draft, [actor["id"], tech["id"]])
    _publish(client, login, pub)
    login("STAKEHOLDER", email="stake@example.com")
    resp = client.get(f"/api/attack/tags/{actor['id']}/layer")
    # Only the published report's technique counts (score 1, not 2).
    scores = {t["techniqueID"]: t["score"] for t in resp.json()["techniques"]}
    assert scores == {"T1566": 1}


# --------------------------------------------------------------------------- #
# Portal surfaces
# --------------------------------------------------------------------------- #
def test_matrix_page_renders_and_empty_state(client, login):
    login("ANALYST", email="author@example.com")
    empty = client.get("/matrix")
    assert empty.status_code == 200
    assert "No ATT&CK techniques tagged" in empty.text

    rid = _report(client, login, "Intrusion")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("ANALYST", email="author@example.com")
    populated = client.get("/matrix")
    assert populated.status_code == 200
    assert "Phishing" in populated.text
    assert "Initial Access" in populated.text


def test_report_view_shows_layer_download_when_tagged(client, login):
    rid = _report(client, login, "Intrusion")
    tag = _tag(client, login)
    _set_tags(client, login, rid, [tag["id"]])
    login("ANALYST", email="author@example.com")
    body = client.get(f"/reports/{rid}").text
    assert f"/api/attack/reports/{rid}/layer" in body


def test_entity_profile_shows_matrix_and_layer_link(client, login):
    actor = _tag(client, login, kind="ACTOR", label="APT29", external_id="G0016",
                 description="")
    tech = _tag(client, login)
    rid = _report(client, login, "Op")
    _set_tags(client, login, rid, [actor["id"], tech["id"]])
    login("ANALYST", email="author@example.com")
    body = client.get(f"/tags/{actor['id']}").text
    assert "ATT&CK coverage" in body
    assert f"/api/attack/tags/{actor['id']}/layer" in body
