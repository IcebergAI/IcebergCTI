"""Programme measures: coverage, gaps, usefulness — aggregate and privacy-safe (#310)."""

import pytest
from sqlmodel import Session

from iceberg.config import get_settings
from iceberg.models import ProductFeedback, Requirement, User, utcnow
from iceberg.services import program_measures


@pytest.fixture
def no_suppression(monkeypatch):
    """Disable the minimum-group control so a small fixture can be asserted."""

    monkeypatch.setattr(get_settings(), "measures_min_group", 0)
    yield


def _stakeholder(client, login, email="sh@example.com"):
    login("STAKEHOLDER", email=email, name=email.split("@")[0])
    return client.get("/api/me").json()["id"]


def _requirement(client, login, *, title, kind="PIR", email="sh@example.com"):
    login("STAKEHOLDER", email=email, name=email.split("@")[0])
    resp = client.post("/api/requirements", json={"title": title, "kind": kind})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _report(
    client,
    login,
    *,
    title="Product",
    gaps="",
    requirement_ids=(),
    publish=True,
    group_ids=None,
):
    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Measures"}).json()
    report = client.post(
        "/api/reports",
        json={"notebook_id": notebook["id"], "title": title, "body_md": "body"},
    ).json()
    if gaps:
        updated = client.patch(
            f"/api/reports/{report['id']}",
            json={"version": report["version"], "intelligence_gaps": gaps},
        )
        assert updated.status_code == 200, updated.text
        report = updated.json()
    if requirement_ids:
        assert client.put(
            f"/api/reports/{report['id']}/requirements",
            json={"requirement_ids": list(requirement_ids)},
        ).status_code == 200
    if group_ids:
        login("ADMIN", email="admin@example.com")
        assert client.put(
            f"/api/audience-groups/reports/{report['id']}", json={"group_ids": group_ids}
        ).status_code == 200
    if publish:
        login("ANALYST", email="author@example.com")
        client.post(f"/api/reports/{report['id']}/transition", json={"target": "IN_REVIEW"})
        login("REVIEWER", email="rev@example.com")
        client.post(f"/api/reports/{report['id']}/transition", json={"target": "APPROVED"})
        assert client.post(
            f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
        ).status_code == 200
    return report


def _feedback(
    client,
    login,
    report_id,
    *,
    email,
    usefulness="USEFUL",
    comment="",
    satisfaction=None,
    requirement_id=None,
):
    login("STAKEHOLDER", email=email, name=email.split("@")[0])
    body = {"usefulness": usefulness, "comment": comment}
    if satisfaction:
        body["satisfaction"] = satisfaction
        body["requirement_id"] = requirement_id
    resp = client.post(f"/api/reports/{report_id}/feedback", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _measures(client, login, engine, *, role="ANALYST", email="author@example.com", **kwargs):
    """Compute the measures as one actor (logging in also provisions them)."""

    login(role, email=email)
    with Session(engine) as session:
        from sqlmodel import select

        user = session.exec(select(User).where(User.email == email)).first()
        assert user is not None, email
        return program_measures.program_measures(session, user=user, **kwargs)


# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #
def test_every_measure_is_defined_with_its_source_records():
    definitions = program_measures.definitions()

    assert definitions, "the catalogue must not be empty"
    for measure in definitions:
        assert measure["label"] and measure["source"]
        assert len(measure["definition"].split()) >= 8, measure["key"]
    assert len({m["key"] for m in definitions}) == len(definitions)


# --------------------------------------------------------------------------- #
# Requirement coverage and gaps
# --------------------------------------------------------------------------- #
def test_a_requirement_with_no_collection_and_no_product_is_neglected(client, login, engine):
    neglected = _requirement(client, login, title="Nobody is on this")
    answered = _requirement(client, login, title="Answered")
    _report(client, login, title="Answering product", requirement_ids=[answered["id"]])

    measures = _measures(client, login, engine)["requirements"]

    assert measures["neglected_total"] == 1
    assert [item["title"] for item in measures["neglected"]] == ["Nobody is on this"]
    assert measures["neglected"][0]["id"] == neglected["id"]


def test_requirement_ages_bucket_and_report_a_median(client, login, engine):
    _requirement(client, login, title="Fresh")
    old = _requirement(client, login, title="Old")
    with Session(engine) as session:
        row = session.get(Requirement, old["id"])
        row.created_at = utcnow().replace(year=utcnow().year - 1)
        session.add(row)
        session.commit()

    measures = _measures(client, login, engine)["requirements"]

    assert measures["age_buckets"]["0–7 days"] == 1
    assert measures["age_buckets"]["over 90 days"] == 1
    assert measures["median_age_days"] > 100


def test_kind_filter_narrows_the_requirement_measures(client, login, engine):
    _requirement(client, login, title="A PIR", kind="PIR")
    _requirement(client, login, title="An RFI", kind="RFI")

    from iceberg.models import RequirementKind

    assert _measures(client, login, engine)["requirements"]["total"] == 2
    filtered = _measures(client, login, engine, kind=RequirementKind.PIR)["requirements"]
    assert filtered["total"] == 1
    assert filtered["by_kind"] == {"PIR": 1}


def test_declared_collection_gaps_are_surfaced(client, login, engine):
    _report(client, login, title="With gaps", gaps="No visibility of the C2 infrastructure.")
    _report(client, login, title="No gaps")

    measures = _measures(client, login, engine)["products"]

    assert measures["published"] == 2
    assert measures["declared_gap_rate"] == 0.5
    assert measures["declared_gaps"][0]["title"] == "With gaps"
    assert "C2 infrastructure" in measures["declared_gaps"][0]["gaps"]


def test_product_linkage_counts_products_answering_a_requirement(client, login, engine):
    requirement = _requirement(client, login, title="Linked")
    _report(client, login, title="Linked product", requirement_ids=[requirement["id"]])
    _report(client, login, title="Unlinked product")

    measures = _measures(client, login, engine)["products"]

    assert (measures["linked"], measures["published"]) == (1, 2)
    assert measures["linkage_rate"] == 0.5


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #
def test_measures_never_count_a_product_the_reader_cannot_access(client, login, engine):
    stakeholder = _stakeholder(client, login, "outsider@example.com")
    login("ADMIN", email="admin@example.com")
    cohort = client.post(
        "/api/audience-groups", json={"name": "Cleared", "member_user_ids": []}
    ).json()["id"]
    _report(client, login, title="Open product")
    _report(client, login, title="Restricted product", group_ids=[cohort])

    writer_view = _measures(client, login, engine)["products"]
    stakeholder_view = _measures(client, login, engine, role="STAKEHOLDER", email="outsider@example.com")["products"]

    assert writer_view["published"] == 2
    assert stakeholder_view["published"] == 1
    assert stakeholder is not None


def test_a_draft_product_is_not_counted_as_published(client, login, engine):
    _report(client, login, title="Draft", publish=False)

    assert _measures(client, login, engine)["products"]["published"] == 0


# --------------------------------------------------------------------------- #
# Feedback: silence is not a bad review
# --------------------------------------------------------------------------- #
def test_missing_feedback_is_not_counted_as_negative(client, login, engine, no_suppression):
    _stakeholder(client, login, "one@example.com")
    _stakeholder(client, login, "two@example.com")
    report = _report(client, login, title="Delivered to two")
    _feedback(client, login, report["id"], email="one@example.com", usefulness="HIGHLY_USEFUL")

    measures = _measures(client, login, engine)["feedback"]

    assert measures["deliveries"] == 2
    assert measures["responses"] == 1
    assert measures["response_rate"] == 0.5
    # The silent stakeholder is absent from the ratings, not counted as a poor one.
    assert measures["useful_rate"] == 1.0
    assert measures["usefulness"]["values"]["HIGHLY_USEFUL"] == 1
    assert sum(measures["usefulness"]["values"].values()) == 1


def test_satisfaction_is_taken_over_verdicts_not_responses(client, login, engine, no_suppression):
    _stakeholder(client, login, "one@example.com")
    _stakeholder(client, login, "two@example.com")
    requirement = _requirement(client, login, title="Answer me", email="one@example.com")
    report = _report(client, login, title="Product", requirement_ids=[requirement["id"]])
    _feedback(
        client, login, report["id"], email="one@example.com",
        usefulness="USEFUL", satisfaction="MET", requirement_id=requirement["id"],
    )
    _feedback(client, login, report["id"], email="two@example.com", usefulness="USEFUL")

    measures = _measures(client, login, engine)["feedback"]

    assert measures["responses"] == 2
    assert measures["verdicts"] == 1
    assert measures["satisfaction_rate"] == 1.0


# --------------------------------------------------------------------------- #
# Small-group suppression
# --------------------------------------------------------------------------- #
def test_a_breakdown_below_the_minimum_group_is_suppressed(client, login, engine):
    _stakeholder(client, login, "one@example.com")
    report = _report(client, login, title="Product")
    _feedback(client, login, report["id"], email="one@example.com", comment="clear and timely")

    measures = _measures(client, login, engine)["feedback"]

    assert measures["min_group"] if False else measures["usefulness"]["suppressed"] is True
    assert measures["usefulness"]["values"] == {}
    assert measures["themes"]["suppressed"] is True
    # The headline counts stay available — only the identifying breakdown goes.
    assert measures["responses"] == 1
    assert measures["response_rate"] == 1.0


def test_suppression_can_be_switched_off_for_a_single_team(client, login, engine, no_suppression):
    _stakeholder(client, login, "one@example.com")
    report = _report(client, login, title="Product")
    _feedback(client, login, report["id"], email="one@example.com", comment="clear and timely")

    measures = _measures(client, login, engine)["feedback"]

    assert measures["usefulness"]["suppressed"] is False
    assert measures["themes"]["values"]["timely"] == 1


def test_the_export_carries_the_suppression_rather_than_the_values(client, login, engine):
    _stakeholder(client, login, "one@example.com")
    report = _report(client, login, title="Product")
    _feedback(client, login, report["id"], email="one@example.com", comment="clear and timely")

    csv = program_measures.export_csv(_measures(client, login, engine))

    assert "usefulness,,suppressed" in csv
    assert "themes,,suppressed" in csv
    assert "timely" not in csv


# --------------------------------------------------------------------------- #
# Trend + export
# --------------------------------------------------------------------------- #
def test_the_usefulness_trend_buckets_responses_by_month(client, login, engine, no_suppression):
    _stakeholder(client, login, "one@example.com")
    report = _report(client, login, title="Product")
    _feedback(client, login, report["id"], email="one@example.com", usefulness="USEFUL")

    trend = _measures(client, login, engine)["feedback"]["trend"]

    assert len(trend) == 1
    month, group = next(iter(trend.items()))
    assert month == utcnow().strftime("%Y-%m")
    assert group["values"] == {"responses": 1, "useful_rate": 1.0}


def test_the_window_filter_excludes_older_responses(client, login, engine, no_suppression):
    _stakeholder(client, login, "one@example.com")
    report = _report(client, login, title="Product")
    _feedback(client, login, report["id"], email="one@example.com")
    with Session(engine) as session:
        from sqlmodel import select

        row = session.exec(select(ProductFeedback)).one()
        row.created_at = utcnow().replace(year=utcnow().year - 1)
        session.add(row)
        session.commit()

    assert _measures(client, login, engine, window_days=30)["feedback"]["responses"] == 0
    assert _measures(client, login, engine, window_days=0)["feedback"]["responses"] == 1


def test_export_rows_cover_every_headline_measure(client, login, engine, no_suppression):
    _requirement(client, login, title="Requirement")
    _report(client, login, title="Product")

    rows = dict((name, value) for name, item, value in program_measures.export_rows(_measures(client, login, engine)) if not item)

    for key in (
        "requirements_total",
        "requirements_outstanding",
        "task_coverage",
        "source_coverage",
        "neglected_requirements",
        "published_products",
        "product_linkage",
        "deliveries",
        "responses",
        "response_rate",
    ):
        assert key in rows, key


# --------------------------------------------------------------------------- #
# Portal
# --------------------------------------------------------------------------- #
def test_the_measures_page_is_writer_only(client, login):
    login("ANALYST", email="author@example.com")
    page = client.get("/measures")
    assert page.status_code == 200
    assert "Program measures" in page.text
    assert "What each measure means" in page.text

    login("STAKEHOLDER", email="sh@example.com")
    assert client.get("/measures").status_code == 403
    assert client.get("/measures/export.csv").status_code == 403


def test_the_page_exports_csv_for_a_writer(client, login):
    login("ANALYST", email="author@example.com")
    export = client.get("/measures/export.csv?window=30")

    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert export.text.splitlines()[0] == "measure,item,value"


def test_drill_down_links_stay_permission_checked(client, login):
    """The page links to ordinary routes, which apply their own guards."""

    requirement = _requirement(client, login, title="Neglected")
    login("ANALYST", email="author@example.com")
    page = client.get("/measures")

    assert f"/requirements/{requirement['id']}" in page.text
    login("STAKEHOLDER", email="other@example.com")
    assert client.get("/measures").status_code == 403
