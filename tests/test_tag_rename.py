"""Taxonomy rename: impact preview, conflicts, aliases and redirects (#307)."""

from sqlmodel import Session, select

from iceberg.models import AuditEvent, PublicationSnapshot, Tag
from iceberg.services import tags as tag_service


def _tag(client, login, *, kind="ACTOR", label="Original Actor", **body):
    login("ADMIN", email="admin@example.com")
    resp = client.post("/api/tags", json={"kind": kind, "label": label, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _preview(client, login, tag_id, new_label):
    login("ADMIN", email="admin@example.com")
    resp = client.post(
        f"/api/tags/{tag_id}/rename/preview", json={"new_label": new_label}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rename(client, login, tag_id, new_label):
    login("ADMIN", email="admin@example.com")
    return client.post(f"/api/tags/{tag_id}/rename", json={"new_label": new_label})


def _current(client, login, tag_id):
    """Read a term back through the list endpoint (there is no per-tag GET)."""

    login("ADMIN", email="admin@example.com")
    tags = client.get("/api/tags", params={"include_inactive": "true"}).json()
    return next(item for item in tags if item["id"] == tag_id)


def _report(client, login, *, tag_ids, publish=False, title="Tagged product"):
    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Rename"}).json()
    report = client.post(
        "/api/reports",
        json={"notebook_id": notebook["id"], "title": title, "body_md": "body"},
    ).json()
    assert client.put(
        f"/api/reports/{report['id']}/tags", json={"tag_ids": list(tag_ids)}
    ).status_code == 200
    if publish:
        client.post(f"/api/reports/{report['id']}/transition", json={"target": "IN_REVIEW"})
        login("REVIEWER", email="rev@example.com")
        client.post(f"/api/reports/{report['id']}/transition", json={"target": "APPROVED"})
        assert client.post(
            f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
        ).status_code == 200
    return report


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def test_the_preview_counts_what_a_rename_would_touch(client, login):
    tag = _tag(client, login)
    _report(client, login, tag_ids=[tag["id"]], title="Draft product")
    _report(client, login, tag_ids=[tag["id"]], title="Published product", publish=True)
    login("STAKEHOLDER", email="sh@example.com")
    assert client.patch(
        "/api/me", json={"subscribed_tag_ids": [tag["id"]]}
    ).status_code == 200

    impact = _preview(client, login, tag["id"], "Renamed Actor")

    assert impact["blocked"] is False
    assert impact["draft_reports"] == 1
    assert impact["published_reports"] == 1
    assert impact["frozen_snapshots"] == 1
    assert impact["subscriptions"] == 1
    assert impact["alias_added"] == "Original Actor"
    # Nothing is denormalised, so a rename has no records to rewrite — and
    # therefore no background migration to resume or leave half-applied.
    assert impact["records_to_rewrite"] == 0


def test_the_preview_changes_nothing(client, login):
    tag = _tag(client, login)

    _preview(client, login, tag["id"], "Renamed Actor")

    assert _current(client, login, tag["id"])["label"] == "Original Actor"


def test_a_preview_lists_the_dissemination_rules_that_select_the_term(client, login):
    tag = _tag(client, login)
    login("ADMIN", email="admin@example.com")
    policy = client.post("/api/dissemination-policies", json={"name": "Routing"}).json()
    client.post(
        f"/api/dissemination-policies/{policy['id']}/versions",
        json={
            "rules": [
                {
                    "id": "by-entity",
                    "effect": "LIMIT_TO",
                    "when": {"tag_ids": [tag["id"]]},
                    "audience": {"organisations": ["acme"]},
                }
            ]
        },
    )

    impact = _preview(client, login, tag["id"], "Renamed Actor")

    assert impact["policy_rules"] == ["routing@v1:by-entity"]


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #
def test_renaming_onto_an_existing_term_is_blocked_with_the_reason(client, login):
    first = _tag(client, login, label="First Actor")
    _tag(client, login, label="Second Actor")

    impact = _preview(client, login, first["id"], "Second Actor")

    assert impact["blocked"] is True
    assert "already exists" in impact["conflicts"][0]
    assert "merge into it" in impact["conflicts"][0]
    assert _rename(client, login, first["id"], "Second Actor").status_code == 409


def test_renaming_onto_another_terms_alias_is_blocked(client, login):
    first = _tag(client, login, label="First Actor")
    _tag(client, login, label="Second Actor", aliases=["Shadow Crane"])

    impact = _preview(client, login, first["id"], "Shadow Crane")

    assert impact["blocked"] is True
    assert "already an alias" in impact["conflicts"][0]
    assert "resolve to two entities" in impact["conflicts"][0]


def test_a_blank_name_is_refused(client, login):
    tag = _tag(client, login)

    assert _preview(client, login, tag["id"], "  ")["blocked"] is True


def test_a_merged_term_cannot_be_renamed(client, login):
    source = _tag(client, login, label="Duplicate Actor")
    target = _tag(client, login, label="Canonical Actor")
    login("ADMIN", email="admin@example.com")
    assert client.post(
        f"/api/tags/{source['id']}/merge", json={"target_tag_id": target["id"]}
    ).status_code == 200

    impact = _preview(client, login, source["id"], "Something Else")

    assert impact["blocked"] is True
    assert "merged into another" in impact["conflicts"][0]


# --------------------------------------------------------------------------- #
# Execute
# --------------------------------------------------------------------------- #
def test_a_rename_keeps_the_old_name_as_an_alias(client, login):
    tag = _tag(client, login, label="Original Actor")

    renamed = _rename(client, login, tag["id"], "Renamed Actor")

    assert renamed.status_code == 200, renamed.text
    current = _current(client, login, tag["id"])
    assert current["label"] == "Renamed Actor"
    assert "Original Actor" in current["aliases"]


def test_the_old_name_still_finds_the_entity_in_search(client, login):
    tag = _tag(client, login, label="Original Actor")
    _report(client, login, tag_ids=[tag["id"]], publish=True, title="Tagged product")
    _rename(client, login, tag["id"], "Renamed Actor")

    login("ANALYST", email="author@example.com")
    results = client.get("/api/search", params={"q": "Original Actor"}).json()

    assert "Tagged product" in {item["report"]["title"] for item in results["results"]}


def test_an_old_link_redirects_to_the_entity(client, login):
    tag = _tag(client, login, label="Original Actor")
    _rename(client, login, tag["id"], "Renamed Actor")

    login("ANALYST", email="author@example.com")
    redirect = client.get("/tags/by-name/original-actor", follow_redirects=False)

    assert redirect.status_code == 303
    assert redirect.headers["location"] == f"/tags/{tag['id']}"
    assert client.get("/tags/by-name/never-existed", follow_redirects=False).status_code == 404


def test_renaming_to_the_current_name_is_a_no_op(client, login):
    tag = _tag(client, login, label="Original Actor")

    result = _rename(client, login, tag["id"], "Original Actor").json()

    assert result["unchanged"] is True
    current = _current(client, login, tag["id"])
    assert current["label"] == "Original Actor"
    assert current["aliases"] == []


def test_repeating_a_rename_is_idempotent(client, login):
    tag = _tag(client, login, label="Original Actor")

    _rename(client, login, tag["id"], "Renamed Actor")
    again = _rename(client, login, tag["id"], "Renamed Actor").json()

    assert again["unchanged"] is True
    assert _current(client, login, tag["id"])["aliases"] == ["Original Actor"]


def test_a_published_snapshot_keeps_the_name_it_froze(client, login, engine):
    tag = _tag(client, login, label="Original Actor")
    report = _report(client, login, tag_ids=[tag["id"]], publish=True)

    _rename(client, login, tag["id"], "Renamed Actor")

    with Session(engine) as session:
        snapshot = session.exec(
            select(PublicationSnapshot).where(PublicationSnapshot.report_id == report["id"])
        ).one()
        frozen = {item["label"] for item in snapshot.payload["misp"]["tags"]}
    assert frozen == {"Original Actor"}


def test_a_rename_is_audited_with_both_names(client, login, engine):
    tag = _tag(client, login, label="Original Actor")

    _rename(client, login, tag["id"], "Renamed Actor")

    with Session(engine) as session:
        event = session.exec(
            select(AuditEvent).where(AuditEvent.action == "TAG_RENAMED")
        ).one()
        assert event.detail["previous_label"] == "Original Actor"
        assert event.detail["new_label"] == "Renamed Actor"
        assert event.detail["alias_added"] == "Original Actor"


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #
def test_a_rename_can_be_reverted_before_it_is_relied_on(client, login):
    tag = _tag(client, login, label="Original Actor")
    _rename(client, login, tag["id"], "Renamed Actor")

    login("ADMIN", email="admin@example.com")
    reverted = client.post(
        f"/api/tags/{tag['id']}/rename/undo", json={"previous_label": "Original Actor"}
    )

    assert reverted.status_code == 200, reverted.text
    current = _current(client, login, tag["id"])
    assert current["label"] == "Original Actor"
    assert current["aliases"] == []


def test_undo_refuses_a_name_the_term_never_had(client, login):
    tag = _tag(client, login, label="Original Actor")
    _rename(client, login, tag["id"], "Renamed Actor")

    login("ADMIN", email="admin@example.com")
    refused = client.post(
        f"/api/tags/{tag['id']}/rename/undo", json={"previous_label": "Never Used"}
    )

    assert refused.status_code == 409
    assert "not an alias" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Permissions + portal
# --------------------------------------------------------------------------- #
def test_rename_is_admin_only(client, login):
    tag = _tag(client, login)

    for role, email in (("ANALYST", "author@example.com"), ("REVIEWER", "rev@example.com")):
        login(role, email=email)
        assert client.post(
            f"/api/tags/{tag['id']}/rename/preview", json={"new_label": "x"}
        ).status_code == 403
        assert client.post(
            f"/api/tags/{tag['id']}/rename", json={"new_label": "x"}
        ).status_code == 403


def test_the_portal_previews_before_it_renames(client, login, engine):
    tag = _tag(client, login, label="Original Actor")
    login("ADMIN", email="admin@example.com")

    preview = client.post(
        f"/admin/tags/{tag['id']}/rename",
        data={"new_label": "Renamed Actor"},
        follow_redirects=False,
    )
    assert preview.status_code == 303
    assert "rename=" in preview.headers["location"]
    with Session(engine) as session:
        assert session.get(Tag, tag["id"]).label == "Original Actor"

    page = client.get(preview.headers["location"])
    assert "Renaming to “Renamed Actor”" in page.text
    assert "stays resolvable as an alias" in page.text

    confirmed = client.post(
        f"/admin/tags/{tag['id']}/rename",
        data={"new_label": "Renamed Actor", "confirm": "true"},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    with Session(engine) as session:
        assert session.get(Tag, tag["id"]).label == "Renamed Actor"


def test_the_portal_surfaces_a_conflict_instead_of_renaming(client, login, engine):
    first = _tag(client, login, label="First Actor")
    _tag(client, login, label="Second Actor")
    login("ADMIN", email="admin@example.com")

    blocked = client.post(
        f"/admin/tags/{first['id']}/rename",
        data={"new_label": "Second Actor", "confirm": "true"},
        follow_redirects=False,
    )

    assert blocked.status_code == 303
    assert "error=" in blocked.headers["location"]
    with Session(engine) as session:
        assert session.get(Tag, first["id"]).label == "First Actor"


def test_find_by_identifier_prefers_a_live_term_over_an_alias(client, login, engine):
    """A name reused as another term's current name must win over an alias."""

    first = _tag(client, login, label="Original Actor")
    _rename(client, login, first["id"], "Renamed Actor")
    second = _tag(client, login, kind="MALWARE", label="Original Actor")

    with Session(engine) as session:
        found = tag_service.find_by_identifier(session, "original-actor")
        assert found is not None and found.id == second["id"]
