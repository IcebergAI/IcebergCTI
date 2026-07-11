"""Milestone 4: controlled-taxonomy tags — admin-only curation, report
classification (editable post-publish), retire semantics, and the seed."""

from sqlmodel import Session, select

from iceberg import seed as seed_cli
from iceberg.models import (
    AuditAction,
    AuditEvent,
    Motivation,
    ReportTag,
    Tag,
    TagKind,
    UserTagSubscription,
)
from iceberg.services.tags import (
    load_starter_tags,
    normalise_motivations,
    seed_default_taxonomy,
    slugify,
)


def _make_report(client, login, title="APT29 wave", body="phishing against finance"):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    return client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": title, "body_md": body},
    ).json()["id"]


def _create_tag(client, login, kind="ACTOR", label="APT29", external_id=""):
    login("ADMIN", email="admin@example.com")
    resp = client.post(
        "/api/tags",
        json={"kind": kind, "label": label, "external_id": external_id},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _publish(client, login, rid):
    login("ANALYST", email="author@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    pub = client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})
    assert pub.json()["status"] == "PUBLISHED"


# --------------------------------------------------------------------------- #
# Curation is admin-only (controlled vocabulary)
# --------------------------------------------------------------------------- #
def test_only_admin_creates_tags(client, login):
    login("ANALYST", email="a@example.com")
    assert client.post("/api/tags", json={"kind": "ACTOR", "label": "X"}).status_code == 403
    login("STAKEHOLDER", email="s@example.com")
    assert client.post("/api/tags", json={"kind": "ACTOR", "label": "X"}).status_code == 403
    login("ADMIN", email="admin@example.com")
    assert client.post("/api/tags", json={"kind": "ACTOR", "label": "X"}).status_code == 201


def test_any_role_can_list_tags(client, login):
    _create_tag(client, login, label="APT29")
    login("STAKEHOLDER", email="s@example.com")
    labels = [t["label"] for t in client.get("/api/tags").json()]
    assert "APT29" in labels


def test_duplicate_tag_within_kind_rejected(client, login):
    _create_tag(client, login, kind="SECTOR", label="Energy")
    login("ADMIN", email="admin@example.com")
    dup = client.post("/api/tags", json={"kind": "SECTOR", "label": "energy"})
    assert dup.status_code == 409  # slug collision, case-insensitive


def test_same_label_different_kind_allowed(client, login):
    _create_tag(client, login, kind="ACTOR", label="Sandworm")
    login("ADMIN", email="admin@example.com")
    other = client.post("/api/tags", json={"kind": "MALWARE", "label": "Sandworm"})
    assert other.status_code == 201


# --------------------------------------------------------------------------- #
# Retire semantics
# --------------------------------------------------------------------------- #
def test_retire_hides_tag_from_default_listing(client, login):
    tag = _create_tag(client, login, label="Old Actor")
    login("ADMIN", email="admin@example.com")
    client.patch(f"/api/tags/{tag['id']}", json={"active": False})
    active = [t["id"] for t in client.get("/api/tags").json()]
    assert tag["id"] not in active
    everything = [t["id"] for t in client.get("/api/tags", params={"include_inactive": True}).json()]
    assert tag["id"] in everything


def test_retired_tag_stays_on_report(client, login):
    tag = _create_tag(client, login, label="Cozy Bear")
    rid = _make_report(client, login)
    login("ANALYST", email="author@example.com")
    assert client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [tag["id"]]}).status_code == 200
    # retire it
    login("ADMIN", email="admin@example.com")
    client.patch(f"/api/tags/{tag['id']}", json={"active": False})
    # still attached to the report
    login("ANALYST", email="author@example.com")
    tags = client.get(f"/api/reports/{rid}").json()["tags"]
    assert [t["label"] for t in tags] == ["Cozy Bear"]


# --------------------------------------------------------------------------- #
# Classification + the decision-1 regression
# --------------------------------------------------------------------------- #
def test_set_report_tags_replaces(client, login):
    a = _create_tag(client, login, kind="ACTOR", label="APT29")
    b = _create_tag(client, login, kind="SECTOR", label="Energy")
    rid = _make_report(client, login)
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [a["id"], b["id"]]})
    out = client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [b["id"]]})
    assert [t["label"] for t in out.json()["tags"]] == ["Energy"]


def test_tags_editable_after_publish(client, login):
    """Regression (decision 1): tags are classification metadata, deliberately
    editable after publication — unlike report content/citations."""
    tag = _create_tag(client, login, label="APT29")
    rid = _make_report(client, login)
    _publish(client, login, rid)
    login("ANALYST", email="author@example.com")
    # content edits are blocked once published...
    assert client.patch(f"/api/reports/{rid}", json={"title": "x"}).status_code == 409
    # ...but tags can still be set.
    resp = client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [tag["id"]]})
    assert resp.status_code == 200
    assert [t["label"] for t in resp.json()["tags"]] == ["APT29"]


def test_non_author_cannot_tag(client, login):
    tag = _create_tag(client, login, label="APT29")
    rid = _make_report(client, login)
    login("ANALYST", email="someone-else@example.com")
    assert client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [tag["id"]]}).status_code == 403


# --------------------------------------------------------------------------- #
# Aliases (roadmap 2a) — named-threat entities carry alternate names
# --------------------------------------------------------------------------- #
def test_create_tag_with_aliases_roundtrips(client, login):
    login("ADMIN", email="admin@example.com")
    resp = client.post(
        "/api/tags",
        json={
            "kind": "ACTOR",
            "label": "APT28",
            "aliases": ["Fancy Bear", "Sofacy", "fancy bear", "APT28"],
        },
    )
    assert resp.status_code == 201, resp.text
    # deduped case-insensitively; the canonical label is dropped as an alias.
    assert resp.json()["aliases"] == ["Fancy Bear", "Sofacy"]


def test_aliases_default_empty(client, login):
    tag = _create_tag(client, login, label="APT29")
    assert tag["aliases"] == []


def test_update_tag_aliases(client, login):
    tag = _create_tag(client, login, kind="ACTOR", label="APT29")
    login("ADMIN", email="admin@example.com")
    out = client.patch(
        f"/api/tags/{tag['id']}", json={"aliases": ["Cozy Bear", "Nobelium"]}
    )
    assert out.status_code == 200
    assert out.json()["aliases"] == ["Cozy Bear", "Nobelium"]


def test_seed_aliases_imported_and_refreshed(engine):
    entries = [
        {"kind": "ACTOR", "label": "APT28", "external_id": "G0007",
         "aliases": ["Fancy Bear"]},
    ]
    with Session(engine) as s:
        seed_default_taxonomy(s, entries)
        tag = s.exec(select(Tag).where(Tag.slug == "apt28")).first()
        assert tag.aliases == ["Fancy Bear"]
        # update=True refreshes the alias list
        entries[0]["aliases"] = ["Fancy Bear", "Sofacy"]
        assert seed_default_taxonomy(s, entries, update=True) == 0
        s.refresh(tag)
        assert tag.aliases == ["Fancy Bear", "Sofacy"]


# --------------------------------------------------------------------------- #
# Starter taxonomy catalog + import step
# --------------------------------------------------------------------------- #
def test_starter_catalog_is_valid():
    entries = load_starter_tags()
    assert len(entries) > 50
    valid_kinds = {k.value for k in TagKind}
    seen = set()
    for e in entries:
        assert e["kind"] in valid_kinds, e
        assert e.get("label"), e
        key = (e["kind"], slugify(e["label"]))
        assert key not in seen, f"duplicate (kind, slug): {key}"
        seen.add(key)


def test_seed_imports_full_catalog_and_is_idempotent(engine):
    expected = len(load_starter_tags())
    with Session(engine) as s:
        first = seed_default_taxonomy(s)
        second = seed_default_taxonomy(s)
    assert first == expected
    assert second == 0


def test_seed_populates_all_kinds(engine):
    with Session(engine) as s:
        seed_default_taxonomy(s)
        kinds = {t.kind for t in s.exec(select(Tag)).all()}
        # spot-check that external_ids came through from the data file
        phishing = s.exec(
            select(Tag).where(Tag.kind == TagKind.TECHNIQUE, Tag.slug == "phishing")
        ).first()
        apt29 = s.exec(
            select(Tag).where(Tag.kind == TagKind.ACTOR, Tag.slug == "apt29")
        ).first()
    # The starter set covers every kind except CAMPAIGN (campaigns are
    # org-specific events, left for admins to create).
    assert kinds == {
        TagKind.SECTOR,
        TagKind.TOPIC,
        TagKind.TECHNIQUE,
        TagKind.ACTOR,
        TagKind.MALWARE,
    }
    assert phishing.external_id == "T1566"
    assert apt29.external_id == "G0016"


def test_seed_custom_entries_and_update(engine):
    entries = [{"kind": "ACTOR", "label": "APT99", "external_id": "G9999", "description": "old"}]
    with Session(engine) as s:
        assert seed_default_taxonomy(s, entries) == 1
        # re-importing without update does not touch the existing row
        entries[0]["description"] = "new"
        assert seed_default_taxonomy(s, entries) == 0
        tag = s.exec(select(Tag).where(Tag.slug == "apt99")).first()
        assert tag.description == "old"
        # update=True refreshes metadata (creating nothing)
        assert seed_default_taxonomy(s, entries, update=True) == 0
        s.refresh(tag)
        assert tag.description == "new"


def test_seed_cli_list_does_not_write(engine, capsys):
    assert seed_cli.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "tag(s)" in out and "TECHNIQUE" in out
    # nothing was written
    with Session(engine) as s:
        assert s.exec(select(Tag)).first() is None


# --------------------------------------------------------------------------- #
# Entity attribution profile (roadmap 2b)
# --------------------------------------------------------------------------- #
def test_normalise_motivations_dedupes_and_drops_unknown():
    out = normalise_motivations(
        ["ESPIONAGE", "espionage", "bogus", "", "financial"]
    )
    assert out == [Motivation.ESPIONAGE, Motivation.FINANCIAL]
    assert normalise_motivations(None) == []


def test_create_tag_with_attribution_roundtrips(client, login):
    login("ADMIN", email="admin@example.com")
    resp = client.post(
        "/api/tags",
        json={
            "kind": "ACTOR",
            "label": "APT28",
            "suspected_attribution": "Russia (GRU)",
            "motivations": ["ESPIONAGE", "INFLUENCE"],
            "first_seen": "2004",
            "last_seen": "present",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["suspected_attribution"] == "Russia (GRU)"
    assert body["motivations"] == ["ESPIONAGE", "INFLUENCE"]
    assert body["first_seen"] == "2004"
    assert body["last_seen"] == "present"


def test_attribution_defaults_empty(client, login):
    tag = _create_tag(client, login, label="APT29")
    assert tag["suspected_attribution"] == ""
    assert tag["motivations"] == []
    assert tag["first_seen"] == "" and tag["last_seen"] == ""


def test_update_and_clear_attribution(client, login):
    tag = _create_tag(client, login, kind="ACTOR", label="APT29")
    login("ADMIN", email="admin@example.com")
    out = client.patch(
        f"/api/tags/{tag['id']}",
        json={
            "suspected_attribution": "Russia (SVR)",
            "motivations": ["ESPIONAGE"],
            "first_seen": "2008",
        },
    )
    assert out.status_code == 200
    assert out.json()["suspected_attribution"] == "Russia (SVR)"
    assert out.json()["motivations"] == ["ESPIONAGE"]
    # clearing: empty list / blank string wipe the fields
    out = client.patch(
        f"/api/tags/{tag['id']}",
        json={"motivations": [], "suspected_attribution": ""},
    )
    assert out.json()["motivations"] == []
    assert out.json()["suspected_attribution"] == ""


def test_seed_attribution_imported_and_refreshed(engine):
    entries = [
        {"kind": "ACTOR", "label": "APT28", "external_id": "G0007",
         "suspected_attribution": "Russia (GRU)", "motivations": ["ESPIONAGE"],
         "first_seen": "2004"},
    ]
    with Session(engine) as s:
        seed_default_taxonomy(s, entries)
        tag = s.exec(select(Tag).where(Tag.slug == "apt28")).first()
        assert tag.suspected_attribution == "Russia (GRU)"
        assert tag.motivations == ["ESPIONAGE"]
        assert tag.first_seen == "2004"
        # update=True refreshes the attribution metadata (creating nothing)
        entries[0]["motivations"] = ["ESPIONAGE", "INFLUENCE"]
        entries[0]["last_seen"] = "present"
        assert seed_default_taxonomy(s, entries, update=True) == 0
        s.refresh(tag)
        assert tag.motivations == ["ESPIONAGE", "INFLUENCE"]
        assert tag.last_seen == "present"


def test_named_threat_tag_renders_entity_profile(client, login):
    login("ADMIN", email="admin@example.com")
    tag = client.post(
        "/api/tags",
        json={
            "kind": "ACTOR",
            "label": "APT28",
            "aliases": ["Fancy Bear"],
            "suspected_attribution": "Russia (GRU)",
            "motivations": ["ESPIONAGE"],
            "first_seen": "2004",
        },
    )
    assert tag.status_code == 201, tag.text
    page = client.get(f"/tags/{tag.json()['id']}")
    assert page.status_code == 200
    html = page.text
    assert "Entity profile" in html
    assert "Attribution" in html
    assert "Russia (GRU)" in html
    assert "Espionage" in html  # motivation chip, Title-cased
    assert "Fancy Bear" in html  # Also known as


def test_plain_tag_keeps_search_drilldown(client, login):
    tag = _create_tag(client, login, kind="SECTOR", label="Energy")
    page = client.get(f"/tags/{tag['id']}")
    assert page.status_code == 200
    # SECTOR is not a named-threat kind: it uses the search results template,
    # not the entity profile.
    assert "Entity profile" not in page.text
    assert "Tagged · Energy" in page.text


# --------------------------------------------------------------------------- #
# Admin taxonomy merges (#185)
# --------------------------------------------------------------------------- #
def test_admin_merge_moves_links_preserves_aliases_and_keeps_lineage(
    client, login, engine
):
    target = _create_tag(client, login, kind="ACTOR", label="Canonical Actor")
    login("ADMIN", email="admin@example.com")
    target = client.patch(
        f"/api/tags/{target['id']}", json={"aliases": ["Existing Alias"]}
    ).json()
    source = client.post(
        "/api/tags",
        json={
            "kind": "ACTOR",
            "label": "Legacy Actor",
            "aliases": ["Legacy Alias", "existing alias"],
        },
    ).json()

    source_only_report = _make_report(client, login, title="Source-only report")
    overlap_report = _make_report(client, login, title="Already canonical report")
    login("ANALYST", email="author@example.com")
    assert client.put(
        f"/api/reports/{source_only_report}/tags", json={"tag_ids": [source["id"]]}
    ).status_code == 200
    assert client.put(
        f"/api/reports/{overlap_report}/tags",
        json={"tag_ids": [source["id"], target["id"]]},
    ).status_code == 200

    # One stakeholder has only the source subscription; another already has the
    # canonical subscription too.  Both cases must merge without a composite-PK
    # duplicate failure.
    login("STAKEHOLDER", email="source-subscriber@example.com")
    assert client.patch(
        "/api/me", json={"subscribed_tag_ids": [source["id"]]}
    ).status_code == 200
    login("STAKEHOLDER", email="overlap-subscriber@example.com")
    assert client.patch(
        "/api/me", json={"subscribed_tag_ids": [source["id"], target["id"]]}
    ).status_code == 200

    login("ADMIN", email="admin@example.com")
    response = client.post(
        f"/api/tags/{source['id']}/merge", json={"target_tag_id": target["id"]}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["report_links_moved"] == 1
    assert result["report_links_deduplicated"] == 1
    assert result["subscriptions_moved"] == 1
    assert result["subscriptions_deduplicated"] == 1
    assert result["target"]["aliases"] == [
        "Existing Alias",
        "Legacy Actor",
        "Legacy Alias",
    ]
    assert result["source"]["active"] is False
    assert result["source"]["merged_into_tag_id"] == target["id"]
    assert result["source"]["merged_at"] is not None

    with Session(engine) as session:
        source_row = session.get(Tag, source["id"])
        assert source_row is not None
        assert source_row.active is False
        assert source_row.merged_into_tag_id == target["id"]
        assert source_row.merged_at is not None
        assert source_row.aliases == ["Legacy Alias", "existing alias"]

        report_links = session.exec(
            select(ReportTag).where(
                ReportTag.report_id.in_([source_only_report, overlap_report])
            )
        ).all()
        assert {(link.report_id, link.tag_id) for link in report_links} == {
            (source_only_report, target["id"]),
            (overlap_report, target["id"]),
        }

        subscription_links = session.exec(select(UserTagSubscription)).all()
        assert all(link.tag_id != source["id"] for link in subscription_links)
        assert sum(link.tag_id == target["id"] for link in subscription_links) == 2

        event = session.exec(
            select(AuditEvent).where(AuditEvent.action == AuditAction.TAG_MERGED)
        ).one()
        assert event.detail["source_tag_id"] == source["id"]
        assert event.detail["target_tag_id"] == target["id"]
        assert event.detail["report_links_deduplicated"] == 1

    # A stale API client cannot reapply a merged source tag to new report or
    # subscription links.
    login("ANALYST", email="author@example.com")
    assert client.put(
        f"/api/reports/{source_only_report}/tags", json={"tag_ids": [source["id"]]}
    ).status_code == 409


def test_merge_rejects_non_admin_cross_kind_self_and_inactive_target(client, login):
    source = _create_tag(client, login, kind="ACTOR", label="Merge source")
    target = _create_tag(client, login, kind="ACTOR", label="Merge target")
    sector = _create_tag(client, login, kind="SECTOR", label="Energy")

    login("ANALYST", email="author@example.com")
    assert client.post(
        f"/api/tags/{source['id']}/merge", json={"target_tag_id": target["id"]}
    ).status_code == 403

    login("ADMIN", email="admin@example.com")
    assert client.post(
        f"/api/tags/{source['id']}/merge", json={"target_tag_id": source["id"]}
    ).status_code == 400
    assert client.post(
        f"/api/tags/{source['id']}/merge", json={"target_tag_id": sector["id"]}
    ).status_code == 400
    assert client.patch(f"/api/tags/{target['id']}", json={"active": False}).status_code == 200
    assert client.post(
        f"/api/tags/{source['id']}/merge", json={"target_tag_id": target["id"]}
    ).status_code == 409


def test_merged_tags_and_their_canonical_target_cannot_be_deleted(client, login, engine):
    source = _create_tag(client, login, kind="ACTOR", label="Preserved source")
    target = _create_tag(client, login, kind="ACTOR", label="Preserved target")
    login("ADMIN", email="admin@example.com")
    assert client.post(
        f"/api/tags/{source['id']}/merge", json={"target_tag_id": target["id"]}
    ).status_code == 200

    with Session(engine) as session:
        before = len(
            session.exec(
                select(AuditEvent).where(AuditEvent.action == AuditAction.TAG_DELETED)
            ).all()
        )
    assert client.delete(f"/api/tags/{source['id']}").status_code == 409
    assert client.delete(f"/api/tags/{target['id']}").status_code == 409
    assert client.patch(f"/api/tags/{source['id']}", json={"active": True}).status_code == 409
    with Session(engine) as session:
        after = len(
            session.exec(
                select(AuditEvent).where(AuditEvent.action == AuditAction.TAG_DELETED)
            ).all()
        )
    assert after == before
