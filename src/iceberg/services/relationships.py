"""Entity relationships: the knowledge graph (roadmap 2c).

A STIX-shaped ``EntityRelationship`` row links two taxonomy entities —
``source --relation_type--> target`` — so the flat tag vocabulary becomes a
graph (*actor uses malware*, *campaign attributed-to actor*, *actor targets
sector*). Admin-curated; surfaced on the ``/tags/{id}`` entity profile as
inbound + outbound edges plus a hand-rendered SVG mini-graph.

Like ``services/tags.py`` / ``services/diamond.py`` this module raises
``fastapi.HTTPException`` directly so the rules stay identical across the JSON
API and the portal. Validation is deliberately *loose*: the source must be a
named-threat kind and the target a named-threat kind or SECTOR — no per-verb
kind matrix.
"""

from typing import NamedTuple
from xml.sax.saxutils import escape  # nosec B406 — escapes text for SVG output, never parses XML

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import EntityRelationship, RelationType, Tag, TagKind
from .tags import ALIASABLE_KINDS

# A named-threat entity can only relate *out of* one of the aliasable kinds; it
# can relate *into* another named-threat entity or a SECTOR (a ``targets`` victim).
SOURCE_KINDS = ALIASABLE_KINDS
TARGETABLE_KINDS = ALIASABLE_KINDS | {TagKind.SECTOR}


class Edge(NamedTuple):
    """One relationship as seen from a profiled entity: the verb + the other end."""

    relation_type: RelationType
    other: Tag
    rel_id: int


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def get_relationship(session: Session, rel_id: int) -> EntityRelationship:
    rel = session.get(EntityRelationship, rel_id)
    if not rel:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Relationship not found")
    return rel


def create_relationship(
    session: Session,
    *,
    source_tag_id: int,
    target_tag_id: int,
    relation_type: RelationType,
) -> EntityRelationship:
    if source_tag_id == target_tag_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "An entity cannot relate to itself"
        )
    source = session.get(Tag, source_tag_id)
    target = session.get(Tag, target_tag_id)
    if source is None or target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    if source.kind not in SOURCE_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Relationship source must be an actor, malware or campaign entity",
        )
    if target.kind not in TARGETABLE_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Relationship target must be a named-threat entity or a sector",
        )
    existing = session.exec(
        select(EntityRelationship).where(
            EntityRelationship.source_tag_id == source_tag_id,
            EntityRelationship.target_tag_id == target_tag_id,
            EntityRelationship.relation_type == relation_type,
        )
    ).first()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That relationship already exists"
        )
    rel = EntityRelationship(
        source_tag_id=source_tag_id,
        target_tag_id=target_tag_id,
        relation_type=relation_type,
    )
    session.add(rel)
    session.commit()
    session.refresh(rel)
    return rel


def delete_relationship(session: Session, rel: EntityRelationship) -> None:
    session.delete(rel)
    session.commit()


# --------------------------------------------------------------------------- #
# Listing / lookups
# --------------------------------------------------------------------------- #
def list_relationships(session: Session) -> list[tuple[EntityRelationship, Tag, Tag]]:
    """All relationships with their source + target tags resolved, ordered by
    source label then verb (for the admin page)."""
    rels = session.exec(select(EntityRelationship)).all()
    tag_ids = {r.source_tag_id for r in rels} | {r.target_tag_id for r in rels}
    tags = (
        {t.id: t for t in session.exec(select(Tag).where(col(Tag.id).in_(tag_ids))).all()}
        if tag_ids
        else {}
    )
    rows = [(r, tags[r.source_tag_id], tags[r.target_tag_id]) for r in rels]
    rows.sort(key=lambda row: (row[1].label.lower(), row[0].relation_type.value))
    return rows


def relationships_for(
    session: Session, tag_id: int
) -> tuple[list[Edge], list[Edge]]:
    """``(outbound, inbound)`` edges for an entity. Outbound = rows where this
    tag is the source (other end = target); inbound = rows where this tag is the
    target (other end = source). Each list is ordered by verb then other label."""
    rels = session.exec(
        select(EntityRelationship).where(
            (EntityRelationship.source_tag_id == tag_id)
            | (EntityRelationship.target_tag_id == tag_id)
        )
    ).all()
    other_ids = {
        (r.target_tag_id if r.source_tag_id == tag_id else r.source_tag_id)
        for r in rels
    }
    tags = (
        {t.id: t for t in session.exec(select(Tag).where(col(Tag.id).in_(other_ids))).all()}
        if other_ids
        else {}
    )
    outbound: list[Edge] = []
    inbound: list[Edge] = []
    for r in rels:
        if r.source_tag_id == tag_id and r.target_tag_id in tags:
            outbound.append(Edge(r.relation_type, tags[r.target_tag_id], r.id))
        elif r.target_tag_id == tag_id and r.source_tag_id in tags:
            inbound.append(Edge(r.relation_type, tags[r.source_tag_id], r.id))
    key = lambda e: (e.relation_type.value, e.other.label.lower())  # noqa: E731
    return sorted(outbound, key=key), sorted(inbound, key=key)


# --------------------------------------------------------------------------- #
# SVG mini-graph
# --------------------------------------------------------------------------- #
_SANS = "Archivo, 'Helvetica Neue', Arial, sans-serif"
_MONO = "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace"

# Kind hues harmonised with the Diamond Model diagram / design-system tag kinds.
_KIND_INK = {
    TagKind.ACTOR: "#8a4bad",
    TagKind.CAMPAIGN: "#b06a1f",
    TagKind.MALWARE: "#b03a4a",
    TagKind.TECHNIQUE: "#3461bd",
    TagKind.SECTOR: "#1c8a8a",
    TagKind.TOPIC: "#5a6672",
}
_NODE_W = 188
_NODE_H = 58


def _truncate(text: str, max_chars: int = 26) -> str:
    raw = " ".join((text or "").split())
    return raw if len(raw) <= max_chars else raw[: max_chars - 1] + "…"


def _node(tag: Tag, cx: float, cy: float, *, centre: bool = False) -> str:
    x = cx - _NODE_W / 2
    y = cy - _NODE_H / 2
    ink = _KIND_INK.get(tag.kind, "#5a6672")
    fill = "#f4f1f8" if centre else "#ffffff"
    stroke = ink if centre else "#cdd6df"
    sw = "2" if centre else "1.4"
    return "".join(
        [
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{_NODE_W}" height="{_NODE_H}" '
            f'rx="10" ry="10" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>',
            f'<rect x="{x:.0f}" y="{y:.0f}" width="5" height="{_NODE_H}" '
            f'rx="2.5" ry="2.5" fill="{ink}"/>',
            f'<text x="{cx:.0f}" y="{y + 23:.0f}" text-anchor="middle" '
            f'font-family="{_MONO}" font-size="9.5" font-weight="700" '
            f'letter-spacing="1" fill="{ink}">{escape(tag.kind.value)}</text>',
            f'<text x="{cx:.0f}" y="{y + 42:.0f}" text-anchor="middle" '
            f'font-family="{_SANS}" font-size="13" font-weight="600" '
            f'fill="#23272f">{escape(_truncate(tag.label))}</text>',
        ]
    )


def _edge(x1: float, y1: float, x2: float, y2: float, verb: str, *, inbound: bool) -> str:
    """A directed edge with an arrowhead + a centred verb label. Inbound edges
    point *towards* the centre node; outbound point away."""
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    return "".join(
        [
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            'stroke="#aeb8c2" stroke-width="1.6" marker-end="url(#rel-arrow)"/>',
            f'<rect x="{mx - 44:.0f}" y="{my - 9:.0f}" width="88" height="18" '
            'rx="9" fill="#fbfdfe"/>',
            f'<text x="{mx:.0f}" y="{my + 4:.0f}" text-anchor="middle" '
            f'font-family="{_MONO}" font-size="9.5" font-weight="700" '
            f'letter-spacing="0.4" fill="#6a7682">{escape(verb)}</text>',
        ]
    )


def render_relationship_graph_svg(
    centre: Tag, outbound: list[Edge], inbound: list[Edge]
) -> str:
    """A self-contained SVG mini-graph: the profiled entity in the middle, with
    outbound neighbours stacked on the right and inbound on the left. Every
    dynamic value is XML-escaped, so a tag label can never inject markup. Returns
    ``""`` when there are no edges (the caller renders nothing)."""
    if not outbound and not inbound:
        return ""

    width, cx = 760, 380
    col_count = max(len(outbound), len(inbound), 1)
    row_h = _NODE_H + 34
    height = max(170, 60 + col_count * row_h)
    cy = height / 2
    left_x, right_x = 120, width - 120

    def _column(edges: list[Edge], x: float, *, inbound: bool) -> str:
        parts: list[str] = []
        n = len(edges)
        for i, e in enumerate(edges):
            ey = (height / 2) + (i - (n - 1) / 2) * row_h
            # edge runs between the centre node and this neighbour
            if inbound:
                ex1, ex2 = x + _NODE_W / 2, cx - _NODE_W / 2
                ey1, ey2 = ey, cy
            else:
                ex1, ex2 = cx + _NODE_W / 2, x - _NODE_W / 2
                ey1, ey2 = cy, ey
            parts.append(_edge(ex1, ey1, ex2, ey2, e.relation_type.value, inbound=inbound))
            parts.append(_node(e.other, x, ey))
        return "".join(parts)

    title = f"Relationship graph for {centre.label}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height:.0f}" '
        f'width="100%" role="img" aria-labelledby="rg-t-{centre.id} rg-d-{centre.id}">',
        f'<title id="rg-t-{centre.id}">{escape(title)}</title>',
        f'<desc id="rg-d-{centre.id}">{escape(_graph_desc(centre, outbound, inbound))}</desc>',
        '<defs><marker id="rel-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#aeb8c2"/></marker></defs>',
        # edges + neighbour nodes first, centre node on top
        _column(inbound, left_x, inbound=True),
        _column(outbound, right_x, inbound=False),
        _node(centre, cx, cy, centre=True),
        "</svg>",
    ]
    return "".join(parts)


def _graph_desc(centre: Tag, outbound: list[Edge], inbound: list[Edge]) -> str:
    bits = [
        f"{centre.label} {e.relation_type.value} {e.other.label}" for e in outbound
    ]
    bits += [
        f"{e.other.label} {e.relation_type.value} {centre.label}" for e in inbound
    ]
    return ". ".join(bits) or "No relationships"
