"""Related-product retrieval over a rebuildable, permission-safe local index.

Three properties matter more than relevance here (#311):

* **Nothing leaks.** Access filtering happens in SQL, *before* the candidate
  window is taken and before anything is scored, so a product the reader cannot
  access can never change the result count, the ranking, a snippet, the shape of
  an error, or how long the request takes.
* **Every entry says where it came from.** A row records the provider, model,
  model version, dimensions, the ``Report.version`` and a digest of the indexed
  text. That makes staleness a *property of the data* rather than a guess, so a
  bounded reindex pass can find exactly the entries a model change or an edit
  invalidated — and can be interrupted and resumed without a cursor.
* **The feature never disappears.** With the vector provider switched off,
  retrieval falls back to lexical overlap over the same permission-filtered
  candidates, so related products keep working (and keep the same access
  guarantees) on a deployment that indexes nothing.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from math import sqrt

from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import (
    Report,
    ReportEmbedding,
    ReportStatus,
    Role,
    User,
    utcnow,
)
from . import ai as ai_service
from . import reports as reports_service


# Local JSON vectors cannot be ranked by either supported database, so related
# lookup intentionally scores a stable recent-candidate window in Python. This
# bounds SQL rows, memory and CPU independently of the total corpus size while
# keeping the result deterministic across SQLite and PostgreSQL.
RELATED_CANDIDATE_LIMIT = 256
# Default bound for one reindex pass, so an operator can run it repeatedly
# against a large corpus without holding a long transaction.
REINDEX_BATCH = 200


def report_text(report: Report) -> str:
    return "\n\n".join(
        part
        for part in (
            report.title,
            report.key_judgements,
            report.body_md,
            report.key_assumptions,
            report.intelligence_gaps,
        )
        if part
    )


def content_digest(report: Report) -> str:
    return hashlib.sha256(report_text(report).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Embedding providers
# --------------------------------------------------------------------------- #
class EmbeddingProvider(ABC):
    """One way of turning a finished product into a comparable vector.

    ``version`` is part of an index entry's provenance: bump it whenever the
    produced vectors change meaning, and every existing entry reads as stale on
    the next pass instead of being compared against incompatible vectors.
    """

    name: str = ""
    model: str = ""
    version: str = ""
    dimensions: int = 0
    enabled: bool = True

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return the vector for one product's indexed text."""


class LocalHashProvider(EmbeddingProvider):
    """Deterministic, non-egress hash embedding — the default.

    Not a semantic model, but it needs no provider, sends nothing anywhere, and
    ranks reproducibly on both supported databases. A governed remote provider
    slots in beside it by implementing this same interface.
    """

    name = "local"
    model = "hash-v1"
    version = "1"
    dimensions = 32

    def embed(self, text: str) -> list[float]:
        return ai_service.local_embedding(text, dimensions=self.dimensions)


class DisabledProvider(EmbeddingProvider):
    """No vector index at all; retrieval uses the lexical fallback."""

    name = "none"
    model = ""
    version = ""
    dimensions = 0
    enabled = False

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("The related-product embedding provider is disabled")


_PROVIDERS: dict[str, type[EmbeddingProvider]] = {
    "local": LocalHashProvider,
    "none": DisabledProvider,
}


def get_provider() -> EmbeddingProvider:
    """The configured provider (validated at config load, so this can't fail)."""

    return _PROVIDERS[get_settings().related_backend]()


# --------------------------------------------------------------------------- #
# Index maintenance
# --------------------------------------------------------------------------- #
def _apply(
    report: Report, row: ReportEmbedding | None, provider: EmbeddingProvider
) -> ReportEmbedding:
    if row is None:
        row = ReportEmbedding(report_id=report.id)
    row.backend = provider.name
    row.model = provider.model
    row.model_version = provider.version
    row.dimensions = provider.dimensions
    row.source_version = report.version
    row.content_sha256 = content_digest(report)
    row.vector = provider.embed(report_text(report))
    row.updated_at = utcnow()
    return row


def is_stale(row: ReportEmbedding, report: Report, provider: EmbeddingProvider) -> bool:
    """True when an entry no longer describes this product under this provider."""

    # ``dimensions`` is recorded provenance, not an identity check: a model and
    # version already fix the shape, and a mismatched vector length scores zero.
    return (
        row.backend != provider.name
        or row.model != provider.model
        or row.model_version != provider.version
        or row.source_version != report.version
        or row.content_sha256 != content_digest(report)
    )


def upsert_report_embedding(session: Session, report: Report) -> ReportEmbedding | None:
    """Create or refresh one published product's vector immediately.

    A product that is no longer published has its entry **removed** rather than
    left behind: an index must not outlive the visibility of what it indexes.
    """

    provider = get_provider()
    if report.id is None:
        return None
    if report.status != ReportStatus.PUBLISHED or not provider.enabled:
        remove_report_embedding(session, report)
        return None
    row = _apply(report, session.get(ReportEmbedding, report.id), provider)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def remove_report_embedding(session: Session, report: Report) -> bool:
    """Drop one product's entry. Idempotent; returns whether a row was removed."""

    if report.id is None:
        return False
    row = session.get(ReportEmbedding, report.id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def _published(session: Session) -> list[Report]:
    return list(
        session.exec(
            select(Report)
            .where(Report.status == ReportStatus.PUBLISHED)
            .order_by(col(Report.id))
        ).all()
    )


def _entries(session: Session, report_ids: list[int]) -> dict[int, ReportEmbedding]:
    if not report_ids:
        return {}
    return {
        row.report_id: row
        for row in session.exec(
            select(ReportEmbedding).where(col(ReportEmbedding.report_id).in_(report_ids))
        ).all()
        if row.report_id is not None
    }


def prune_orphans(session: Session) -> int:
    """Remove entries for products that are no longer published."""

    published = {report.id for report in _published(session)}
    removed = 0
    for row in session.exec(select(ReportEmbedding)).all():
        if row.report_id not in published:
            session.delete(row)
            removed += 1
    if removed:
        session.commit()
    return removed


def reindex(session: Session, *, batch: int = REINDEX_BATCH) -> dict[str, int]:
    """Refresh up to ``batch`` stale entries and report what is left to do.

    Resumability needs no cursor: staleness is derived from the row itself, so an
    interrupted pass simply leaves fewer stale entries and the next pass picks up
    exactly where it stopped. Running it twice is harmless.
    """

    provider = get_provider()
    removed = prune_orphans(session)
    if not provider.enabled:
        return {"indexed": 0, "removed": removed, "pending": 0, "up_to_date": 0}

    reports = _published(session)
    existing = _entries(session, [r.id for r in reports if r.id is not None])
    indexed = pending = up_to_date = 0
    for report in reports:
        row = existing.get(report.id)
        if row is not None and not is_stale(row, report, provider):
            up_to_date += 1
            continue
        if indexed >= batch:
            pending += 1
            continue
        session.add(_apply(report, row, provider))
        indexed += 1
    if indexed:
        session.commit()
    return {
        "indexed": indexed,
        "removed": removed,
        "pending": pending,
        "up_to_date": up_to_date,
    }


def rebuild(session: Session) -> int:
    """Reindex everything in bounded passes until nothing is pending."""

    total = 0
    while True:
        result = reindex(session)
        total += result["indexed"]
        if not result["pending"]:
            return total


def index_health(session: Session) -> dict:
    """Operator view of the index: provider, coverage, staleness, freshness."""

    provider = get_provider()
    reports = _published(session)
    existing = _entries(session, [r.id for r in reports if r.id is not None])
    stale = sum(
        1
        for report in reports
        if (row := existing.get(report.id)) is not None and is_stale(row, report, provider)
    )
    missing = sum(1 for report in reports if report.id not in existing)
    orphans = sum(
        1
        for row in session.exec(select(ReportEmbedding)).all()
        if row.report_id not in {report.id for report in reports}
    )
    generated = [row.updated_at for row in existing.values()]
    return {
        "backend": provider.name,
        "model": provider.model,
        "model_version": provider.version,
        "dimensions": provider.dimensions,
        "enabled": provider.enabled,
        "published": len(reports),
        "indexed": len(existing),
        "stale": stale,
        "missing": missing,
        "orphans": orphans,
        "pending": stale + missing if provider.enabled else 0,
        "last_generated_at": max(generated) if generated else None,
        "retrieval": "vector" if provider.enabled else "lexical",
    }


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
def _candidate_statement(report: Report, user: User, *, with_entries: bool):
    """Products this reader may see, capped to a stable window.

    The access filter is part of the *query*, so an unauthorised product is
    never fetched, never scored, and never competes for a place in the window —
    which is what keeps result counts and ranking free of side channels.
    ``with_entries`` joins the index so the vector path needs no second query per
    candidate.
    """

    selected = (
        select(Report, ReportEmbedding).join(
            ReportEmbedding, ReportEmbedding.report_id == Report.id
        )
        if with_entries
        else select(Report)
    )
    statement = (
        selected.where(Report.id != report.id, Report.status == ReportStatus.PUBLISHED)
        # A stable database order makes the capped set reproducible. Scores are
        # applied below because vectors are JSON arrays on both supported DBs.
        .order_by(col(Report.published_at).desc(), col(Report.id).asc())
        .limit(RELATED_CANDIDATE_LIMIT)
    )
    if user.role == Role.STAKEHOLDER:
        if user.id is None:
            return None
        statement = statement.where(
            reports_service.stakeholder_visibility_clause(user.id)
        )
    return statement


_WORD = re.compile(r"[a-z0-9]{3,}")
# Ubiquitous in finished intelligence, so they say nothing about relatedness.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "have", "has", "was",
        "were", "are", "not", "but", "its", "their", "they", "which", "report",
        "assess", "assessed", "assessment", "likely", "intelligence", "threat",
    }
)


def _terms(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


def _lexical_score(query_terms: set[str], candidate: Report) -> float:
    """Jaccard overlap of distinctive terms — deterministic and index-free."""

    other = _terms(report_text(candidate))
    if not query_terms or not other:
        return 0.0
    return len(query_terms & other) / len(query_terms | other)


def related_reports(
    session: Session, *, report: Report, user: User, limit: int = 5
) -> list[dict]:
    """Return visible related products from a bounded, stable candidate set."""

    if limit <= 0 or report.id is None:
        return []
    provider = get_provider()
    scored: list[tuple[float, Report]] = []
    method = "vector" if provider.enabled else "lexical"

    query: ReportEmbedding | None = None
    if provider.enabled:
        query = session.get(ReportEmbedding, report.id)
        if query is None or is_stale(query, report, provider):
            query = upsert_report_embedding(session, report)
        if query is None:
            method = "lexical"

    statement = _candidate_statement(report, user, with_entries=method == "vector")
    if statement is None:
        return []
    rows = list(session.exec(statement).all())

    if method == "vector" and query is not None:
        for other, entry in rows:
            # An entry that no longer describes its product is not scored:
            # ranking by a superseded revision would rank a product by text it
            # no longer contains.
            if is_stale(entry, other, provider):
                continue
            score = _cosine(query.vector, entry.vector)
            if score > 0:
                scored.append((score, other))
    else:
        query_terms = _terms(report_text(report))
        for other in rows:
            score = _lexical_score(query_terms, other)
            if score > 0:
                scored.append((score, other))

    scored.sort(key=lambda item: (-item[0], item[1].id or 0))
    return [
        {"report": other, "score": round(score, 4), "method": method}
        for score, other in scored[:limit]
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sqrt(sum(x * x for x in a))
    nb = sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)
