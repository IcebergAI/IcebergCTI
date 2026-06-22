"""Full-text + faceted search over reports — backend chosen by SQL dialect.

The free-text query resolves to a ranked list of report ids by backend, then the
same facet filters + access-control rules apply to both:

- **SQLite** (dev/test default): an external-content FTS5 table (``report_fts``)
  mirrors each report's title + body + judgement scaffolding; ``AFTER
  INSERT/UPDATE/DELETE`` triggers keep it in sync, so nothing in the report save
  paths needs to know about search. The table + triggers are created by an
  ``after_create`` event on ``Report.__table__`` (wired by
  :func:`register_fts_events`, called by ``db`` at import time so the objects are
  built by *every* ``create_all``). Ranked with ``bm25``.
- **PostgreSQL** (production option): a DB-maintained generated ``tsvector``
  column (``report.search_vector`` + a GIN index, created by the ``postgres_fts``
  migration) over the same text. Queried with ``websearch_to_tsquery`` and ranked
  with ``ts_rank``. Always current, so no reindex step is needed.

Access control: stakeholders (read-only consumers) only ever match *published*
reports — the same rule as :func:`services.reports.ensure_visible`, reapplied here
so search can't leak unpublished material.
"""

import re

from fastapi import HTTPException
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
from . import tags as tag_service
from . import reports as report_service

_FTS_TABLE = "report_fts"

# Indexed columns mirror report columns by name so FTS5's external-content
# 'rebuild' (used by reindex) can backfill them straight from `report`. The
# ICD 203 judgement scaffolding is indexed alongside the body so the core
# assessment (Key Judgements) is discoverable, not just the narrative.
_FTS_COLS = ("title", "body_md", "key_judgements", "key_assumptions", "intelligence_gaps")
_COLS = ", ".join(_FTS_COLS)
_NEW = ", ".join(f"new.{c}" for c in _FTS_COLS)
_OLD = ", ".join(f"old.{c}" for c in _FTS_COLS)

# nosec B608: every interpolated name here (_FTS_TABLE, _COLS, _NEW, _OLD) is a
# fixed internal identifier, never user input — there is no injection surface.
_DDL = [
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} "  # nosec B608
    f"USING fts5({_COLS}, content='report', content_rowid='id')",
    f"""CREATE TRIGGER IF NOT EXISTS report_ai AFTER INSERT ON report BEGIN
        INSERT INTO {_FTS_TABLE}(rowid, {_COLS})
        VALUES (new.id, {_NEW});
    END""",  # nosec B608
    f"""CREATE TRIGGER IF NOT EXISTS report_ad AFTER DELETE ON report BEGIN
        INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, {_COLS})
        VALUES('delete', old.id, {_OLD});
    END""",  # nosec B608
    f"""CREATE TRIGGER IF NOT EXISTS report_au AFTER UPDATE ON report BEGIN
        INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, {_COLS})
        VALUES('delete', old.id, {_OLD});
        INSERT INTO {_FTS_TABLE}(rowid, {_COLS})
        VALUES (new.id, {_NEW});
    END""",  # nosec B608
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
    # nosec B608: _FTS_TABLE is a fixed internal identifier, not user input.
    session.execute(text(f"INSERT INTO {_FTS_TABLE}({_FTS_TABLE}) VALUES('rebuild')"))  # nosec B608
    session.commit()


def _match_query(q: str) -> str | None:
    """Turn free user input into a safe FTS5 MATCH expression: each word becomes a
    prefix term (AND-combined). Punctuation is dropped so it can't be an operator."""
    tokens = re.findall(r"\w+", q.lower())
    if not tokens:
        return None
    return " ".join(f'"{tok}"*' for tok in tokens)


def _fts_ids(session: Session, q: str) -> list[int]:
    """Full-text ranked report ids for the free-text query, dispatched by backend
    (SQLite FTS5 / PostgreSQL tsvector). Other backends return no FTS matches —
    only the alias/label resolution in :func:`search_reports` contributes."""
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "sqlite":
        return _fts_ids_sqlite(session, q)
    if dialect == "postgresql":
        return _fts_ids_postgres(session, q)
    return []


def _fts_ids_sqlite(session: Session, q: str) -> list[int]:
    """SQLite FTS5 ranked ids (bm25) over ``report_fts``."""
    match = _match_query(q)
    if match is None:
        return []
    rows = session.execute(
        # nosec B608: _FTS_TABLE is a constant; the user's query is bound via :m.
        text(
            f"SELECT rowid FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH :m "  # nosec B608
            f"ORDER BY bm25({_FTS_TABLE})"
        ),
        {"m": match},
    ).all()
    return [r[0] for r in rows]


def _fts_ids_postgres(session: Session, q: str) -> list[int]:
    """PostgreSQL FTS ranked ids (ts_rank) over the generated ``search_vector``
    column. ``websearch_to_tsquery`` parses the raw user string safely (bound via
    :q), so no manual tokenisation/escaping is needed."""
    if not (q or "").strip():
        return []
    rows = session.execute(
        text(
            "SELECT id FROM report "
            "WHERE search_vector @@ websearch_to_tsquery('english', :q) "
            "ORDER BY ts_rank(search_vector, websearch_to_tsquery('english', :q)) DESC"
        ),
        {"q": q},
    ).all()
    return [r[0] for r in rows]


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
    """Faceted report search. ``q`` runs FTS over title+body (bm25-ranked) and is
    *alias-aware* — reports tagged with a named-threat entity whose label/alias
    matches ``q`` are appended after the body matches, so e.g. "Fancy Bear" finds
    APT28-tagged reports even when the body never names the alias. The facets are
    SQL filters. Stakeholders are restricted to published reports."""
    ranked_ids: list[int] | None = None
    if q:
        fts_ids = _fts_ids(session, q)
        # Entity (alias/label) matches appended after body relevance — the recall
        # win without disturbing the full-text ranking.
        ranked_ids = list(fts_ids)
        seen = set(fts_ids)
        for rid in tag_service.resolve_alias_report_ids(session, q):
            if rid not in seen:
                seen.add(rid)
                ranked_ids.append(rid)
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

    if user.role == Role.STAKEHOLDER:
        visible: list[Report] = []
        for report in results:
            try:
                visible.append(report_service.ensure_visible(report, user))
            except HTTPException:
                continue
        results = visible

    return results[offset : offset + limit]
