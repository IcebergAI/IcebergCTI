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
def test_cooccurring_entities_do_not_become_relationship_assertions(client, login, entities):
    """A report discussing two entities is not a claim that they are related.

    `uses`/`targets` are semantic assertions; Iceberg records none, so deriving
    them from shared tagging would invent claims the analyst never made.
    """

    report = _report(client, login, tag_ids=[e["id"] for e in entities.values()])
    bundle, _ = _bundle(client, login, report["id"])

    verbs = {rel["relationship_type"] for rel in _by_type(bundle, "relationship")}
    assert verbs <= {"attributed-to"}
    assert "uses" not in verbs and "targets" not in verbs

    # The co-occurrence still travels, as the weaker claim it actually is.
    report_obj = _one(bundle, "report")
    by_id = {obj["id"]: obj for obj in bundle["objects"]}
    referenced = {by_id[ref]["type"] for ref in report_obj["object_refs"] if ref in by_id}
    assert {"threat-actor", "malware", "attack-pattern"} <= referenced


def test_co_occurrence_without_a_relationship_is_reported(client, login, entities):
    report = _report(
        client, login, tag_ids=[entities["actor"]["id"], entities["malware"]["id"]]
    )

    codes = _codes(_conformance(client, login, report["id"]))

    assert "relationship_not_inferred" in codes
    assert "relationship_asserted_by_product" not in codes



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


def test_one_id_never_carries_two_representations(client, login, entities):
    """Two products classifying the same entity under different markings.

    An object id must identify one immutable representation. The entity objects
    a product derives are therefore scoped to that product; the objects that are
    genuinely shared — the producer identity and the marking definitions — are
    byte-identical wherever they appear, so the same id is always the same
    content.
    """

    shared = [entities["actor"]["id"], entities["malware"]["id"]]
    green = _report(client, login, tag_ids=shared, tlp="GREEN", title="Green product")
    red = _report(client, login, tag_ids=shared, tlp="RED", title="Red product")
    green_bundle, _ = _bundle(client, login, green["id"])
    red_bundle, _ = _bundle(client, login, red["id"])

    def derived(bundle):
        producer_id = _one(bundle, "report")["created_by_ref"]
        return {
            obj["id"]: obj
            for obj in bundle["objects"]
            if obj["type"] != "marking-definition" and obj["id"] != producer_id
        }

    green_objects, red_objects = derived(green_bundle), derived(red_bundle)
    assert not (set(green_objects) & set(red_objects))

    # ... and the entity is still recognisable across the two products.
    def actor(objects):
        return next(o for o in objects.values() if o["type"] == "threat-actor")

    assert actor(green_objects)["name"] == actor(red_objects)["name"]
    assert (
        actor(green_objects)["external_references"]
        == actor(red_objects)["external_references"]
    )

    # The shared objects are identical, not merely same-id.
    def shared_objects(bundle):
        producer_id = _one(bundle, "report")["created_by_ref"]
        return {
            obj["id"]: obj
            for obj in bundle["objects"]
            if obj["type"] == "marking-definition" or obj["id"] == producer_id
        }

    overlap = set(shared_objects(green_bundle)) & set(shared_objects(red_bundle))
    assert overlap
    for object_id in overlap:
        assert shared_objects(green_bundle)[object_id] == shared_objects(red_bundle)[object_id]


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
        # AMBER+STRICT has no STIX marking; TLP:AMBER would be *less*
        # restrictive, so it exports as TLP:RED.
        ("AMBER_STRICT", "red", True),
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


def test_amber_strict_is_never_exported_as_the_more_permissive_amber(client, login):
    """AMBER+STRICT is recipient-organization-only; STIX TLP:AMBER permits
    onward sharing with clients, so exporting it as AMBER would release a
    control. It over-restricts to TLP:RED instead."""

    report = _report(client, login, tlp="AMBER_STRICT")
    bundle, _ = _bundle(client, login, report["id"])

    served = {m["id"] for m in _by_type(bundle, "marking-definition")}
    assert _TLP_IDS["red"] in served
    assert _TLP_IDS["amber"] not in served
    statements = [
        m for m in _by_type(bundle, "marking-definition")
        if m["definition_type"] == "statement"
    ]
    assert any("AMBER+STRICT" in m["definition"]["statement"] for m in statements)


def test_every_exported_object_carries_the_products_marking(client, login, entities):
    """Every object the product derives is marked with the product's TLP.

    The two exceptions carry no product content and are byte-identical in every
    bundle: the marking definitions themselves, and the producer identity — the
    deployment that made the export. Marking those with one product's TLP would
    give the same id different content in the next product's bundle.
    """

    report = _report(client, login, tag_ids=[e["id"] for e in entities.values()], tlp="AMBER")
    bundle, _ = _bundle(client, login, report["id"])
    producer_id = _one(bundle, "report")["created_by_ref"]

    marked = 0
    for obj in bundle["objects"]:
        if obj["type"] == "marking-definition" or obj["id"] == producer_id:
            assert "object_marking_refs" not in obj, obj["type"]
            continue
        assert _TLP_IDS["amber"] in obj["object_marking_refs"], obj["type"]
        marked += 1
    assert marked > 1


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


def test_taxii_versions_survive_the_same_object_appearing_in_two_products(
    client, login, entities
):
    """The versions endpoint reports what the collection holds, not the first hit.

    The producer identity is repeated verbatim by every product's bundle. Two
    published products therefore both carry it, and the endpoint must answer
    from all of them rather than stopping at the first record it walks.
    """

    shared = [entities["actor"]["id"], entities["malware"]["id"]]
    for title, tlp in (("First product", "GREEN"), ("Second product", "RED")):
        report = _report(client, login, tag_ids=shared, title=title, tlp=tlp)
        _publish(client, login, report["id"])
    login("ANALYST", email="author@example.com")

    served = _objects(client)
    reports = [obj for obj in served if obj["type"] == "report"]
    assert len(reports) == 2
    producer_id = reports[0]["created_by_ref"]
    assert reports[1]["created_by_ref"] == producer_id

    resp = client.get(
        f"/api/taxii2/collections/published-reports/objects/{producer_id}/versions/"
    )
    assert resp.status_code == 200, resp.text
    versions = resp.json()["versions"]
    producer = next(obj for obj in served if obj["id"] == producer_id)
    assert versions == [producer["modified"]]

    # Each product's own report object keeps its own id and its own version.
    for report_obj in reports:
        one = client.get(
            f"/api/taxii2/collections/published-reports/objects/{report_obj['id']}/versions/"
        )
        assert one.status_code == 200
        assert one.json()["versions"] == [report_obj["modified"]]
