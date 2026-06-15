"""Milestone 4: FTS5 report search — relevance, facets, trigger-driven index
sync, and the stakeholder access-control filter."""

from sqlmodel import Session

from iceberg.models import Report


def _report(client, login, title, body, author="author@example.com"):
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


def _create_tag(client, login, kind="ACTOR", label="APT29", aliases=None):
    login("ADMIN", email="admin@example.com")
    return client.post(
        "/api/tags",
        json={"kind": kind, "label": label, "aliases": aliases or []},
    ).json()


def _titles(resp):
    return [r["report"]["title"] for r in resp.json()["results"]]


# --------------------------------------------------------------------------- #
# Full-text matching
# --------------------------------------------------------------------------- #
def test_fts_matches_title_and_body(client, login):
    _report(client, login, "Spearphishing wave", "Targeting the energy sector.")
    login("ANALYST", email="author@example.com")
    assert client.get("/api/search", params={"q": "spearphishing"}).json()["count"] == 1
    assert client.get("/api/search", params={"q": "energy"}).json()["count"] == 1
    assert client.get("/api/search", params={"q": "nonexistentterm"}).json()["count"] == 0


def test_fts_prefix_match(client, login):
    _report(client, login, "Ransomware report", "Discusses encryption tooling.")
    login("ANALYST", email="author@example.com")
    # token is indexed as a prefix term, so "ransom" matches "ransomware"
    assert client.get("/api/search", params={"q": "ransom"}).json()["count"] == 1


def test_fts_matches_judgement_scaffolding(client, login):
    """ICD 203 scaffolding is indexed too: a term appearing only in Key
    Judgements / Intelligence Gaps (not the title or body) is still found, and
    the update trigger keeps the new columns in sync."""
    rid = _report(client, login, "Intrusion set update", "Generic narrative body.")
    login("ANALYST", email="author@example.com")
    client.patch(
        f"/api/reports/{rid}",
        json={
            "key_judgements": "We assess uniquejudgementterm with high confidence.",
            "intelligence_gaps": "uniquegapterm remains unknown.",
        },
    )
    assert client.get("/api/search", params={"q": "uniquejudgementterm"}).json()["count"] == 1
    assert client.get("/api/search", params={"q": "uniquegapterm"}).json()["count"] == 1
    # Sync on update: clearing the field removes it from the index.
    client.patch(f"/api/reports/{rid}", json={"key_judgements": ""})
    assert client.get("/api/search", params={"q": "uniquejudgementterm"}).json()["count"] == 0


def test_bm25_ranks_more_relevant_first(client, login):
    _report(client, login, "Lazarus Group operations", "Lazarus Lazarus Lazarus activity.")
    _report(client, login, "Quarterly roundup", "A brief mention of Lazarus once.")
    login("ANALYST", email="author@example.com")
    titles = _titles(client.get("/api/search", params={"q": "lazarus"}))
    assert titles[0] == "Lazarus Group operations"


# --------------------------------------------------------------------------- #
# Facets
# --------------------------------------------------------------------------- #
def test_facet_by_tag(client, login):
    tag = _create_tag(client, login, label="APT29")
    rid = _report(client, login, "Tagged report", "body")
    _report(client, login, "Untagged report", "body")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [tag["id"]]})
    out = client.get("/api/search", params={"tag": [tag["id"]]})
    assert _titles(out) == ["Tagged report"]


def test_web_search_page_blank_facets_render_html(client, login):
    """Regression: the portal facet form always submits its filter selects, so an
    unset filter arrives as an empty string (e.g. ?intel_level=&tlp=&status=).
    The web /search route must treat blanks as "no filter" and render HTML, not
    422 with a JSON validation error (the bug seen when clicking a tag facet)."""
    tag = _create_tag(client, login, label="APT29")
    login("ANALYST", email="author@example.com")
    resp = client.get(
        f"/search?q=&tag={tag['id']}&intel_level=&tlp=&status="
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # a genuinely invalid enum value is still rejected (cleanly, not a 500)
    assert client.get("/search?intel_level=BOGUS").status_code == 400


def test_facet_by_intel_level(client, login):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    client.post("/api/reports", json={"notebook_id": nb["id"], "title": "Strat", "intel_level": "STRATEGIC"})
    client.post("/api/reports", json={"notebook_id": nb["id"], "title": "Op", "intel_level": "OPERATIONAL"})
    out = client.get("/api/search", params={"intel_level": "STRATEGIC"})
    assert _titles(out) == ["Strat"]


# --------------------------------------------------------------------------- #
# Alias-aware search (roadmap 2a)
# --------------------------------------------------------------------------- #
def test_alias_query_finds_canonical_entity_reports(client, login):
    """Searching an alias surfaces reports tagged with the canonical entity even
    when the body never names the alias — the core 2a recall win."""
    tag = _create_tag(client, login, label="APT28", aliases=["Fancy Bear", "Sofacy"])
    rid = _report(client, login, "Intrusion set update", "Generic narrative body.")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [tag["id"]]})
    # body has no "Fancy Bear" mention, yet the alias resolves via the tag.
    assert _titles(client.get("/api/search", params={"q": "Fancy Bear"})) == [
        "Intrusion set update"
    ]
    # an unrelated alias does not match.
    assert client.get("/api/search", params={"q": "Cozy Bear"}).json()["count"] == 0


def test_body_match_ranks_above_alias_only_match(client, login):
    """Body relevance stays on top; alias-only entity matches are appended."""
    tag = _create_tag(client, login, label="APT28", aliases=["Fancy Bear"])
    alias_only = _report(client, login, "Tagged only", "Generic body, no keyword.")
    _report(client, login, "Mentions fancybearkeyword", "fancybearkeyword in the body")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{alias_only}/tags", json={"tag_ids": [tag["id"]]})
    # "fancybearkeyword" hits the body of one report (FTS) and no alias.
    titles = _titles(client.get("/api/search", params={"q": "fancybearkeyword"}))
    assert titles == ["Mentions fancybearkeyword"]


# --------------------------------------------------------------------------- #
# Access control — stakeholders only ever match published reports
# --------------------------------------------------------------------------- #
def test_stakeholder_search_excludes_unpublished(client, login):
    """Regression: search must reapply ensure_visible's rule so a read-only
    stakeholder can't surface an unpublished (e.g. TLP:RED) report by keyword."""
    _report(client, login, "Phantom draft", "phantommenace secret material")
    pub = _report(client, login, "Phantom published", "phantommenace public brief")
    _publish(client, login, pub)

    login("STAKEHOLDER", email="nosy@example.com")
    titles = _titles(client.get("/api/search", params={"q": "phantommenace"}))
    assert titles == ["Phantom published"]

    # An analyst still sees both.
    login("ANALYST", email="author@example.com")
    assert client.get("/api/search", params={"q": "phantommenace"}).json()["count"] == 2


# --------------------------------------------------------------------------- #
# Trigger-driven index sync
# --------------------------------------------------------------------------- #
def test_fts_sync_on_update(client, login):
    rid = _report(client, login, "Initial", "originalkeyword in body")
    login("ANALYST", email="author@example.com")
    assert client.get("/api/search", params={"q": "originalkeyword"}).json()["count"] == 1
    client.patch(f"/api/reports/{rid}", json={"body_md": "replacedkeyword now"})
    assert client.get("/api/search", params={"q": "originalkeyword"}).json()["count"] == 0
    assert client.get("/api/search", params={"q": "replacedkeyword"}).json()["count"] == 1


def test_fts_sync_on_delete(client, login, engine):
    rid = _report(client, login, "Doomed", "ephemeralkeyword present")
    login("ANALYST", email="author@example.com")
    assert client.get("/api/search", params={"q": "ephemeralkeyword"}).json()["count"] == 1
    with Session(engine) as s:
        s.delete(s.get(Report, rid))
        s.commit()
    assert client.get("/api/search", params={"q": "ephemeralkeyword"}).json()["count"] == 0
