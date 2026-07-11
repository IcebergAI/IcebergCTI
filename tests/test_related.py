"""Bounded, access-scoped related-product retrieval."""

from sqlalchemy import event
from sqlmodel import Session, select

from iceberg.models import (
    AudienceGroup,
    Notebook,
    Report,
    ReportAudienceGroup,
    ReportEmbedding,
    ReportStatus,
    Role,
    User,
    UserAudienceGroup,
)
from iceberg.services import related


def _add_user(session: Session, *, email: str, role: Role) -> User:
    user = User(email=email, display_name=email, role=role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _add_report(
    session: Session,
    *,
    notebook_id: int,
    author_id: int,
    title: str,
    status: ReportStatus = ReportStatus.PUBLISHED,
) -> Report:
    report = Report(
        notebook_id=notebook_id,
        author_id=author_id,
        title=title,
        status=status,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


def _add_embedding(session: Session, report: Report, vector: list[float]) -> None:
    session.add(
        ReportEmbedding(
            report_id=report.id,
            backend="local:hash-v1",
            vector=vector,
        )
    )
    session.commit()


def test_related_lookup_uses_a_fixed_query_budget_for_large_corpora(engine):
    with Session(engine) as session:
        author = _add_user(session, email="author@example.com", role=Role.ANALYST)
        notebook = Notebook(title="Related corpus", owner_id=author.id)
        session.add(notebook)
        session.commit()
        session.refresh(notebook)
        target = _add_report(
            session,
            notebook_id=notebook.id,
            author_id=author.id,
            title="Target",
        )
        _add_embedding(session, target, [1.0, 0.0])
        for index in range(120):
            report = _add_report(
                session,
                notebook_id=notebook.id,
                author_id=author.id,
                title=f"Candidate {index}",
            )
            _add_embedding(session, report, [1.0, 0.0])
        target_id = target.id
        author_id = author.id

    statements = 0

    def count_queries(_conn, _cursor, _statement, _parameters, _context, _executemany):
        nonlocal statements
        statements += 1

    with Session(engine) as session:
        target_record = session.get(Report, target_id)
        author_record = session.get(User, author_id)
        event.listen(engine, "before_cursor_execute", count_queries)
        try:
            result = related.related_reports(
                session,
                report=target_record,
                user=author_record,
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_queries)

    assert len(result) == 5
    # One target-vector lookup plus one joined, capped candidate query: no N+1.
    assert statements <= 2


def test_related_lookup_caps_vectors_scored_and_orders_ties_deterministically(engine, monkeypatch):
    with Session(engine) as session:
        author = _add_user(session, email="author@example.com", role=Role.ANALYST)
        notebook = Notebook(title="Bounded corpus", owner_id=author.id)
        session.add(notebook)
        session.commit()
        session.refresh(notebook)
        target = _add_report(
            session,
            notebook_id=notebook.id,
            author_id=author.id,
            title="Target",
        )
        _add_embedding(session, target, [1.0, 0.0])
        candidates = []
        for index in range(related.RELATED_CANDIDATE_LIMIT + 30):
            report = _add_report(
                session,
                notebook_id=notebook.id,
                author_id=author.id,
                title=f"Candidate {index}",
            )
            _add_embedding(session, report, [1.0, 0.0])
            candidates.append(report)
        target_id = target.id
        author_id = author.id

    original_cosine = related._cosine
    scored = 0

    def count_scores(a, b):
        nonlocal scored
        scored += 1
        return original_cosine(a, b)

    monkeypatch.setattr(related, "_cosine", count_scores)
    with Session(engine) as session:
        result = related.related_reports(
            session,
            report=session.get(Report, target_id),
            user=session.get(User, author_id),
        )

    assert scored == related.RELATED_CANDIDATE_LIMIT
    assert [item["report"].id for item in result] == sorted(
        item["report"].id for item in result
    )
    assert all(item["score"] == 1.0 for item in result)


def test_related_lookup_filters_drafts_and_audience_hidden_reports_in_sql(engine):
    with Session(engine) as session:
        author = _add_user(session, email="author@example.com", role=Role.ANALYST)
        stakeholder = _add_user(
            session, email="stakeholder@example.com", role=Role.STAKEHOLDER
        )
        notebook = Notebook(title="Visibility corpus", owner_id=author.id)
        session.add(notebook)
        visible_group = AudienceGroup(name="Visible", slug="visible")
        hidden_group = AudienceGroup(name="Hidden", slug="hidden")
        session.add(visible_group)
        session.add(hidden_group)
        session.commit()
        session.refresh(notebook)
        session.refresh(visible_group)
        session.refresh(hidden_group)
        session.add(
            UserAudienceGroup(user_id=stakeholder.id, group_id=visible_group.id)
        )
        target = _add_report(
            session,
            notebook_id=notebook.id,
            author_id=author.id,
            title="Target",
        )
        public = _add_report(
            session,
            notebook_id=notebook.id,
            author_id=author.id,
            title="Public",
        )
        matching = _add_report(
            session,
            notebook_id=notebook.id,
            author_id=author.id,
            title="Matching group",
        )
        hidden = _add_report(
            session,
            notebook_id=notebook.id,
            author_id=author.id,
            title="Hidden group",
        )
        draft = _add_report(
            session,
            notebook_id=notebook.id,
            author_id=author.id,
            title="Draft",
            status=ReportStatus.DRAFT,
        )
        session.add(
            ReportAudienceGroup(report_id=matching.id, group_id=visible_group.id)
        )
        session.add(ReportAudienceGroup(report_id=hidden.id, group_id=hidden_group.id))
        session.commit()
        for report in (target, public, matching, hidden, draft):
            _add_embedding(session, report, [1.0, 0.0])
        target_id = target.id
        stakeholder_id = stakeholder.id

    with Session(engine) as session:
        result = related.related_reports(
            session,
            report=session.get(Report, target_id),
            user=session.get(User, stakeholder_id),
        )

    assert {item["report"].title for item in result} == {"Public", "Matching group"}


def test_related_rebuild_commits_all_embedding_writes_once(engine, monkeypatch):
    with Session(engine) as session:
        author = _add_user(session, email="author@example.com", role=Role.ANALYST)
        notebook = Notebook(title="Rebuild corpus", owner_id=author.id)
        session.add(notebook)
        session.commit()
        session.refresh(notebook)
        for index in range(3):
            _add_report(
                session,
                notebook_id=notebook.id,
                author_id=author.id,
                title=f"Published {index}",
            )

        original_commit = session.commit
        commits = 0

        def count_commits():
            nonlocal commits
            commits += 1
            return original_commit()

        monkeypatch.setattr(session, "commit", count_commits)
        assert related.rebuild(session) == 3
        assert commits == 1
        assert len(session.exec(select(ReportEmbedding)).all()) == 3
