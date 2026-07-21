"""Workspace dashboard aggregate regressions."""

import re
from datetime import timedelta

from sqlmodel import Session, select

from iceberg.models import Notebook, Report, ReportStatus, User, utcnow


def test_dashboard_counts_all_in_flight_reports_not_only_the_recent_eight(
    client, login, engine
):
    login("ANALYST", email="analyst@example.com")
    user_id = client.get("/api/me").json()["id"]
    now = utcnow()
    with Session(engine) as session:
        notebook = Notebook(title="Dashboard", owner_id=user_id)
        session.add(notebook)
        session.commit()
        session.refresh(notebook)
        session.add(
            Report(
                notebook_id=notebook.id,
                author_id=user_id,
                title="Older approved report",
                status=ReportStatus.APPROVED,
                updated_at=now - timedelta(hours=1),
            )
        )
        for index in range(8):
            session.add(
                Report(
                    notebook_id=notebook.id,
                    author_id=user_id,
                    title=f"Recent draft {index}",
                    status=ReportStatus.DRAFT,
                    updated_at=now + timedelta(minutes=index),
                )
            )
        session.add(
            Report(
                notebook_id=notebook.id,
                author_id=user_id,
                title="Published report",
                status=ReportStatus.PUBLISHED,
                updated_at=now + timedelta(hours=1),
            )
        )
        session.commit()

    response = client.get("/")
    assert response.status_code == 200
    assert re.search(
        r"Reports in flight</div>\s*<div class=\"stat-row\">\s*"
        r"<span class=\"stat-num\">9</span>",
        response.text,
    )
    assert "0 in review · 1 approved" in response.text
    # The display list is still presentation-capped, independently of the KPI
    # (scoped to the recent-reports column — the "Needs you now" queue above it
    # renders rows of its own).
    recent = response.text.split("Recent reports", 1)[1]
    assert recent.count('class="row-title"') == 8
    assert "Older approved report" not in recent


def _other_author(session) -> int:
    """A second real author, so "someone else's draft" is a genuine FK row."""
    user = User(email="other@example.com", display_name="Other")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.id


def _seed(session, *, owner_id, title, status, author_id=None, age_minutes=0):
    notebook = session.exec(select(Notebook)).first()
    if notebook is None:
        notebook = Notebook(title="Queue", owner_id=owner_id)
        session.add(notebook)
        session.commit()
        session.refresh(notebook)
    session.add(
        Report(
            notebook_id=notebook.id,
            author_id=author_id if author_id is not None else owner_id,
            title=title,
            status=status,
            updated_at=utcnow() - timedelta(minutes=age_minutes),
        )
    )
    session.commit()


def test_needs_you_now_lists_your_drafts_only(client, login, engine):
    """An analyst's queue is their own unfinished work — another author's draft
    is not theirs to resume, and they cannot review."""
    login("ANALYST", email="mine@example.com")
    me = client.get("/api/me").json()["id"]
    with Session(engine) as session:
        _seed(session, owner_id=me, title="My draft", status=ReportStatus.DRAFT)
        _seed(
            session,
            owner_id=me,
            author_id=_other_author(session),
            title="Their draft",
            status=ReportStatus.DRAFT,
        )
        _seed(session, owner_id=me, title="Awaiting review", status=ReportStatus.IN_REVIEW)

    page = client.get("/").text
    queue = page.split("Recent reports", 1)[0]
    assert "Needs you now" in queue
    assert "My draft" in queue
    assert "Their draft" not in queue
    assert "Awaiting review" not in queue  # an analyst is not a reviewer


def test_needs_you_now_puts_the_review_queue_first(client, login, engine):
    """For a reviewer, work waiting on *them* outranks their own drafts."""
    login("REVIEWER", email="rev@example.com")
    me = client.get("/api/me").json()["id"]
    with Session(engine) as session:
        _seed(
            session,
            owner_id=me,
            title="My newer draft",
            status=ReportStatus.DRAFT,
            age_minutes=0,
        )
        _seed(
            session,
            owner_id=me,
            author_id=_other_author(session),
            title="Older submission",
            status=ReportStatus.IN_REVIEW,
            age_minutes=60,
        )

    queue = client.get("/").text.split("Recent reports", 1)[0]
    assert queue.index("Older submission") < queue.index("My newer draft")
    assert "Review →" in queue and "Resume →" in queue


def test_needs_you_now_is_capped(client, login, engine):
    login("ANALYST", email="many@example.com")
    me = client.get("/api/me").json()["id"]
    with Session(engine) as session:
        for i in range(9):
            _seed(
                session,
                owner_id=me,
                title=f"Draft {i}",
                status=ReportStatus.DRAFT,
                age_minutes=i,
            )

    queue = client.get("/").text.split("Recent reports", 1)[0]
    assert queue.count('class="queue-dot') == 5


def test_stakeholder_dashboard_has_no_writer_queue(client, login):
    login("STAKEHOLDER", email="reader@example.com")
    assert "Needs you now" not in client.get("/").text
