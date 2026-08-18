"""Versioned dissemination policy: preview, narrowing-only rules, stale sends (#308).

The invariant under test throughout is that a policy can only *remove* recipients
from the set a product's own marking and audience scope already allow.
"""

import pytest
from sqlmodel import Session, select

from iceberg.models import (
    AuditEvent,
    DisseminationEvent,
    DisseminationPolicyVersion,
    PublicationSnapshot,
    Report,
)
from iceberg.services import email as email_service


@pytest.fixture(autouse=True)
def _clear_outbox():
    email_service.OUTBOX.clear()
    yield
    email_service.OUTBOX.clear()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _stakeholder(client, login, email, *, level=None, organisation=""):
    login("STAKEHOLDER", email=email, name=email.split("@")[0])
    body: dict = {}
    if level is not None:
        body["preferred_intel_level"] = level
    if body:
        assert client.patch("/api/me", json=body).status_code == 200
    return client.get("/api/me").json()["id"]


def _group(client, login, name, member_ids):
    login("ADMIN", email="admin@example.com")
    group = client.post(
        "/api/audience-groups", json={"name": name, "member_user_ids": member_ids}
    )
    assert group.status_code == 201, group.text
    return group.json()["id"]


def _approved_report(client, login, *, level="STRATEGIC", tlp="AMBER", group_ids=None):
    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "nb"}).json()
    report = client.post(
        "/api/reports",
        json={
            "notebook_id": notebook["id"],
            "title": "Brief",
            "intel_level": level,
            "tlp": tlp,
        },
    ).json()
    client.post(f"/api/reports/{report['id']}/transition", json={"target": "IN_REVIEW"})
    if group_ids:
        login("ADMIN", email="admin@example.com")
        assert (
            client.put(
                f"/api/audience-groups/reports/{report['id']}",
                json={"group_ids": group_ids},
            ).status_code
            == 200
        )
    login("REVIEWER", email="rev@example.com")
    approved = client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "APPROVED"}
    )
    assert approved.status_code == 200, approved.text
    return report["id"]


def _policy(client, login, rules, *, name="Routing", approve=True, **version):
    login("ADMIN", email="admin@example.com")
    policy = client.post("/api/dissemination-policies", json={"name": name})
    assert policy.status_code == 201, policy.text
    created = client.post(
        f"/api/dissemination-policies/{policy.json()['id']}/versions",
        json={"rules": rules, **version},
    )
    assert created.status_code == 201, created.text
    if approve:
        approved = client.post(
            f"/api/dissemination-policies/versions/{created.json()['id']}/approve"
        )
        assert approved.status_code == 200, approved.text
    return policy.json()["id"], created.json()["id"]


def _next_version(client, login, policy_id, rules, *, approve=True, **version):
    """Draft (and by default approve) the next version of an existing policy."""

    login("ADMIN", email="admin@example.com")
    created = client.post(
        f"/api/dissemination-policies/{policy_id}/versions",
        json={"rules": rules, **version},
    )
    assert created.status_code == 201, created.text
    if approve:
        approved = client.post(
            f"/api/dissemination-policies/versions/{created.json()['id']}/approve"
        )
        assert approved.status_code == 200, approved.text
    return created.json()["id"]


def _versions(client, login, policy_id):
    login("ADMIN", email="admin@example.com")
    listed = client.get("/api/dissemination-policies")
    assert listed.status_code == 200, listed.text
    policy = next(p for p in listed.json() if p["id"] == policy_id)
    return {v["version"]: v for v in policy["versions"]}


def _preview(client, login, report_id, *, params=None):
    login("REVIEWER", email="rev@example.com")
    resp = client.get(
        f"/api/dissemination-policies/reports/{report_id}/preview", params=params
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _names(decisions):
    return sorted(d["display_name"] for d in decisions)


# --------------------------------------------------------------------------- #
# Rule validation — a rule that silently does nothing is the worst outcome
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ([{"id": "r", "effect": "SEND_TO", "audience": {"stakeholder_ids": [1]}}], "effect"),
        ([{"id": "r", "effect": "EXCLUDE", "audience": {}}], "at least one"),
        (
            [{"id": "r", "effect": "EXCLUDE", "when": {"tlp": ["PURPLE"]}, "audience": {"stakeholder_ids": [1]}}],
            "PURPLE",
        ),
        (
            [{"id": "r", "effect": "EXCLUDE", "when": {"colour": ["RED"]}, "audience": {"stakeholder_ids": [1]}}],
            "colour",
        ),
        (
            [
                {"id": "same", "effect": "EXCLUDE", "audience": {"stakeholder_ids": [1]}},
                {"id": "same", "effect": "EXCLUDE", "audience": {"stakeholder_ids": [2]}},
            ],
            "duplicate",
        ),
        ([{"id": "r", "effect": "EXCLUDE", "audience": {"stakeholder_ids": ["me"]}}], "integer ids"),
        (["not-an-object"], "JSON object"),
    ],
)
def test_invalid_rules_are_refused_with_an_actionable_message(client, login, rules, expected):
    login("ADMIN", email="admin@example.com")
    policy = client.post("/api/dissemination-policies", json={"name": "Routing"}).json()
    resp = client.post(
        f"/api/dissemination-policies/{policy['id']}/versions", json={"rules": rules}
    )
    assert resp.status_code == 422, resp.text
    assert expected.lower() in resp.json()["detail"].lower()


def test_a_valid_rule_set_round_trips_normalised(client, login):
    _, version_id = _policy(
        client,
        login,
        [{"id": "exec", "effect": "limit_to", "when": {"tlp": ["amber"]}, "audience": {"audience_group_ids": [1, 1]}}],
        approve=False,
    )
    stored = client.get("/api/dissemination-policies").json()[0]["versions"][0]
    assert stored["rules"] == [
        {
            "id": "exec",
            "description": "",
            "effect": "LIMIT_TO",
            "when": {"tlp": ["AMBER"]},
            "audience": {"audience_group_ids": [1]},
        }
    ]
    assert stored["id"] == version_id


# --------------------------------------------------------------------------- #
# Version lifecycle — an approved version is the publication record
# --------------------------------------------------------------------------- #
def test_approved_version_is_immutable_and_undeletable(client, login):
    _, version_id = _policy(client, login, [])
    edit = client.patch(
        f"/api/dissemination-policies/versions/{version_id}", json={"rules": []}
    )
    assert edit.status_code == 409
    assert "immutable" in edit.json()["detail"]
    assert client.delete(f"/api/dissemination-policies/versions/{version_id}").status_code == 409


def test_only_one_draft_at_a_time_and_drafts_are_deletable(client, login):
    policy_id, version_id = _policy(client, login, [], approve=False)
    second = client.post(f"/api/dissemination-policies/{policy_id}/versions", json={"rules": []})
    assert second.status_code == 409
    assert client.delete(f"/api/dissemination-policies/versions/{version_id}").status_code == 204
    assert client.post(
        f"/api/dissemination-policies/{policy_id}/versions", json={"rules": []}
    ).status_code == 201


def test_versions_number_upwards_from_one(client, login):
    policy_id, version_id = _policy(client, login, [])
    second = client.post(f"/api/dissemination-policies/{policy_id}/versions", json={"rules": []})
    assert second.json()["version"] == 2
    assert second.json()["label"].endswith("@v2")
    assert client.get("/api/dissemination-policies").json()[0]["versions"][1]["id"] == version_id


def test_effective_window_end_must_follow_its_start(client, login):
    login("ADMIN", email="admin@example.com")
    policy = client.post("/api/dissemination-policies", json={"name": "Routing"}).json()
    resp = client.post(
        f"/api/dissemination-policies/{policy['id']}/versions",
        json={
            "rules": [],
            "effective_from": "2026-02-01T00:00:00",
            "effective_to": "2026-01-01T00:00:00",
        },
    )
    assert resp.status_code == 422
    assert "after the effective start" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Resolution matrix
# --------------------------------------------------------------------------- #
def test_without_a_policy_everyone_authorised_receives_and_is_told_so(client, login):
    _stakeholder(client, login, "one@example.com")
    _stakeholder(client, login, "two@example.com", level="TACTICAL")
    report_id = _approved_report(client, login, level="STRATEGIC")

    preview = _preview(client, login, report_id)

    assert _names(preview["included"]) == ["one"]
    assert preview["policy_versions"] == []
    assert [d["reason"] for d in preview["excluded"]] == ["intel_level"]
    assert "TACTICAL products only" in preview["excluded"][0]["explanation"]
    assert any("No approved policy version applies" in w for w in preview["warnings"])


def test_limit_to_rule_narrows_delivery_to_the_named_group(client, login):
    exec_id = _stakeholder(client, login, "exec@example.com")
    _stakeholder(client, login, "desk@example.com")
    group_id = _group(client, login, "Executive", [exec_id])
    _policy(
        client,
        login,
        [
            {
                "id": "exec-only",
                "description": "Strategic products go to the exec cohort",
                "effect": "LIMIT_TO",
                "when": {"intel_level": ["STRATEGIC"]},
                "audience": {"audience_group_ids": [group_id]},
            }
        ],
    )
    report_id = _approved_report(client, login, level="STRATEGIC")

    preview = _preview(client, login, report_id)

    assert _names(preview["included"]) == ["exec"]
    withheld = preview["excluded"][0]
    assert withheld["display_name"] == "desk"
    assert withheld["reason"] == "policy_limit"
    assert withheld["rule"] == "routing@v1:exec-only"
    assert "exec cohort" in withheld["explanation"]


def test_exclude_rule_withholds_from_the_named_stakeholder(client, login):
    blocked = _stakeholder(client, login, "blocked@example.com")
    _stakeholder(client, login, "other@example.com")
    _policy(
        client,
        login,
        [{"id": "no-vendor", "effect": "EXCLUDE", "audience": {"stakeholder_ids": [blocked]}}],
    )
    report_id = _approved_report(client, login)

    preview = _preview(client, login, report_id)

    assert _names(preview["included"]) == ["other"]
    assert preview["excluded"][0]["reason"] == "policy_exclude"


def test_organisation_audience_selector_matches_the_stakeholders_company(client, login, engine):
    member = _stakeholder(client, login, "acme@example.com")
    _stakeholder(client, login, "other@example.com")
    with Session(engine) as session:
        from iceberg.models import User

        user = session.get(User, member)
        user.company_name = "Acme Corp"
        session.add(user)
        session.commit()
    _policy(
        client,
        login,
        [{"id": "acme", "effect": "LIMIT_TO", "audience": {"organisations": ["acme corp"]}}],
    )
    report_id = _approved_report(client, login)

    assert _names(_preview(client, login, report_id)["included"]) == ["acme"]


def test_a_rule_whose_when_clause_misses_the_product_does_not_fire(client, login):
    _stakeholder(client, login, "desk@example.com")
    _policy(
        client,
        login,
        [
            {
                "id": "tactical-only",
                "effect": "EXCLUDE",
                "when": {"intel_level": ["TACTICAL"]},
                "audience": {"organisations": ["anyone"], "stakeholder_ids": [1, 2, 3]},
            }
        ],
    )
    report_id = _approved_report(client, login, level="STRATEGIC")

    assert _names(_preview(client, login, report_id)["included"]) == ["desk"]


def test_a_policy_cannot_add_a_recipient_the_report_scope_excludes(client, login):
    """The narrowing-only invariant: LIMIT_TO is a filter, never a grant."""

    insider = _stakeholder(client, login, "insider@example.com")
    outsider = _stakeholder(client, login, "outsider@example.com")
    scoped = _group(client, login, "Cleared", [insider])
    everyone = _group(client, login, "Everyone", [insider, outsider])
    _policy(
        client,
        login,
        [{"id": "broaden", "effect": "LIMIT_TO", "audience": {"audience_group_ids": [everyone]}}],
    )
    report_id = _approved_report(client, login, group_ids=[scoped])

    preview = _preview(client, login, report_id)

    assert _names(preview["included"]) == ["insider"]
    assert [d["reason"] for d in preview["excluded"]] == ["audience_group"]


def test_a_policy_cannot_release_a_product_above_the_tlp_ceiling(client, login):
    everyone = _stakeholder(client, login, "everyone@example.com")
    _policy(
        client,
        login,
        [{"id": "release", "effect": "LIMIT_TO", "audience": {"stakeholder_ids": [everyone]}}],
    )
    report_id = _approved_report(client, login, tlp="RED")

    preview = _preview(client, login, report_id)

    assert preview["included"] == []
    assert preview["excluded"][0]["reason"] == "tlp_ceiling"


def test_a_draft_or_out_of_window_version_does_not_route(client, login):
    _stakeholder(client, login, "desk@example.com")
    blocking = [{"id": "block", "effect": "LIMIT_TO", "audience": {"organisations": ["nobody"]}}]
    _policy(client, login, blocking, name="Draft policy", approve=False)
    _policy(
        client,
        login,
        [{"id": "later", "effect": "EXCLUDE", "audience": {"stakeholder_ids": [1, 2, 3]}}],
        name="Future policy",
        effective_from="2099-01-01T00:00:00",
    )
    report_id = _approved_report(client, login)

    preview = _preview(client, login, report_id)

    assert _names(preview["included"]) == ["desk"]
    assert preview["policy_versions"] == []


def test_retiring_a_version_stops_it_routing(client, login):
    blocked = _stakeholder(client, login, "blocked@example.com")
    _, version_id = _policy(
        client,
        login,
        [{"id": "no-one", "effect": "EXCLUDE", "audience": {"stakeholder_ids": [blocked]}}],
    )
    report_id = _approved_report(client, login)
    assert _preview(client, login, report_id)["included"] == []

    login("ADMIN", email="admin@example.com")
    assert client.post(
        f"/api/dissemination-policies/versions/{version_id}/retire"
    ).status_code == 200

    assert _names(_preview(client, login, report_id)["included"]) == ["blocked"]


def test_a_rule_naming_a_deleted_group_warns_instead_of_failing_open(client, login):
    _stakeholder(client, login, "desk@example.com")
    _policy(
        client,
        login,
        [{"id": "ghost", "effect": "LIMIT_TO", "audience": {"audience_group_ids": [9999]}}],
    )
    report_id = _approved_report(client, login)

    preview = _preview(client, login, report_id)

    assert preview["included"] == []
    assert any("no longer exist" in warning for warning in preview["warnings"])


# --------------------------------------------------------------------------- #
# Publishing: confirmation, exceptions, and the immutable record
# --------------------------------------------------------------------------- #
def _publish(client, login, report_id, **body):
    login("REVIEWER", email="rev@example.com")
    return client.post(
        f"/api/reports/{report_id}/transition", json={"target": "PUBLISHED", **body}
    )


def test_publishing_with_a_stale_preview_is_refused(client, login):
    _stakeholder(client, login, "first@example.com")
    report_id = _approved_report(client, login)
    fingerprint = _preview(client, login, report_id)["fingerprint"]

    # A second stakeholder appears between preview and send.
    _stakeholder(client, login, "late@example.com")

    refused = _publish(client, login, report_id, audience_fingerprint=fingerprint)
    assert refused.status_code == 409
    assert "changed since it was previewed" in refused.json()["detail"]


def test_publishing_with_a_current_preview_succeeds(client, login):
    _stakeholder(client, login, "first@example.com")
    report_id = _approved_report(client, login)
    fingerprint = _preview(client, login, report_id)["fingerprint"]

    published = _publish(client, login, report_id, audience_fingerprint=fingerprint)

    assert published.status_code == 200
    assert published.json()["status"] == "PUBLISHED"


def test_membership_change_alone_invalidates_a_preview(client, login):
    member = _stakeholder(client, login, "member@example.com")
    spare = _stakeholder(client, login, "spare@example.com")
    group_id = _group(client, login, "Cohort", [member])
    _policy(
        client,
        login,
        [{"id": "cohort", "effect": "LIMIT_TO", "audience": {"audience_group_ids": [group_id]}}],
    )
    report_id = _approved_report(client, login)
    fingerprint = _preview(client, login, report_id)["fingerprint"]

    login("ADMIN", email="admin@example.com")
    assert client.put(
        f"/api/audience-groups/{group_id}/members", json={"member_user_ids": [member, spare]}
    ).status_code == 200

    refused = _publish(client, login, report_id, audience_fingerprint=fingerprint)
    assert refused.status_code == 409


def test_an_exception_withholds_delivery_and_is_recorded(client, login, engine):
    withheld = _stakeholder(client, login, "withheld@example.com")
    _stakeholder(client, login, "kept@example.com")
    report_id = _approved_report(client, login)
    preview = _preview(client, login, report_id, params={"exception": [withheld]})
    assert _names(preview["included"]) == ["kept"]
    assert preview["excluded"][0]["reason"] == "exception"

    published = _publish(
        client,
        login,
        report_id,
        audience_fingerprint=preview["fingerprint"],
        audience_exceptions=[withheld],
    )
    assert published.status_code == 200

    with Session(engine) as session:
        delivered = session.exec(
            select(DisseminationEvent).where(DisseminationEvent.report_id == report_id)
        ).all()
        assert [event.stakeholder_id for event in delivered] != [withheld]
        assert len(delivered) == 1
        snapshot = session.exec(
            select(PublicationSnapshot).where(PublicationSnapshot.report_id == report_id)
        ).one()
        record = snapshot.payload["audience"]
        assert record["exceptions"] == [withheld]
        assert [d["display_name"] for d in record["included"]] == ["kept"]
        assert record["fingerprint"] == preview["fingerprint"]


def test_publication_freezes_the_policy_versions_that_routed_it(client, login, engine):
    keeper = _stakeholder(client, login, "keeper@example.com")
    _stakeholder(client, login, "dropped@example.com")
    _policy(
        client,
        login,
        [{"id": "keeper", "effect": "LIMIT_TO", "audience": {"stakeholder_ids": [keeper]}}],
        name="Exec routing",
    )
    report_id = _approved_report(client, login)
    assert _publish(client, login, report_id).status_code == 200

    with Session(engine) as session:
        snapshot = session.exec(
            select(PublicationSnapshot).where(PublicationSnapshot.report_id == report_id)
        ).one()
        assert snapshot.payload["audience"]["policy_versions"] == ["exec-routing@v1"]
        event = session.exec(
            select(AuditEvent)
            .where(AuditEvent.action == "REPORT_PUBLISHED")
            .where(AuditEvent.resource_id == report_id)
        ).one()
        assert event.detail["policy_versions"] == ["exec-routing@v1"]
        assert event.detail["recipients"] == 1
        assert event.detail["withheld"] == 1


def test_a_retired_policy_does_not_rewrite_a_published_record(client, login, engine):
    keeper = _stakeholder(client, login, "keeper@example.com")
    _, version_id = _policy(
        client,
        login,
        [{"id": "keeper", "effect": "LIMIT_TO", "audience": {"stakeholder_ids": [keeper]}}],
    )
    report_id = _approved_report(client, login)
    assert _publish(client, login, report_id).status_code == 200

    login("ADMIN", email="admin@example.com")
    client.post(f"/api/dissemination-policies/versions/{version_id}/retire")

    with Session(engine) as session:
        snapshot = session.exec(
            select(PublicationSnapshot).where(PublicationSnapshot.report_id == report_id)
        ).one()
        assert snapshot.payload["audience"]["policy_versions"] == ["routing@v1"]


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def test_preview_and_exceptions_are_publisher_only(client, login):
    report_id = _approved_report(client, login)
    for role, email in (("ANALYST", "author@example.com"), ("STAKEHOLDER", "sh@example.com")):
        login(role, email=email)
        denied = client.get(f"/api/dissemination-policies/reports/{report_id}/preview")
        assert denied.status_code == 403, role


def test_policy_administration_is_admin_only(client, login):
    login("REVIEWER", email="rev@example.com")
    assert client.get("/api/dissemination-policies").status_code == 403
    assert client.post("/api/dissemination-policies", json={"name": "x"}).status_code == 403


def test_portal_audience_page_is_publisher_only(client, login):
    report_id = _approved_report(client, login)
    login("REVIEWER", email="rev@example.com")
    page = client.get(f"/reports/{report_id}/audience")
    assert page.status_code == 200
    assert "Who receives this product" in page.text

    login("ANALYST", email="author@example.com")
    assert client.get(f"/reports/{report_id}/audience").status_code == 403


def test_portal_publish_carries_the_reviewed_fingerprint_and_exceptions(client, login, engine):
    """The audience page posts to the portal transition route, not the JSON API."""

    _stakeholder(client, login, "first@example.com")
    withheld = _stakeholder(client, login, "second@example.com")
    report_id = _approved_report(client, login)
    fingerprint = _preview(client, login, report_id, params={"exception": [withheld]})[
        "fingerprint"
    ]

    _stakeholder(client, login, "late@example.com")
    login("REVIEWER", email="rev@example.com")
    stale = client.post(
        f"/reports/{report_id}/transition",
        data={
            "target": "PUBLISHED",
            "audience_fingerprint": fingerprint,
            "audience_exceptions": [withheld],
        },
    )
    assert stale.status_code == 409
    assert "changed since it was previewed" in stale.json()["detail"]

    current = _preview(client, login, report_id, params={"exception": [withheld]})[
        "fingerprint"
    ]
    login("REVIEWER", email="rev@example.com")
    published = client.post(
        f"/reports/{report_id}/transition",
        data={
            "target": "PUBLISHED",
            "audience_fingerprint": current,
            "audience_exceptions": [withheld],
        },
        follow_redirects=False,
    )
    assert published.status_code in (302, 303), published.text

    with Session(engine) as session:
        delivered = session.exec(
            select(DisseminationEvent).where(DisseminationEvent.report_id == report_id)
        ).all()
        assert withheld not in {event.stakeholder_id for event in delivered}
        snapshot = session.exec(
            select(PublicationSnapshot).where(PublicationSnapshot.report_id == report_id)
        ).one()
        assert snapshot.payload["audience"]["exceptions"] == [withheld]


def test_portal_admin_console_renders_and_is_admin_only(client, login):
    login("ADMIN", email="admin@example.com")
    page = client.get("/admin/dissemination-policy")
    assert page.status_code == 200
    assert "Dissemination policy" in page.text

    login("REVIEWER", email="rev@example.com")
    assert client.get("/admin/dissemination-policy").status_code == 403


def test_portal_rejects_an_invalid_rule_set_with_the_same_message(client, login):
    login("ADMIN", email="admin@example.com")
    policy = client.post("/api/dissemination-policies", json={"name": "Routing"}).json()
    resp = client.post(
        f"/admin/dissemination-policy/{policy['id']}/versions",
        data={"rules": "{not json"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "not%20valid%20JSON" in resp.headers["location"]


def test_report_status_is_unchanged_by_a_refused_publish(client, login, engine):
    _stakeholder(client, login, "first@example.com")
    report_id = _approved_report(client, login)
    fingerprint = _preview(client, login, report_id)["fingerprint"]
    _stakeholder(client, login, "late@example.com")

    assert _publish(client, login, report_id, audience_fingerprint=fingerprint).status_code == 409

    with Session(engine) as session:
        assert str(session.get(Report, report_id).status) == "APPROVED"
        assert (
            session.exec(
                select(PublicationSnapshot).where(PublicationSnapshot.report_id == report_id)
            ).first()
            is None
        )


# --------------------------------------------------------------------------- #
# Version replacement — one policy carries exactly one live rule set
# --------------------------------------------------------------------------- #
def test_approving_a_replacement_version_stops_the_one_it_replaces(client, login):
    """A rewrite must be able to loosen the rule it was drafted to loosen.

    v1 and its open-ended replacement v2 would otherwise both be applicable, and
    because rules only ever subtract, the stale restriction would survive forever.
    """

    exec_id = _stakeholder(client, login, "exec@example.com")
    _stakeholder(client, login, "desk@example.com")
    group_id = _group(client, login, "Executive", [exec_id])
    policy_id, _ = _policy(
        client,
        login,
        [{"id": "exec-only", "effect": "LIMIT_TO", "audience": {"audience_group_ids": [group_id]}}],
    )
    report_id = _approved_report(client, login)
    assert _names(_preview(client, login, report_id)["included"]) == ["exec"]

    _next_version(
        client,
        login,
        policy_id,
        [{"id": "exec-only", "effect": "LIMIT_TO", "audience": {"audience_group_ids": [group_id]}}]
        + [{"id": "widen", "effect": "EXCLUDE", "audience": {"organisations": ["nobody"]}}],
    )
    # v2 keeps the same restriction, so this only proves v2 is the one routing.
    preview = _preview(client, login, report_id)
    assert preview["policy_versions"] == ["routing@v2"]

    _next_version(client, login, policy_id, [])

    preview = _preview(client, login, report_id)
    assert _names(preview["included"]) == ["desk", "exec"]
    assert preview["policy_versions"] == ["routing@v3"]


def test_a_replacement_closes_the_previous_versions_effective_window(client, login):
    policy_id, _ = _policy(client, login, [])
    _next_version(client, login, policy_id, [])

    listed = _versions(client, login, policy_id)

    assert listed[1]["effective_to"] is not None
    assert listed[2]["effective_to"] is None


def test_a_future_dated_replacement_leaves_the_current_version_live_until_it_starts(
    client, login
):
    blocked = _stakeholder(client, login, "blocked@example.com")
    policy_id, _ = _policy(
        client,
        login,
        [{"id": "no-vendor", "effect": "EXCLUDE", "audience": {"stakeholder_ids": [blocked]}}],
    )
    report_id = _approved_report(client, login)

    _next_version(client, login, policy_id, [], effective_from="2099-01-01T00:00:00")

    preview = _preview(client, login, report_id)
    assert preview["included"] == []
    assert preview["policy_versions"] == ["routing@v1"]
    assert _versions(client, login, policy_id)[1]["effective_to"].startswith("2099-01-01")


def test_a_retired_version_is_not_reopened_by_a_later_approval(client, login):
    policy_id, first = _policy(client, login, [])
    login("ADMIN", email="admin@example.com")
    retired = client.post(f"/api/dissemination-policies/versions/{first}/retire")
    assert retired.status_code == 200, retired.text
    closed_at = retired.json()["effective_to"]

    _next_version(client, login, policy_id, [])

    assert _versions(client, login, policy_id)[1]["effective_to"] == closed_at


def test_only_the_newest_applicable_version_of_a_policy_routes(client, login, engine):
    """Defence in depth for windows widened by hand after a replacement landed."""

    blocked = _stakeholder(client, login, "blocked@example.com")
    policy_id, first = _policy(
        client,
        login,
        [{"id": "no-vendor", "effect": "EXCLUDE", "audience": {"stakeholder_ids": [blocked]}}],
    )
    _next_version(client, login, policy_id, [])
    report_id = _approved_report(client, login)

    with Session(engine) as session:
        stale = session.get(DisseminationPolicyVersion, first)
        stale.effective_to = None
        session.add(stale)
        session.commit()

    preview = _preview(client, login, report_id)

    assert _names(preview["included"]) == ["blocked"]
    assert preview["policy_versions"] == ["routing@v2"]


def test_a_replacement_does_not_disturb_another_policys_versions(client, login):
    blocked = _stakeholder(client, login, "blocked@example.com")
    _policy(
        client,
        login,
        [{"id": "no-vendor", "effect": "EXCLUDE", "audience": {"stakeholder_ids": [blocked]}}],
        name="Vendor routing",
    )
    other_id, _ = _policy(client, login, [], name="Second routing")
    report_id = _approved_report(client, login)

    _next_version(client, login, other_id, [])

    preview = _preview(client, login, report_id)
    assert preview["included"] == []
    assert preview["policy_versions"] == ["second-routing@v2", "vendor-routing@v1"]
