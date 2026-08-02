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


# --------------------------------------------------------------------------- #
# A lifecycle transition must not race the autosave debounce (#278)
# --------------------------------------------------------------------------- #
def test_transition_forms_flush_the_autosave_before_navigating():
    """A transition POST navigates away, dropping a pending 1.2s debounce. For
    publish that is unrecoverable: the snapshot freezes the PRE-EDIT text and
    `report_save`'s not-published guard means it can never be corrected. So the
    transition must await the flush first — the pattern the AI panel already
    uses — and must NOT navigate if the flush fails."""
    script = EDITOR_JS.read_text()
    handler = script[
        script.index("async submitTransition(event)") : script.index("async refresh()")
    ]

    assert "await this.saveNow()" in handler
    # The flush is awaited before the form is handed over, not after.
    assert handler.index("await this.saveNow()") < handler.index("form.submit()")
    # A failed flush aborts the transition rather than publishing stale content.
    assert "if (!saved)" in handler
    assert handler.index("if (!saved)") < handler.index("form.submit()")
    # The pending debounce is cancelled so it cannot fire mid-navigation.
    assert "clearTimeout(this.saveTimer)" in handler
    # A conflict is called out specifically — "try again" would be a dead end.
    assert "this.conflict" in handler


def test_transition_forms_are_wired_to_the_flush_in_the_editor(client, login):
    report = _report(client, login)
    page = client.get(f"/reports/{report['id']}/edit").text

    assert page.count('@submit.prevent="submitTransition($event)"') >= 1
    # Every transition form goes through it — none may post directly.
    transition_forms = page.count(f'action="/reports/{report["id"]}/transition"')
    assert transition_forms >= 1
    assert page.count("submitTransition($event)") == transition_forms
    # The refusal reason has somewhere to render.
    assert 'class="dock-foot-error"' in page


def test_a_reviewer_transition_does_not_attempt_an_author_only_save(client, login):
    """`canEdit` gates the flush: a reviewer approving someone else's report
    cannot save it (author-only), so attempting one would 403 and block a
    perfectly legal transition."""
    script = EDITOR_JS.read_text()
    handler = script[
        script.index("async submitTransition(event)") : script.index("async refresh()")
    ]
    assert "this.canEdit" in handler
    assert handler.index("this.canEdit") < handler.index("await this.saveNow()")

    # And the server still enforces the roles regardless of what the client does.
    report = _report(client, login)
    login("REVIEWER", email="reviewer@example.com")
    blocked = client.post(
        f"/reports/{report['id']}/transition",
        data={"target": "PUBLISHED"},
        follow_redirects=False,
    )
    assert blocked.status_code in (400, 403, 409)


def test_transition_flush_repeats_while_a_save_is_in_flight():
    """Post-merge review fix for #278: with a save already in flight, saveNow()
    returns THAT promise — whose FormData snapshot predates any keystrokes typed
    since it started — and merely queues a follow-up. A single await therefore
    narrowed the publish race to one save RTT without closing it, and the queued
    follow-up is a plain fetch the navigation aborts. The handler must repeat
    the flush until nothing is dirty or in flight, bounded so a persistent
    failure surfaces instead of spinning."""
    script = EDITOR_JS.read_text()
    handler = script[
        script.index("async submitTransition(event)") : script.index("async refresh()")
    ]

    assert "(this.dirty || this.savePromise) && attempt < 5" in handler
    # After the loop, a leftover queued save or re-armed timer must not fire a
    # doomed fetch into the navigation.
    assert handler.count("clearTimeout(this.saveTimer)") >= 2
    assert "this.saveQueued = false" in handler
    # Exiting the loop still dirty (cap hit / failure) refuses the transition.
    assert "if (!flushed || this.dirty || this.savePromise)" in handler


def test_conflict_recovery_drops_a_stale_queued_save():
    """A save queued behind the one that 409'd would re-post the same stale
    version the moment recovery re-enabled saving; the 409 branch clears it."""
    script = EDITOR_JS.read_text()
    save_now = script[script.index("async saveNow()") : script.index("async autosave()")]
    conflict_branch = save_now[
        save_now.index("res.status === 409") : save_now.index("if (!res.ok)")
    ]
    assert "this.saveQueued = false" in conflict_branch
