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
from dataclasses import dataclass

from sqlalchemy import column, event, exists, func, literal, literal_column, or_, table, text, union_all
from sqlmodel import Session, col, select

from ..models import (
    IntelLevel,
    Report,
    ReportStatus,
    ReportAudienceGroup,
    ReportTag,
    Role,
    Tag,
    TagKind,
    TLP,
    User,
    UserAudienceGroup,
)
from . import tags as tag_service

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
    return search_page(
        session, user=user, q=q, kinds=kinds, tag_ids=tag_ids,
        intel_level=intel_level, tlp=tlp, status=status,
        limit=limit, offset=offset,
    ).results


@dataclass(frozen=True)
class SearchPage:
    results: list[Report]
    total: int
    limit: int
    offset: int


def _ranked_matches(session: Session, q: str, *, include_rank: bool = True):
    dialect = session.bind.dialect.name if session.bind is not None else ""
    alias_ids = tag_service.resolve_alias_report_ids(session, q)
    body = None
    if dialect == "sqlite" and (match := _match_query(q)):
        fts = table(_FTS_TABLE, column("rowid"))
        body = (
            select(
                fts.c.rowid.label("report_id"),
                literal(0).label("bucket"),
                (
                    func.bm25(literal_column(_FTS_TABLE))
                    if include_rank and not alias_ids
                    else literal(0.0)
                ).label("rank"),
            )
            .where(text(f"{_FTS_TABLE} MATCH :search_match"))  # nosec B608
            .params(search_match=match)
        )
    elif dialect == "postgresql" and q.strip():
        query = func.websearch_to_tsquery("english", q)
        vector = literal_column("report.search_vector")
        body = select(
            Report.id.label("report_id"),
            literal(0).label("bucket"),
            (
                -func.ts_rank(vector, query) if include_rank else literal(0.0)
            ).label("rank"),
        ).where(vector.op("@@")(query))

    parts = [body] if body is not None else []
    if alias_ids:
        parts.extend(
            select(
                literal(report_id).label("report_id"),
                literal(1).label("bucket"),
                literal(0.0).label("rank"),
            )
            for report_id in alias_ids
        )
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0].subquery("ranked_matches")
    combined = union_all(*parts)
    matches = combined.subquery("search_matches")
    return (
        select(
            matches.c.report_id,
            func.min(matches.c.bucket).label("bucket"),
            func.min(matches.c.rank).label("rank"),
        )
        .group_by(matches.c.report_id)
        .subquery("ranked_matches")
    )


def search_page(
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
) -> SearchPage:
    """Faceted report search. ``q`` runs FTS over title+body (bm25-ranked) and is
    *alias-aware* — reports tagged with a named-threat entity whose label/alias
    matches ``q`` are appended after the body matches, so e.g. "Fancy Bear" finds
    APT28-tagged reports even when the body never names the alias. The facets are
    SQL filters. Stakeholders are restricted to published reports."""
    stmt = select(Report)
    ranked = _ranked_matches(session, q) if q else None
    if q and ranked is None:
        return SearchPage([], 0, limit, offset)
    if ranked is not None:
        stmt = stmt.join(ranked, ranked.c.report_id == Report.id).order_by(
            ranked.c.bucket, ranked.c.rank, Report.id
        )
    else:
        stmt = stmt.order_by(Report.updated_at.desc())

    def apply_filters(statement):
        # Access control executes in SQL before counting and pagination.
        if user.role == Role.STAKEHOLDER:
            statement = statement.where(Report.status == ReportStatus.PUBLISHED)
            scoped = exists(
            select(ReportAudienceGroup.report_id).where(
                ReportAudienceGroup.report_id == Report.id
            )
            )
            matching = exists(
            select(ReportAudienceGroup.report_id)
            .join(
                UserAudienceGroup,
                UserAudienceGroup.group_id == ReportAudienceGroup.group_id,
            )
            .where(
                ReportAudienceGroup.report_id == Report.id,
                UserAudienceGroup.user_id == user.id,
            )
            )
            statement = statement.where(or_(~scoped, matching))
        elif status is not None:
            statement = statement.where(Report.status == status)
        if tag_ids:
            statement = statement.where(
            col(Report.id).in_(
                select(ReportTag.report_id).where(col(ReportTag.tag_id).in_(tag_ids))
            )
            )
        if kinds:
            statement = statement.where(
            col(Report.id).in_(
                select(ReportTag.report_id)
                .join(Tag, col(Tag.id) == ReportTag.tag_id)
                .where(col(Tag.kind).in_(kinds))
            )
            )
        if intel_level is not None:
            statement = statement.where(Report.intel_level == intel_level)
        if tlp is not None:
            statement = statement.where(Report.tlp == tlp)
        return statement

    stmt = apply_filters(stmt)
    count_stmt = select(Report.id)
    if q:
        count_ranked = _ranked_matches(session, q, include_rank=False)
        count_stmt = count_stmt.join(
            count_ranked, count_ranked.c.report_id == Report.id
        )
    count_stmt = apply_filters(count_stmt)
    total = session.exec(select(func.count()).select_from(count_stmt.subquery())).one()
    results = list(session.exec(stmt.offset(offset).limit(limit)).all())
    return SearchPage(results, int(total), limit, offset)
