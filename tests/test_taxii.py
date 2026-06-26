"""TAXII serving for published STIX report bundles."""

from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from iceberg.models import Report


BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _notebook(client):
    return client.post("/api/notebooks", json={"title": "Interop notebook"}).json()


def _report(client, login, *, title="Published STIX report", body="body"):
    login("ANALYST", email="author@example.com")
    nb = _notebook(client)
    resp = client.post(
        "/api/reports",
        json={
            "notebook_id": nb["id"],
            "title": title,
            "body_md": body,
            "tlp": "AMBER",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _publish(client, login, report_id):
    login("ANALYST", email="author@example.com")
    assert client.post(f"/api/reports/{report_id}/transition", json={"target": "IN_REVIEW"}).status_code == 200
    login("REVIEWER", email="rev@example.com")
    assert client.post(f"/api/reports/{report_id}/transition", json={"target": "APPROVED"}).status_code == 200
    resp = client.post(f"/api/reports/{report_id}/transition", json={"target": "PUBLISHED"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _tag(client, login, label="APT TAXII"):
    login("ADMIN", email="admin@example.com")
    resp = client.post(
        "/api/tags",
        json={"kind": "ACTOR", "label": label, "external_id": "G9999"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _set_published_at(engine, report_id, value):
    with Session(engine) as session:
        report = session.get(Report, report_id)
        assert report
        report.published_at = value
        report.updated_at = value
        session.add(report)
        session.commit()


def _taxii_ts(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _stix_report_object(payload):
    return next(obj for obj in payload["objects"] if obj["type"] == "report")


def test_taxii_requires_auth(client):
    paths = [
        "/api/taxii2/",
        "/api/taxii2/collections/",
        "/api/taxii2/collections/published-reports/",
        "/api/taxii2/collections/published-reports/manifest/",
        "/api/taxii2/collections/published-reports/objects/",
        "/api/taxii2/collections/published-reports/objects/report--missing/",
    ]
    for path in paths:
        assert client.get(path).status_code == 401


def test_taxii_root_and_collection_metadata(client, login):
    login("ANALYST")

    root = client.get("/api/taxii2/")
    assert root.status_code == 200
    assert root.json()["versions"] == ["taxii-2.1"]
    assert root.json()["collections"] == "/api/taxii2/collections/"

    collections = client.get("/api/taxii2/collections/")
    assert collections.status_code == 200
    assert collections.json()["collections"][0]["id"] == "published-reports"
    assert collections.json()["collections"][0]["can_write"] is False

    detail = client.get("/api/taxii2/collections/published-reports/")
    assert detail.status_code == 200
    assert detail.json()["media_types"] == ["application/stix+json;version=2.1"]
    assert client.get("/api/taxii2/collections/nope/").status_code == 404


def test_taxii_manifest_and_objects_include_published_stix_only(client, login):
    tag = _tag(client, login)
    published = _report(client, login, title="Visible TAXII product", body="visible-body")
    draft = _report(client, login, title="Draft TAXII product", body="draft-body")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{published['id']}/tags", json={"tag_ids": [tag["id"]]})
    _publish(client, login, published["id"])

    login("ANALYST", email="author@example.com")
    manifest = client.get("/api/taxii2/collections/published-reports/manifest/")
    assert manifest.status_code == 200
    entries = manifest.json()["objects"]
    assert len(entries) == 2
    assert {
        entry["metadata"]["report_title"] for entry in entries
    } == {"Visible TAXII product"}
    assert {entry["media_type"] for entry in entries} == {
        "application/stix+json;version=2.1"
    }

    objects = client.get("/api/taxii2/collections/published-reports/objects/")
    assert objects.status_code == 200
    payload = objects.json()
    assert payload["more"] is False
    assert {entry["id"] for entry in entries} == {obj["id"] for obj in payload["objects"]}
    names = {obj.get("name") for obj in payload["objects"]}
    assert "Visible TAXII product" in names
    assert "Draft TAXII product" not in names
    assert any(obj["type"] == "threat-actor" for obj in payload["objects"])
    assert draft["id"]


def test_taxii_single_object_fetch(client, login):
    report = _report(client, login, title="Single object product")
    _publish(client, login, report["id"])

    login("ANALYST", email="author@example.com")
    objects = client.get("/api/taxii2/collections/published-reports/objects/").json()
    report_obj = _stix_report_object(objects)

    resp = client.get(
        f"/api/taxii2/collections/published-reports/objects/{report_obj['id']}/"
    )
    assert resp.status_code == 200
    assert resp.json()["objects"] == [report_obj]
    assert client.get(
        "/api/taxii2/collections/published-reports/objects/report--missing/"
    ).status_code == 404


def test_taxii_stakeholder_scope_matches_audience_groups(client, login):
    login("STAKEHOLDER", email="allowed@example.com")
    allowed_id = client.get("/api/me").json()["id"]
    login("STAKEHOLDER", email="blocked@example.com")
    blocked_id = client.get("/api/me").json()["id"]

    login("ADMIN", email="admin@example.com")
    group = client.post(
        "/api/audience-groups",
        json={"name": "TAXII audience", "member_user_ids": [allowed_id]},
    ).json()

    report = _report(client, login, title="Scoped TAXII product", body="scoped-body")
    _publish(client, login, report["id"])
    login("ADMIN", email="admin@example.com")
    client.put(
        f"/api/audience-groups/reports/{report['id']}",
        json={"group_ids": [group["id"]]},
    )

    login("STAKEHOLDER", email="blocked@example.com")
    blocked_objects = client.get("/api/taxii2/collections/published-reports/objects/")
    assert blocked_objects.status_code == 200
    assert "Scoped TAXII product" not in {
        obj.get("name") for obj in blocked_objects.json()["objects"]
    }

    login("STAKEHOLDER", email="allowed@example.com")
    allowed_objects = client.get("/api/taxii2/collections/published-reports/objects/")
    assert "Scoped TAXII product" in {
        obj.get("name") for obj in allowed_objects.json()["objects"]
    }
    assert blocked_id


def test_taxii_added_after_filters_incremental_objects(client, login, engine):
    old_report = _report(client, login, title="Old TAXII product")
    new_report = _report(client, login, title="New TAXII product")
    _publish(client, login, old_report["id"])
    _publish(client, login, new_report["id"])
    _set_published_at(engine, old_report["id"], BASE_TIME)
    _set_published_at(engine, new_report["id"], BASE_TIME + timedelta(days=1))

    login("ANALYST", email="author@example.com")
    resp = client.get(
        "/api/taxii2/collections/published-reports/objects/",
        params={"added_after": _taxii_ts(BASE_TIME)},
    )

    assert resp.status_code == 200
    names = {obj.get("name") for obj in resp.json()["objects"]}
    assert "Old TAXII product" not in names
    assert "New TAXII product" in names


def test_taxii_match_filters_apply_to_objects_and_manifest(client, login):
    tag = _tag(client, login)
    report = _report(client, login, title="Filterable TAXII product")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{report['id']}/tags", json={"tag_ids": [tag["id"]]})
    _publish(client, login, report["id"])

    login("ANALYST", email="author@example.com")
    repeated_type = client.get(
        "/api/taxii2/collections/published-reports/objects/",
        params=[("match[type]", "report"), ("match[type]", "threat-actor")],
    )
    assert repeated_type.status_code == 200
    assert {obj["type"] for obj in repeated_type.json()["objects"]} == {
        "report",
        "threat-actor",
    }

    actor_obj = next(
        obj for obj in repeated_type.json()["objects"] if obj["type"] == "threat-actor"
    )
    by_id = client.get(
        "/api/taxii2/collections/published-reports/objects/",
        params={"match[id]": actor_obj["id"]},
    )
    assert by_id.status_code == 200
    assert by_id.json()["objects"] == [actor_obj]

    manifest = client.get(
        "/api/taxii2/collections/published-reports/manifest/",
        params={"match[type]": "threat-actor"},
    )
    assert manifest.status_code == 200
    assert [entry["id"] for entry in manifest.json()["objects"]] == [actor_obj["id"]]


def test_taxii_paginates_objects_and_manifest(client, login, engine):
    first = _report(client, login, title="First page product")
    second = _report(client, login, title="Second page product")
    _publish(client, login, first["id"])
    _publish(client, login, second["id"])
    _set_published_at(engine, first["id"], BASE_TIME)
    _set_published_at(engine, second["id"], BASE_TIME + timedelta(minutes=1))

    login("ANALYST", email="author@example.com")
    all_objects = client.get("/api/taxii2/collections/published-reports/objects/")
    expected_ids = [obj["id"] for obj in all_objects.json()["objects"]]

    page_one = client.get(
        "/api/taxii2/collections/published-reports/objects/",
        params={"limit": "1"},
    )
    assert page_one.status_code == 200
    assert page_one.json()["more"] is True
    assert len(page_one.json()["objects"]) == 1
    assert page_one.json()["next"]

    page_two = client.get(
        "/api/taxii2/collections/published-reports/objects/",
        params={"limit": "1", "next": page_one.json()["next"]},
    )
    assert page_two.status_code == 200
    assert page_two.json()["more"] is False
    page_ids = [obj["id"] for obj in page_one.json()["objects"]]
    page_ids += [obj["id"] for obj in page_two.json()["objects"]]
    assert page_ids == expected_ids

    manifest_page_one = client.get(
        "/api/taxii2/collections/published-reports/manifest/",
        params={"limit": "1"},
    )
    manifest_page_two = client.get(
        "/api/taxii2/collections/published-reports/manifest/",
        params={"limit": "1", "next": manifest_page_one.json()["next"]},
    )
    manifest_ids = [entry["id"] for entry in manifest_page_one.json()["objects"]]
    manifest_ids += [entry["id"] for entry in manifest_page_two.json()["objects"]]
    assert manifest_ids == expected_ids


def test_taxii_rejects_invalid_query_values(client, login):
    login("ANALYST")

    bad_added_after = client.get(
        "/api/taxii2/collections/published-reports/objects/",
        params={"added_after": "not-a-date"},
    )
    assert bad_added_after.status_code == 400

    bad_next = client.get(
        "/api/taxii2/collections/published-reports/objects/",
        params={"next": "not-a-cursor"},
    )
    assert bad_next.status_code == 400

    bad_limit = client.get(
        "/api/taxii2/collections/published-reports/objects/",
        params={"limit": "0"},
    )
    assert bad_limit.status_code == 422
