"""Why a product reached a reader — the stakeholder feed's delivery reason.

The reason is *derived* from the same rules that routed the product
(``dissemination.matched_stakeholders``) rather than stored on the event, so
these tests pin the derivation to those rules. A stored copy could drift; this
cannot.
"""

import pytest
from sqlmodel import Session, select

from iceberg.models import (
    AudienceGroup,
    DisseminationEvent,
    IntelLevel,
    Notebook,
    Report,
    ReportStatus,
    Requirement,
    RequirementKind,
    Tag,
    TagKind,
    User,
)
from iceberg.services import feed as feed_service


@pytest.fixture
def reader(engine):
    with Session(engine) as session:
        user = User(email="reader@example.com", display_name="Reader")
        session.add(user)
        session.commit()
        session.refresh(user)
        yield user.id


def _report(session, **fields) -> Report:
    notebook = session.exec(select(Notebook)).first()
    if notebook is None:
        author = session.exec(select(User)).first()
        notebook = Notebook(title="Feed nb", owner_id=author.id)
        session.add(notebook)
        session.commit()
        session.refresh(notebook)
    report = Report(notebook_id=notebook.id, author_id=notebook.owner_id, title="Product", **fields)
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


def test_own_requirement_outranks_every_other_reason(engine, reader):
    """A product that answers your own RFI is the strongest thing the feed can
    say about it, even when a tag subscription also matched."""
    with Session(engine) as session:
        user = session.get(User, reader)
        tag = Tag(kind=TagKind.ACTOR, label="Volt Typhoon", slug="volt-typhoon")
        session.add(tag)
        session.commit()
        user.tag_subscriptions.append(tag)
        report = _report(session, intel_level=IntelLevel.STRATEGIC)
        report.tags.append(tag)
        req = Requirement(
            title="What is the CNI risk?",
            kind=RequirementKind.RFI,
            stakeholder_id=user.id,
        )
        session.add(req)
        session.commit()
        report.requirements.append(req)
        session.commit()

        reason = feed_service.delivery_reason(user, report)
        assert reason["kind"] == "requirement"
        assert reason["requirement_id"] == req.id


def test_someone_elses_requirement_is_not_your_reason(engine, reader):
    with Session(engine) as session:
        user = session.get(User, reader)
        other = User(email="other@example.com", display_name="Other")
        session.add(other)
        session.commit()
        report = _report(session)
        req = Requirement(
            title="Their question",
            kind=RequirementKind.RFI,
            stakeholder_id=other.id,
        )
        session.add(req)
        session.commit()
        report.requirements.append(req)
        session.commit()

        assert feed_service.delivery_reason(user, report)["kind"] != "requirement"


def test_tag_subscription_names_the_matching_entity(engine, reader):
    with Session(engine) as session:
        user = session.get(User, reader)
        subscribed = Tag(kind=TagKind.ACTOR, label="Sandworm", slug="sandworm")
        ignored = Tag(kind=TagKind.SECTOR, label="Energy", slug="energy")
        session.add_all([subscribed, ignored])
        session.commit()
        user.tag_subscriptions.append(subscribed)
        report = _report(session)
        report.tags.extend([ignored, subscribed])
        session.commit()

        reason = feed_service.delivery_reason(user, report)
        assert (reason["kind"], reason["label"]) == ("tag", "Sandworm")


def test_audience_group_membership_is_a_reason(engine, reader):
    with Session(engine) as session:
        user = session.get(User, reader)
        group = AudienceGroup(name="CNI leads", slug="cni-leads")
        session.add(group)
        session.commit()
        user.audience_groups.append(group)
        report = _report(session)
        report.audience_groups.append(group)
        session.commit()

        reason = feed_service.delivery_reason(user, report)
        assert (reason["kind"], reason["label"]) == ("audience", "CNI leads")


def test_level_preference_and_the_no_preference_default(engine, reader):
    with Session(engine) as session:
        user = session.get(User, reader)
        report = _report(session, intel_level=IntelLevel.TACTICAL)

        # No preference set = "send me everything".
        assert feed_service.delivery_reason(user, report)["kind"] == "all"

        user.preferred_intel_level = IntelLevel.TACTICAL
        session.add(user)
        session.commit()
        reason = feed_service.delivery_reason(user, report)
        assert reason["kind"] == "level"
        assert "TACTICAL" in reason["label"]


def test_feed_page_shows_the_reason_and_a_way_to_close_the_loop(client, login, engine):
    email = login("STAKEHOLDER", email="feedreader@example.com")
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).one()
        report = _report(
            session, intel_level=IntelLevel.STRATEGIC, status=ReportStatus.PUBLISHED
        )
        req = Requirement(
            title="Ransomware outlook?",
            kind=RequirementKind.RFI,
            stakeholder_id=user.id,
        )
        session.add(req)
        session.commit()
        report.requirements.append(req)
        session.add(DisseminationEvent(report_id=report.id, stakeholder_id=user.id))
        session.commit()
        req_id = req.id
        report_id = report.id

    page = client.get("/feed")
    assert page.status_code == 200
    assert "Answers your RFI" in page.text
    assert f"/reports/{report_id}?requirement={req_id}#feedback" in page.text
    # Time buckets replace the flat list.
    assert "Today" in page.text


def test_preselected_requirement_must_be_the_readers_own(client, login, engine):
    """The ?requirement= deep-link only ever preselects a requirement the reader
    owns *and* the report answers — it can never surface anything else."""
    email = login("STAKEHOLDER", email="picky@example.com")
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).one()
        other = User(email="stranger@example.com", display_name="Stranger")
        session.add(other)
        session.commit()
        report = _report(session, status=ReportStatus.PUBLISHED)
        theirs = Requirement(
            title="Someone else's secret question",
            kind=RequirementKind.RFI,
            stakeholder_id=other.id,
        )
        session.add(theirs)
        session.commit()
        report.requirements.append(theirs)
        session.add(DisseminationEvent(report_id=report.id, stakeholder_id=user.id))
        session.commit()
        report_id, foreign_req_id = report.id, theirs.id

    page = client.get(f"/reports/{report_id}?requirement={foreign_req_id}")
    assert page.status_code == 200
    assert "Someone else's secret question" not in page.text
