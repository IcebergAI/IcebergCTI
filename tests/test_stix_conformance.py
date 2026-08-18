"""STIX 2.1 / TAXII conformance: relationships, references, markings, warnings (#309).

The export is deliberately honest rather than maximal: everything Iceberg models
more precisely than STIX does still leaves the building, but it leaves with a
machine-readable note saying how it was approximated.
"""

import pytest


def _tag(client, login, **body):
    login("ADMIN", email="admin@example.com")
    resp = client.post("/api/tags", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _report(
    client,
    login,
    *,
    tag_ids=(),
    tlp="AMBER",
    title="Interop product",
    analytic_confidence=None,
    **fields,
):
    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Interop"}).json()
    report = client.post(
        "/api/reports",
        json={
            "notebook_id": notebook["id"],
            "title": title,
            "body_md": "Representative finished product.",
            "tlp": tlp,
            **fields,
        },
    )
    assert report.status_code == 201, report.text
    report = report.json()
    if analytic_confidence is not None:
        updated = client.patch(
            f"/api/reports/{report['id']}",
            json={"version": report["version"], "analytic_confidence": analytic_confidence},
        )
        assert updated.status_code == 200, updated.text
        report = updated.json()
    if tag_ids:
        assert client.put(
            f"/api/reports/{report['id']}/tags", json={"tag_ids": list(tag_ids)}
        ).status_code == 200
    return report


def _publish(client, login, report_id):
    login("ANALYST", email="author@example.com")
    client.post(f"/api/reports/{report_id}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{report_id}/transition", json={"target": "APPROVED"})
    resp = client.post(f"/api/reports/{report_id}/transition", json={"target": "PUBLISHED"})
    assert resp.status_code == 200, resp.text


def _bundle(client, login, report_id):
    login("ANALYST", email="author@example.com")
    resp = client.get(f"/api/reports/{report_id}/stix")
    assert resp.status_code == 200, resp.text
    return resp.json(), resp.headers


def _conformance(client, login, report_id):
    login("ANALYST", email="author@example.com")
    resp = client.get(f"/api/reports/{report_id}/stix/conformance")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_type(bundle, stix_type):
    return [obj for obj in bundle["objects"] if obj["type"] == stix_type]


def _one(bundle, stix_type):
    matches = _by_type(bundle, stix_type)
    assert len(matches) == 1, f"expected one {stix_type}, got {len(matches)}"
    return matches[0]


def _codes(payload):
    return {warning["code"] for warning in payload["warnings"]}


@pytest.fixture
def entities(client, login):
    """One tag of every mappable kind, with attribution and ATT&CK detail."""

    return {
        "actor": _tag(
            client, login, kind="ACTOR", label="Conformance Actor",
            aliases=["Conf Bear"], suspected_attribution="Testland (Unit 1)",
            motivations=["ESPIONAGE", "DESTRUCTIVE"], first_seen="2004",
        ),
        "malware": _tag(client, login, kind="MALWARE", label="Conformance Malware"),
        "campaign": _tag(client, login, kind="CAMPAIGN", label="Conformance Campaign"),
        "technique": _tag(
            client, login, kind="TECHNIQUE", label="Phishing", external_id="T1566",
            attack_tactics=["Initial Access"],
        ),
        "sector": _tag(client, login, kind="SECTOR", label="financial-services"),
    }


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #
def test_cooccurring_entities_become_relationship_objects(client, login, entities):
    report = _report(client, login, tag_ids=[e["id"] for e in entities.values()])
    bundle, _ = _bundle(client, login, report["id"])

    by_id = {obj["id"]: obj for obj in bundle["objects"]}
    asserted = {
        (by_id[rel["source_ref"]]["type"], rel["relationship_type"], by_id[rel["target_ref"]]["type"])
        for rel in _by_type(bundle, "relationship")
    }
    assert ("threat-actor", "uses", "malware") in asserted
    assert ("threat-actor", "uses", "attack-pattern") in asserted
    assert ("campaign", "uses", "malware") in asserted
    assert ("malware", "uses", "attack-pattern") in asserted
    assert ("threat-actor", "targets", "identity") in asserted
    # And the report refers to every relationship it asserts.
    report_obj = _one(bundle, "report")
    assert {rel["id"] for rel in _by_type(bundle, "relationship")} <= set(report_obj["object_refs"])


def test_relationships_are_evidenced_by_the_product_and_flagged_as_such(client, login, entities):
    report = _report(
        client, login, tag_ids=[entities["actor"]["id"], entities["malware"]["id"]]
    )
    bundle, _ = _bundle(client, login, report["id"])

    relationship = next(
        rel
        for rel in _by_type(bundle, "relationship")
        if rel["relationship_type"] == "uses"
    )
    assert report["title"] in relationship["description"]
    assert "relationship_asserted_by_product" in _codes(
        _conformance(client, login, report["id"])
    )


def test_a_single_entity_asserts_no_relationship(client, login):
    """Nothing co-occurs, and this actor records no attribution to assert."""

    actor = _tag(client, login, kind="ACTOR", label="Lone actor")
    report = _report(client, login, tag_ids=[actor["id"]])
    bundle, _ = _bundle(client, login, report["id"])

    assert _by_type(bundle, "relationship") == []


def test_suspected_attribution_becomes_an_attributed_to_relationship(client, login, entities):
    report = _report(client, login, tag_ids=[entities["actor"]["id"]])
    bundle, _ = _bundle(client, login, report["id"])

    sponsor = next(
        obj for obj in _by_type(bundle, "identity") if obj["name"] == "Testland (Unit 1)"
    )
    attribution = next(
        rel
        for rel in _by_type(bundle, "relationship")
        if rel["relationship_type"] == "attributed-to"
    )
    assert attribution["source_ref"] == _one(bundle, "threat-actor")["id"]
    assert attribution["target_ref"] == sponsor["id"]
    assert "attribution_is_free_text" in _codes(_conformance(client, login, report["id"]))


def test_identifiers_are_deterministic_across_exports(client, login, entities):
    report = _report(
        client, login, tag_ids=[entities["actor"]["id"], entities["malware"]["id"]]
    )
    first, _ = _bundle(client, login, report["id"])
    second, _ = _bundle(client, login, report["id"])

    assert [obj["id"] for obj in first["objects"]] == [obj["id"] for obj in second["objects"]]
    assert first["id"] == second["id"]
    # A relationship id is derived from its endpoints and verb, so the same
    # assertion is the same object every time it is exported.
    assert len({rel["id"] for rel in _by_type(first, "relationship")}) == len(
        _by_type(first, "relationship")
    )


# --------------------------------------------------------------------------- #
# External references
# --------------------------------------------------------------------------- #
def test_report_carries_a_resolvable_external_reference(client, login):
    report = _report(client, login)
    bundle, _ = _bundle(client, login, report["id"])

    reference = _one(bundle, "report")["external_references"][0]
    assert reference["source_name"] == "iceberg"
    assert reference["external_id"] == str(report["id"])
    assert reference["url"].endswith(f"/reports/{report['id']}")


@pytest.mark.parametrize(
    ("external_id", "expected"),
    [
        ("T1566", "https://attack.mitre.org/techniques/T1566/"),
        ("T1566.001", "https://attack.mitre.org/techniques/T1566/001/"),
    ],
)
def test_technique_reference_links_to_mitre(client, login, external_id, expected):
    tag = _tag(
        client, login, kind="TECHNIQUE", label=f"Tech {external_id}",
        external_id=external_id, attack_tactics=["Initial Access"],
    )
    report = _report(client, login, tag_ids=[tag["id"]])
    bundle, _ = _bundle(client, login, report["id"])

    pattern = _one(bundle, "attack-pattern")
    mitre = next(r for r in pattern["external_references"] if r["source_name"] == "mitre-attack")
    assert mitre["external_id"] == external_id
    assert mitre["url"] == expected
    assert pattern["kill_chain_phases"] == [
        {"kill_chain_name": "mitre-attack", "phase_name": "initial-access"}
    ]


def test_a_technique_without_an_attack_id_is_dropped_with_a_warning(client, login):
    tag = _tag(client, login, kind="TECHNIQUE", label="Unmapped technique")
    report = _report(client, login, tag_ids=[tag["id"]])
    bundle, _ = _bundle(client, login, report["id"])

    assert _by_type(bundle, "attack-pattern") == []
    assert "technique_without_external_id" in _codes(_conformance(client, login, report["id"]))


def test_a_topic_term_has_no_stix_object_and_says_so(client, login):
    tag = _tag(client, login, kind="TOPIC", label="Ransomware trends")
    report = _report(client, login, tag_ids=[tag["id"]])

    assert "unmapped_tag_kind" in _codes(_conformance(client, login, report["id"]))


# --------------------------------------------------------------------------- #
# Markings — an export must never weaken one
# --------------------------------------------------------------------------- #
_TLP_IDS = {
    "white": "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9",
    "green": "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da",
    "amber": "marking-definition--f88d31f6-486f-44da-b317-01333bde0b82",
    "red": "marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed",
}


@pytest.mark.parametrize(
    ("tlp", "level", "lossy"),
    [
        ("CLEAR", "white", True),
        ("GREEN", "green", False),
        ("AMBER", "amber", False),
        ("AMBER_STRICT", "amber", True),
        ("RED", "red", False),
    ],
)
def test_tlp_maps_to_the_well_known_marking_and_reports_any_loss(client, login, tlp, level, lossy):
    report = _report(client, login, tlp=tlp)
    bundle, _ = _bundle(client, login, report["id"])

    markings = _by_type(bundle, "marking-definition")
    tlp_markings = [m for m in markings if m["definition_type"] == "tlp"]
    assert [m["id"] for m in tlp_markings] == [_TLP_IDS[level]]
    assert tlp_markings[0]["definition"] == {"tlp": level}
    assert tlp_markings[0]["name"] == f"TLP:{level.upper()}"

    codes = _codes(_conformance(client, login, report["id"]))
    assert ("tlp_not_representable" in codes) is lossy
    if lossy:
        statements = [m for m in markings if m["definition_type"] == "statement"]
        assert any(tlp.replace("_", "+") in m["definition"]["statement"] for m in statements)


def test_a_restricted_product_never_exports_a_more_permissive_marking(client, login):
    report = _report(client, login, tlp="RED")
    bundle, _ = _bundle(client, login, report["id"])

    served = {m["id"] for m in _by_type(bundle, "marking-definition")}
    assert _TLP_IDS["red"] in served
    assert served.isdisjoint({_TLP_IDS["white"], _TLP_IDS["green"], _TLP_IDS["amber"]})


def test_every_exported_object_carries_the_products_marking(client, login, entities):
    report = _report(client, login, tag_ids=[e["id"] for e in entities.values()], tlp="AMBER")
    bundle, _ = _bundle(client, login, report["id"])

    for obj in bundle["objects"]:
        if obj["type"] == "marking-definition":
            continue
        assert _TLP_IDS["amber"] in obj["object_marking_refs"], obj["type"]


def test_need_to_know_scoping_is_marked_and_flagged_as_unenforceable(client, login):
    report = _report(client, login)
    login("ADMIN", email="admin@example.com")
    group = client.post(
        "/api/audience-groups", json={"name": "Cleared", "member_user_ids": []}
    ).json()
    assert client.put(
        f"/api/audience-groups/reports/{report['id']}", json={"group_ids": [group["id"]]}
    ).status_code == 200

    bundle, _ = _bundle(client, login, report["id"])
    statements = [
        m for m in _by_type(bundle, "marking-definition") if m["definition_type"] == "statement"
    ]
    assert any("Need-to-know" in m["definition"]["statement"] for m in statements)
    assert "audience_restriction_not_enforceable" in _codes(
        _conformance(client, login, report["id"])
    )


# --------------------------------------------------------------------------- #
# Lossy-mapping warnings
# --------------------------------------------------------------------------- #
def test_motivations_are_approximated_or_reported_as_unmapped(client, login, entities):
    report = _report(client, login, tag_ids=[entities["actor"]["id"]])
    bundle, _ = _bundle(client, login, report["id"])

    actor = _one(bundle, "threat-actor")
    assert actor["primary_motivation"] == "organizational-gain"
    # Nothing is dropped: both motivations still travel verbatim as labels.
    assert set(actor["labels"]) == {
        "iceberg:motivation=ESPIONAGE",
        "iceberg:motivation=DESTRUCTIVE",
    }
    codes = _codes(_conformance(client, login, report["id"]))
    assert {"approximate_motivation", "unmapped_motivation"} <= codes


def test_a_fuzzy_seen_marker_is_parsed_or_reported(client, login):
    dated = _tag(client, login, kind="ACTOR", label="Dated actor", first_seen="2004")
    fuzzy = _tag(client, login, kind="ACTOR", label="Fuzzy actor", last_seen="present")
    dated_report = _report(client, login, tag_ids=[dated["id"]], title="Dated")
    fuzzy_report = _report(client, login, tag_ids=[fuzzy["id"]], title="Fuzzy")

    bundle, _ = _bundle(client, login, dated_report["id"])
    assert _one(bundle, "threat-actor")["first_seen"] == "2004-01-01T00:00:00.000Z"
    assert "unparsable_seen_marker" not in _codes(_conformance(client, login, dated_report["id"]))

    bundle, _ = _bundle(client, login, fuzzy_report["id"])
    assert "last_seen" not in _one(bundle, "threat-actor")
    assert "unparsable_seen_marker" in _codes(_conformance(client, login, fuzzy_report["id"]))


def test_inverted_seen_markers_are_dropped_rather_than_exported_invalid(client, login):
    tag = _tag(
        client, login, kind="ACTOR", label="Inverted actor",
        first_seen="2020", last_seen="2010",
    )
    report = _report(client, login, tag_ids=[tag["id"]])
    bundle, _ = _bundle(client, login, report["id"])

    actor = _one(bundle, "threat-actor")
    assert "first_seen" not in actor and "last_seen" not in actor
    assert "inverted_seen_markers" in _codes(_conformance(client, login, report["id"]))


def test_analytic_confidence_maps_to_the_stix_confidence_scale(client, login):
    report = _report(client, login, analytic_confidence="HIGH")
    bundle, _ = _bundle(client, login, report["id"])

    assert _one(bundle, "report")["confidence"] == 85


def test_a_product_without_confidence_omits_the_property(client, login):
    report = _report(client, login)
    bundle, _ = _bundle(client, login, report["id"])

    assert "confidence" not in _one(bundle, "report")


# --------------------------------------------------------------------------- #
# The conformance surface itself
# --------------------------------------------------------------------------- #
def test_the_export_points_at_its_own_conformance_report(client, login, entities):
    report = _report(client, login, tag_ids=[entities["actor"]["id"]], tlp="AMBER_STRICT")
    _, headers = _bundle(client, login, report["id"])

    assert int(headers["X-Iceberg-Stix-Warning-Count"]) > 0
    assert f"/api/reports/{report['id']}/stix/conformance" in headers["Link"]


def test_conformance_reports_counts_and_readable_messages(client, login, entities):
    report = _report(client, login, tag_ids=[e["id"] for e in entities.values()])
    payload = _conformance(client, login, report["id"])

    assert payload["spec_version"] == "2.1"
    assert payload["object_counts"]["report"] == 1
    assert payload["object_counts"]["relationship"] >= 1
    assert payload["object_count"] == sum(payload["object_counts"].values())
    assert payload["round_trip_safe"]["report.title"] == "report.name"
    for warning in payload["warnings"]:
        # Machine-readable code + field, and a sentence a person can act on.
        assert warning["code"] and warning["code"].islower()
        assert len(warning["message"].split()) >= 5
        assert warning["field"]


def test_conformance_respects_report_visibility(client, login):
    report = _report(client, login)
    login("STAKEHOLDER", email="sh@example.com")
    assert client.get(f"/api/reports/{report['id']}/stix/conformance").status_code == 404

    _publish(client, login, report["id"])
    login("STAKEHOLDER", email="sh@example.com")
    assert client.get(f"/api/reports/{report['id']}/stix/conformance").status_code == 200


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #
def test_declared_round_trip_safe_fields_survive_the_export(client, login, entities):
    report = _report(
        client,
        login,
        tag_ids=[entities["actor"]["id"], entities["technique"]["id"]],
        tlp="GREEN",
        intel_level="STRATEGIC",
        analytic_confidence="MODERATE",
        title="Round trip product",
    )
    _publish(client, login, report["id"])
    bundle, _ = _bundle(client, login, report["id"])

    report_obj = _one(bundle, "report")
    assert report_obj["name"] == "Round trip product"
    assert report_obj["published"]
    assert report_obj["confidence"] == 50
    assert set(report_obj["labels"]) == {
        "iceberg:intel-level=STRATEGIC",
        "iceberg:tlp=TLP:GREEN",
        "iceberg:status=PUBLISHED",
    }
    assert _one(bundle, "threat-actor")["aliases"] == ["Conf Bear"]
    assert _one(bundle, "attack-pattern")["external_references"][0]["external_id"] == "T1566"
    reconstructed = {
        "title": report_obj["name"],
        "id": int(report_obj["external_references"][0]["external_id"]),
        "tlp": report_obj["labels"][1].split("=", 1)[1],
    }
    assert reconstructed == {"title": "Round trip product", "id": report["id"], "tlp": "TLP:GREEN"}


# --------------------------------------------------------------------------- #
# TAXII version behaviour
# --------------------------------------------------------------------------- #
def _objects(client, params=None):
    resp = client.get("/api/taxii2/collections/published-reports/objects/", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["objects"]


def test_taxii_version_keywords_all_select_the_single_held_version(client, login):
    report = _report(client, login, title="Versioned product")
    _publish(client, login, report["id"])
    login("ANALYST", email="author@example.com")

    baseline = [obj["id"] for obj in _objects(client)]
    for keyword in ("first", "last", "all"):
        assert [obj["id"] for obj in _objects(client, {"match[version]": keyword})] == baseline


def test_taxii_filters_by_an_exact_version_timestamp(client, login):
    report = _report(client, login, title="Versioned product")
    _publish(client, login, report["id"])
    login("ANALYST", email="author@example.com")

    report_obj = next(obj for obj in _objects(client) if obj["type"] == "report")
    matched = _objects(client, {"match[version]": report_obj["modified"]})
    assert report_obj["id"] in {obj["id"] for obj in matched}
    assert _objects(client, {"match[version]": "1999-01-01T00:00:00.000Z"}) == []


def test_taxii_filters_by_spec_version(client, login):
    report = _report(client, login, title="Versioned product")
    _publish(client, login, report["id"])
    login("ANALYST", email="author@example.com")

    assert _objects(client, {"match[spec_version]": "2.1"})
    assert _objects(client, {"match[spec_version]": "2.0"}) == []


def test_taxii_serves_the_versions_of_one_object(client, login):
    report = _report(client, login, title="Versioned product")
    _publish(client, login, report["id"])
    login("ANALYST", email="author@example.com")

    report_obj = next(obj for obj in _objects(client) if obj["type"] == "report")
    resp = client.get(
        f"/api/taxii2/collections/published-reports/objects/{report_obj['id']}/versions/"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"versions": [report_obj["modified"]], "more": False}

    missing = client.get(
        "/api/taxii2/collections/published-reports/objects/report--nope/versions/"
    )
    assert missing.status_code == 404
