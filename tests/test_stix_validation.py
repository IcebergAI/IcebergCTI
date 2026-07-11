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
    result = validate_string(
        json.dumps(response.json()), ValidationOptions(version="2.1")
    )
    assert result.is_valid, "\n".join(str(error) for error in result.errors)
