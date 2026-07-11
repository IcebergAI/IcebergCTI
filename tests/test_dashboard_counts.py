"""Workspace dashboard aggregate regressions."""

import re
from datetime import timedelta

from sqlmodel import Session

from iceberg.models import Notebook, Report, ReportStatus, utcnow


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
    # The display list is still presentation-capped, independently of the KPI.
    assert response.text.count('class="row-title"') == 8
    assert "Older approved report" not in response.text
