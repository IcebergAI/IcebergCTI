"""What the stakeholder feed can honestly say about a delivered product.

Two independent statements, both present-tense: whether it answers a requirement
the reader raised (a fact about the product) and how it matches their routing
preferences (a fact about their settings *now*). ``DisseminationEvent`` records
no routing metadata, so neither claims to reconstruct the publish-time decision —
these tests pin that separation, because conflating them is what makes a feed
lie after a subscription changes.
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


def test_requirement_and_match_are_reported_independently(engine, reader):
    """An RFI link is *not* why a product was routed — requirements are not a
    predicate in ``dissemination.matched_stakeholders``. So the RFI badge and
    the preference match must both appear, not compete."""
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

        ctx = feed_service.delivery_context(user, report)
        assert ctx["answers_requirement_id"] == req.id
        # The tag subscription is still reported as the routing match.
        assert ctx["match"]["kind"] == "tag"
        assert "Volt Typhoon" in ctx["match"]["label"]


def test_someone_elses_requirement_is_not_your_rfi(engine, reader):
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

        assert (
            feed_service.delivery_context(user, report)["answers_requirement_id"]
            is None
        )


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

        match = feed_service.delivery_context(user, report)["match"]
        assert match["kind"] == "tag"
        assert "Sandworm" in match["label"]


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

        match = feed_service.delivery_context(user, report)["match"]
        assert match["kind"] == "audience"
        assert "CNI leads" in match["label"]


def test_level_preference_and_the_no_preference_default(engine, reader):
    with Session(engine) as session:
        user = session.get(User, reader)
        report = _report(session, intel_level=IntelLevel.TACTICAL)

        # No preference set = "send me everything".
        assert feed_service.delivery_context(user, report)["match"]["kind"] == "all"

        user.preferred_intel_level = IntelLevel.TACTICAL
        session.add(user)
        session.commit()
        match = feed_service.delivery_context(user, report)["match"]
        assert match["kind"] == "level"
        assert "TACTICAL" in match["label"]


def test_a_preference_changed_since_delivery_is_not_claimed_as_a_match(engine, reader):
    """Delivery is never retracted, so a product stays in the feed after the
    reader changes their level preference. Saying it "matches" would be the
    exact false present-tense claim this split exists to prevent — it must name
    the product's level *and* the preference it no longer satisfies."""
    with Session(engine) as session:
        user = session.get(User, reader)
        report = _report(session, intel_level=IntelLevel.TACTICAL)
        user.preferred_intel_level = IntelLevel.STRATEGIC
        session.add(user)
        session.commit()

        match = feed_service.delivery_context(user, report)["match"]
        assert match["kind"] == "level_changed"
        assert "TACTICAL" in match["label"]      # what the product is
        assert "STRATEGIC" in match["label"]     # what they now prefer
        assert "Matches your" not in match["label"]


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


def test_read_rows_are_never_cloaked(client, login, engine):
    """`[x-cloak]` is `display:none !important`, so cloaking a row whose default
    state is *shown* would make a fully-read feed look empty whenever Alpine
    fails to load. The Unread filter must degrade to "everything visible", not
    "nothing visible"."""
    email = login("STAKEHOLDER", email="nojs@example.com")
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).one()
        report = _report(session, status=ReportStatus.PUBLISHED)
        report.title = "Already read product"
        session.add(report)
        session.add(DisseminationEvent(report_id=report.id, stakeholder_id=user.id))
        session.commit()

    client.get("/feed")  # first visit marks it read
    page = client.get("/feed").text

    import re

    assert "Already read product" in page
    # Scope to the feed rows: base.html's ⌘K overlay legitimately uses x-cloak
    # (it *should* be hidden until hydration).
    rows = re.findall(r'<div class="row"[^>]*>', page)
    assert rows, "no feed rows rendered"
    assert not [row for row in rows if "x-cloak" in row], (
        f"a read feed row is cloaked and would vanish without Alpine: {rows}"
    )
    # The bucket wrapper around an all-read group must not be cloaked either.
    assert 'x-show="!unreadOnly" x-cloak' not in page
