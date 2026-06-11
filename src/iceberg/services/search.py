"""Full-text + faceted search over reports, backed by SQLite FTS5.

An external-content FTS5 table (``report_fts``) mirrors each report's title and
body; ``AFTER INSERT/UPDATE/DELETE`` triggers keep it in sync, so nothing in the
report save paths needs to know about search. The table + triggers are created by
an ``after_create`` event on ``Report.__table__`` — wired by
:func:`register_fts_events`, which ``db`` calls at import time so the objects are
built by *every* ``create_all`` (production boot and the in-memory test engine).

Access control: stakeholders (read-only consumers) only ever match *published*
reports — the same rule as :func:`services.reports.ensure_visible`, reapplied here
so search can't leak unpublished material.
"""

import re

from sqlalchemy import event, text
from sqlmodel import Session, col, select

from ..models import (
    IntelLevel,
    Report,
    ReportStatus,
    ReportTag,
    Role,
    Tag,
    TagKind,
    TLP,
    User,
)

_FTS_TABLE = "report_fts"

_DDL = [
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} "
    f"USING fts5(title, body_md, content='report', content_rowid='id')",
    f"""CREATE TRIGGER IF NOT EXISTS report_ai AFTER INSERT ON report BEGIN
        INSERT INTO {_FTS_TABLE}(rowid, title, body_md)
        VALUES (new.id, new.title, new.body_md);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS report_ad AFTER DELETE ON report BEGIN
        INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, title, body_md)
        VALUES('delete', old.id, old.title, old.body_md);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS report_au AFTER UPDATE ON report BEGIN
        INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, title, body_md)
        VALUES('delete', old.id, old.title, old.body_md);
        INSERT INTO {_FTS_TABLE}(rowid, title, body_md)
        VALUES (new.id, new.title, new.body_md);
    END""",
]

_registered = False


def _create_report_fts(target, connection, **_kw) -> None:
    if connection.dialect.name != "sqlite":
        return  # FTS5 is SQLite-specific; other backends skip search indexing.
    for stmt in _DDL:
        connection.exec_driver_sql(stmt)


def register_fts_events() -> None:
    """Attach the FTS table/trigger creation to ``Report`` table creation. Safe
    to call repeatedly (idempotent)."""
    global _registered
    if _registered:
        return
    event.listen(Report.__table__, "after_create", _create_report_fts)
    _registered = True


def reindex(session: Session) -> None:
    """Rebuild the FTS index from the content table (backfills rows that predate
    the index). No-op on non-SQLite backends."""
    if session.bind is None or session.bind.dialect.name != "sqlite":
        return
    session.execute(text(f"INSERT INTO {_FTS_TABLE}({_FTS_TABLE}) VALUES('rebuild')"))
    session.commit()


def _match_query(q: str) -> str | None:
    """Turn free user input into a safe FTS5 MATCH expression: each word becomes a
    prefix term (AND-combined). Punctuation is dropped so it can't be an operator."""
    tokens = re.findall(r"\w+", q.lower())
    if not tokens:
        return None
    return " ".join(f'"{tok}"*' for tok in tokens)


def search_reports(
    session: Session,
    *,
    user: User,
    q: str | None = None,
    kinds: list[TagKind] | None = None,
    tag_ids: list[int] | None = None,
    intel_level: IntelLevel | None = None,
    tlp: TLP | None = None,
    status: ReportStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Report]:
    """Faceted report search. ``q`` runs FTS over title+body (bm25-ranked); the
    facets are SQL filters. Stakeholders are restricted to published reports."""
    ranked_ids: list[int] | None = None
    match = _match_query(q) if q else None
    if match is not None:
        rows = session.execute(
            text(
                f"SELECT rowid FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH :m "
                f"ORDER BY bm25({_FTS_TABLE})"
            ),
            {"m": match},
        ).all()
        ranked_ids = [r[0] for r in rows]
        if not ranked_ids:
            return []

    stmt = select(Report)
    if ranked_ids is not None:
        stmt = stmt.where(col(Report.id).in_(ranked_ids))
    else:
        stmt = stmt.order_by(Report.updated_at.desc())

    # Access control: read-only stakeholders only ever see published reports.
    if user.role == Role.STAKEHOLDER:
        stmt = stmt.where(Report.status == ReportStatus.PUBLISHED)
    elif status is not None:
        stmt = stmt.where(Report.status == status)

    if tag_ids:
        stmt = stmt.where(
            col(Report.id).in_(
                select(ReportTag.report_id).where(col(ReportTag.tag_id).in_(tag_ids))
            )
        )
    if kinds:
        stmt = stmt.where(
            col(Report.id).in_(
                select(ReportTag.report_id)
                .join(Tag, col(Tag.id) == ReportTag.tag_id)
                .where(col(Tag.kind).in_(kinds))
            )
        )
    if intel_level is not None:
        stmt = stmt.where(Report.intel_level == intel_level)
    if tlp is not None:
        stmt = stmt.where(Report.tlp == tlp)

    results = list(session.exec(stmt).all())

    if ranked_ids is not None:
        order = {rid: i for i, rid in enumerate(ranked_ids)}
        results.sort(key=lambda r: order.get(r.id, len(order)))

    return results[offset : offset + limit]
