"""The /help page: per-role guide rendering, cross-role browsing, the bad-param
fallback, glossary coverage, the nav link + contextual deep-links, the anonymous
redirect, and the content-module invariants."""

import pytest
from markupsafe import escape

from iceberg import help_content
from iceberg.models import Role


def _rendered(text: str) -> str:
    """How a string appears once Jinja has autoescaped it (apostrophes etc.)."""
    return str(escape(text))

ROLES = ["ANALYST", "REVIEWER", "STAKEHOLDER", "ADMIN"]


# --------------------------------------------------------------------------- #
# Per-role rendering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ROLES)
def test_help_renders_for_each_role(client, login, role):
    login(role)
    resp = client.get("/help")
    assert resp.status_code == 200, resp.text
    guide = help_content.guide_for(Role(role))
    # The page leads with the viewer's own role guide and marks its tab "you".
    assert _rendered(guide.tagline) in resp.text
    assert "help-tab-you" in resp.text


def test_help_defaults_to_viewer_role(client, login):
    login("STAKEHOLDER")
    html = client.get("/help").text
    # The stakeholder guide leads; the analyst-only tagline is not the active one.
    assert _rendered(help_content.guide_for(Role.STAKEHOLDER).tagline) in html


# --------------------------------------------------------------------------- #
# Cross-role browsing + bad-param fallback
# --------------------------------------------------------------------------- #
def test_help_is_browsable_across_roles(client, login):
    login("ANALYST")
    html = client.get("/help?role=STAKEHOLDER").text
    assert _rendered(help_content.guide_for(Role.STAKEHOLDER).tagline) in html


def test_help_bad_role_falls_back_to_viewer(client, login):
    login("ANALYST")
    resp = client.get("/help?role=BOGUS")
    assert resp.status_code == 200
    assert _rendered(help_content.guide_for(Role.ANALYST).tagline) in resp.text


# --------------------------------------------------------------------------- #
# Glossary coverage + deep-link anchors
# --------------------------------------------------------------------------- #
def test_help_glossary_covers_core_concepts(client, login):
    login("ANALYST")
    html = client.get("/help").text
    for slug in (
        "intel-levels",
        "tlp",
        "notebooks",
        "source-grading",
        "diamond-model",
        "icd-203",
        "lifecycle",
        "requirements",
        "dissemination",
        "tags",
    ):
        # Each concept renders an anchor id the contextual deep-links target.
        assert f'id="{slug}"' in html


# --------------------------------------------------------------------------- #
# Nav link + contextual deep-links
# --------------------------------------------------------------------------- #
def test_nav_shows_help_link(client, login):
    login("ANALYST")
    assert 'href="/help"' in client.get("/").text


def test_contextual_deeplinks_present(client, login):
    """Representative screens carry a "?"/hint that jumps to the right section."""
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    rid = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "R"}
    ).json()["id"]
    editor = client.get(f"/reports/{rid}/edit").text
    assert "/help#icd-203" in editor
    assert "/help#diamond-model" in editor

    login("STAKEHOLDER", email="s@example.com")
    assert "/help#dissemination" in client.get("/feed").text


# --------------------------------------------------------------------------- #
# Anonymous access
# --------------------------------------------------------------------------- #
def test_help_requires_auth(client):
    resp = client.get(
        "/help", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


# --------------------------------------------------------------------------- #
# Content-module invariants (no HTTP client)
# --------------------------------------------------------------------------- #
def test_every_role_has_exactly_one_guide():
    roles = [g.role for g in help_content.ROLE_GUIDES]
    assert sorted(roles) == sorted(Role)
    assert len(roles) == len(set(roles))


def test_guide_concept_slugs_resolve():
    valid = {c.slug for c in help_content.CONCEPTS}
    for guide in help_content.ROLE_GUIDES:
        for slug in guide.concepts:
            assert slug in valid, f"{guide.role} references unknown concept {slug!r}"


def test_concept_slugs_are_unique():
    slugs = [c.slug for c in help_content.CONCEPTS]
    assert len(slugs) == len(set(slugs))
