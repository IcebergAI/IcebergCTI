"""External STIX 2.1 schema validation for Iceberg's published bundle."""

import json

from stix2validator import ValidationOptions, validate_string


def _publish(client, login, report_id: int) -> None:
    login("ANALYST", email="author@example.com")
    assert client.post(
        f"/api/reports/{report_id}/transition", json={"target": "IN_REVIEW"}
    ).status_code == 200
    login("REVIEWER", email="reviewer@example.com")
    assert client.post(
        f"/api/reports/{report_id}/transition", json={"target": "APPROVED"}
    ).status_code == 200
    assert client.post(
        f"/api/reports/{report_id}/transition", json={"target": "PUBLISHED"}
    ).status_code == 200


def test_published_bundle_passes_external_stix_21_validator(client, login):
    """Validate each Iceberg STIX object against the OASIS validator schemas.

    The validator's normal mode enforces STIX's mandatory schema requirements.
    Iceberg intentionally uses a documented, deployment-scoped UUIDv5 namespace
    for stable SDO IDs; UUIDv4 is a STIX recommendation rather than a schema
    requirement, so strict best-practice mode is deliberately not enabled here.
    """

    login("ADMIN", email="admin@example.com")
    tag_ids = []
    for kind, label, external_id in (
        ("ACTOR", "Validator Actor", ""),
        ("MALWARE", "Validator Malware", ""),
        ("CAMPAIGN", "Validator Campaign", ""),
        ("TECHNIQUE", "Validator Technique", "T1566"),
        ("SECTOR", "financial-services", ""),
    ):
        response = client.post(
            "/api/tags",
            json={"kind": kind, "label": label, "external_id": external_id},
        )
        assert response.status_code == 201, response.text
        tag_ids.append(response.json()["id"])

    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Interop"}).json()
    report = client.post(
        "/api/reports",
        json={
            "notebook_id": notebook["id"],
            "title": "Validator report",
            "body_md": "Representative finished product.",
        },
    ).json()
    response = client.put(
        f"/api/reports/{report['id']}/tags", json={"tag_ids": tag_ids}
    )
    assert response.status_code == 200, response.text
    _publish(client, login, report["id"])

    response = client.get(f"/api/reports/{report['id']}/stix")
    assert response.status_code == 200, response.text
    _assert_valid(response.json())


def _assert_valid(bundle: dict) -> None:
    result = validate_string(json.dumps(bundle), ValidationOptions(version="2.1"))
    assert result.is_valid, "\n".join(str(error) for error in result.errors)


def test_relationship_and_marking_objects_pass_the_validator(client, login):
    """The richer mapping (#309) — relationships, references, markings, an
    approximated TLP 2.0 marking and a need-to-know statement — must still be
    schema-valid, not merely plausible."""

    login("ADMIN", email="admin@example.com")
    tag_ids = []
    for kind, label, external_id, extra in (
        ("ACTOR", "Interop Actor", "", {
            "aliases": ["Interop Bear"],
            "suspected_attribution": "Testland",
            "motivations": ["ESPIONAGE"],
            "first_seen": "2004",
        }),
        ("MALWARE", "Interop Malware", "", {}),
        ("CAMPAIGN", "Interop Campaign", "", {}),
        ("TECHNIQUE", "Interop Phishing", "T1566", {"attack_tactics": ["Initial Access"]}),
        ("SECTOR", "financial-services", "", {}),
    ):
        response = client.post(
            "/api/tags",
            json={"kind": kind, "label": label, "external_id": external_id, **extra},
        )
        assert response.status_code == 201, response.text
        tag_ids.append(response.json()["id"])
    group = client.post(
        "/api/audience-groups", json={"name": "Interop cohort", "member_user_ids": []}
    ).json()

    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Interop"}).json()
    report = client.post(
        "/api/reports",
        json={
            "notebook_id": notebook["id"],
            "title": "Relationship report",
            "body_md": "Representative finished product.",
            # AMBER+STRICT has no STIX marking, so this also validates the
            # approximated marking plus its statement marking.
            "tlp": "AMBER_STRICT",
        },
    ).json()
    assert client.put(
        f"/api/reports/{report['id']}/tags", json={"tag_ids": tag_ids}
    ).status_code == 200
    login("ADMIN", email="admin@example.com")
    assert client.put(
        f"/api/audience-groups/reports/{report['id']}", json={"group_ids": [group["id"]]}
    ).status_code == 200
    _publish(client, login, report["id"])

    login("ANALYST", email="author@example.com")
    response = client.get(f"/api/reports/{report['id']}/stix")
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert any(obj["type"] == "relationship" for obj in bundle["objects"])
    _assert_valid(bundle)


def test_a_product_with_no_taxonomy_terms_still_validates(client, login):
    """``report.object_refs`` is required and must not be empty, so an untagged
    product still has to reference something — the producer identity."""

    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Interop"}).json()
    report = client.post(
        "/api/reports",
        json={"notebook_id": notebook["id"], "title": "Untagged product"},
    ).json()
    _publish(client, login, report["id"])

    login("ANALYST", email="author@example.com")
    response = client.get(f"/api/reports/{report['id']}/stix")
    assert response.status_code == 200, response.text
    bundle = response.json()
    report_obj = next(obj for obj in bundle["objects"] if obj["type"] == "report")
    assert report_obj["object_refs"]
    _assert_valid(bundle)
