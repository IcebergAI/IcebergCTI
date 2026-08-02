"""Stale-write conflicts in the report editor (#271).

PR #267 made the debounced autosave the *only* save path, so an optimistic-lock
409 can no longer be surfaced by a full-form POST hitting the app-level handler.
Notebook access is deliberately role-wide (#65), so two writers in one report is
a supported case — a conflict that is neither reported nor recoverable silently
discards every keystroke the loser types afterwards.
"""

from pathlib import Path

from sqlmodel import Session

from iceberg.models import Report, ReportStatus


ROOT = Path(__file__).resolve().parents[1]
EDITOR_JS = ROOT / "src/iceberg/static/js/tags.js"
EDITOR_HTML = ROOT / "src/iceberg/templates/report_edit.html"


def _report(client, login) -> dict:
    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Conflict notebook"}).json()
    return client.post(
        "/api/reports",
        json={
            "notebook_id": notebook["id"],
            "title": "Contested report",
            "body_md": "First draft.",
        },
    ).json()


def _save(client, report_id: int, *, version: int, body: str, fetch: bool = True):
    return client.post(
        f"/reports/{report_id}",
        data={"version": str(version), "title": "Contested report", "body_md": body},
        headers={"X-Requested-With": "fetch"} if fetch else {},
    )


def test_stale_autosave_returns_409_with_the_current_version(client, login, engine):
    """Writer A saves; writer B's editor is now a revision behind. B's autosave
    must come back as a distinct, recoverable 409 carrying the current version —
    not a bare error the client cannot tell from a network blip."""
    report = _report(client, login)
    first = _save(client, report["id"], version=report["version"], body="A's edit.")
    assert first.status_code == 200
    assert first.json()["version"] == report["version"] + 1

    stale = _save(client, report["id"], version=report["version"], body="B's edit.")
    assert stale.status_code == 409
    body = stale.json()
    assert body["version"] == report["version"] + 1
    assert "stale" in body["detail"].lower()

    # The losing write left no trace — the report still holds A's content.
    with Session(engine) as session:
        assert session.get(Report, report["id"]).body_md == "A's edit."


def test_conflicted_editor_can_overwrite_with_the_returned_version(client, login, engine):
    """The version handed back is what makes the "overwrite with my version"
    affordance work: re-posting with it saves B's work instead of losing it."""
    report = _report(client, login)
    _save(client, report["id"], version=report["version"], body="A's edit.")
    current = _save(client, report["id"], version=report["version"], body="B's edit.")
    assert current.status_code == 409

    retried = _save(
        client, report["id"], version=current.json()["version"], body="B's edit."
    )
    assert retried.status_code == 200
    with Session(engine) as session:
        assert session.get(Report, report["id"]).body_md == "B's edit."


def test_non_fetch_stale_save_still_uses_the_plain_error(client, login):
    """The JSON shape is for the editor's fetch client only; the <noscript>
    full-form POST keeps the ordinary error path."""
    report = _report(client, login)
    _save(client, report["id"], version=report["version"], body="A's edit.")
    stale = _save(
        client, report["id"], version=report["version"], body="B's edit.", fetch=False
    )
    assert stale.status_code == 409


def test_published_report_conflict_carries_no_version_to_overwrite(client, login, engine):
    """Published products are immutable, so there is nothing to overwrite — that
    409 must NOT hand back a version the client could retry with."""
    report = _report(client, login)
    with Session(engine) as session:
        row = session.get(Report, report["id"])
        row.status = ReportStatus.PUBLISHED
        session.add(row)
        session.commit()

    blocked = _save(client, report["id"], version=report["version"], body="Late edit.")
    assert blocked.status_code == 409
    assert "version" not in blocked.json()


def test_editor_client_surfaces_the_conflict_and_stops_the_retry_loop():
    """The client half of the contract: a 409 is handled before the generic
    ``!res.ok`` bail-out, drives its own state, and halts autosave — otherwise
    every later save re-posts the same stale version and 409s forever."""
    script = EDITOR_JS.read_text()
    save_now = script[script.index("async saveNow()") : script.index("async autosave()")]

    assert "res.status === 409" in save_now
    assert save_now.index("res.status === 409") < save_now.index("if (!res.ok)")
    assert "this.conflict = true" in save_now
    # A successful save clears the state again.
    assert "this.conflict = false" in save_now
    # The debounce and the immediate re-arm both respect the conflict state.
    schedule = script[script.index("scheduleSave()") : script.index("async saveNow()")]
    assert "if (this.conflict) return;" in schedule
    assert "if (!this.conflict && (this.saveQueued" in save_now

    # Both ways out are explicit — nothing resolves a conflict on its own.
    assert "reloadForConflict()" in script
    assert "async overwriteConflict()" in script


def test_editor_template_offers_both_recovery_affordances(client, login):
    """The banner has to be in the rendered editor, announced, and offer the
    reload and overwrite actions."""
    report = _report(client, login)
    page = client.get(f"/reports/{report['id']}/edit").text

    assert 'class="editor-conflict"' in page
    assert 'x-show="conflict"' in page
    assert 'role="alert"' in page
    assert "reloadForConflict()" in page
    assert "overwriteConflict()" in page
    # The chip reports the conflict distinctly from "Unsaved changes".
    assert "Not saved — conflict" in page


def test_conflict_banner_is_not_offered_on_a_read_only_editor(client, login, engine):
    """A published product is immutable, so the editor opens read-only — there is
    no save path and therefore no save-conflict affordance to offer."""
    report = _report(client, login)
    with Session(engine) as session:
        row = session.get(Report, report["id"])
        row.status = ReportStatus.PUBLISHED
        session.add(row)
        session.commit()

    page = client.get(f"/reports/{report['id']}/edit")
    assert page.status_code == 200
    assert "Read-only" in page.text
    assert 'class="editor-conflict"' not in page.text
