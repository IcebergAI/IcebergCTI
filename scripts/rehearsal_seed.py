#!/usr/bin/env python3
"""Stages of the release rehearsal that need the application's own code (#314).

Kept out of ``scripts/release_rehearsal.sh`` because each of these has to reason
about Alembic revisions and Iceberg's own models, which shell cannot do honestly.

Stages
------
``previous``        migrate a database to the *previous* schema revision and seed
                    representative rows, so the rehearsal upgrades something real
``verify``          assert the seeded rows are present and readable at head
``rollback-report`` classify every migration's ``downgrade()`` so the release
                    notes can state the rollback boundary rather than imply one
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SEED_TITLE = "Rehearsal product"
SEED_NOTEBOOK = "Rehearsal notebook"
SEED_EMAIL = "rehearsal@example.invalid"


def _session():
    from sqlmodel import Session

    from iceberg.db import engine

    return Session(engine)


def _revisions_at(ref: str) -> list[str] | None:
    """The migration chain as it stood at ``ref``, oldest first.

    Reads the migration files out of the git object store rather than the work
    tree, so the previous release's schema is taken from the previous release.
    """

    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--",
         "src/iceberg/migrations/versions"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if listed.returncode != 0:
        return None
    down: dict[str, str | None] = {}
    for path in listed.stdout.split():
        if not path.endswith(".py") or path.endswith("__init__.py"):
            continue
        shown = subprocess.run(
            ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, cwd=ROOT
        )
        if shown.returncode != 0:
            continue
        revision = re.search(
            r'^revision(?::\s*str)?\s*=\s*["\'](.+?)["\']', shown.stdout, re.M
        )
        parent = re.search(r"^down_revision.*?=\s*(.+)$", shown.stdout, re.M)
        if not revision:
            continue
        raw = (parent.group(1).strip() if parent else "None").strip("\"'")
        down[revision.group(1)] = None if raw in {"None", ""} else raw
    if not down:
        return None
    children = {parent: rev for rev, parent in down.items() if parent is not None}
    roots = [rev for rev, parent in down.items() if parent is None]
    if not roots:
        return None
    chain = [roots[0]]
    while chain[-1] in children:
        chain.append(children[chain[-1]])
    return chain


def _previous_release() -> tuple[str, str] | None:
    """``(tag, head revision)`` of the newest release tag that is not HEAD.

    The supported upgrade path is "from the last release", not "from the last
    migration": a release carrying three migrations must be entered at the
    schema the previous release shipped, or two of them are never exercised.
    """

    listed = subprocess.run(
        ["git", "tag", "--list", "v*", "--sort=-v:refname"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if listed.returncode != 0:
        return None
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()
    for tag in listed.stdout.split():
        commit = subprocess.run(
            ["git", "rev-parse", f"{tag}^{{commit}}"],
            capture_output=True, text=True, cwd=ROOT,
        ).stdout.strip()
        if not commit or commit == head:
            continue
        chain = _revisions_at(tag)
        if chain:
            return tag, chain[-1]
    return None


def _staging_target() -> tuple[str, str, str]:
    """Where the upgrade rehearsal should start, and how honest that start is.

    Returns ``(revision, description, caveat)``; the caveat is empty when the
    rehearsal really does begin at a released schema.
    """

    previous = _previous_release()
    if previous is not None:
        tag, target = previous
        return target, f"the schema released in {tag}", ""
    # No release has been tagged yet, so there is no supported upgrade path to
    # rehearse. Stage at the penultimate migration to exercise the last one, and
    # say plainly that this is weaker than the real thing rather than letting
    # the record read as though a release upgrade was tested.
    revisions = _revision_chain()
    target = revisions[-2] if len(revisions) > 1 else revisions[-1]
    return (
        target,
        "the penultimate migration (no previous release tag exists)",
        "no previous release tag found; this rehearses the last migration only, "
        "not an upgrade from a released version",
    )


def stage_previous() -> int:
    """Bring an empty database to the previous revision and seed it."""

    from alembic import command
    from sqlmodel import select

    from iceberg import db
    from iceberg.models import Notebook, Report, Role, Source, User

    target, origin, caveat = _staging_target()
    if caveat:
        print(f"NOTE: {caveat}", file=sys.stderr)
    command.upgrade(db.alembic_config(), target)
    print(f"staged at {target} — {origin}")

    with _session() as session:
        user = session.exec(select(User).where(User.email == SEED_EMAIL)).first()
        if user is None:
            user = User(
                email=SEED_EMAIL,
                display_name="Rehearsal analyst",
                role=Role.ANALYST,
                issuer="https://rehearsal.invalid",
                sub="rehearsal-1",
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        notebook = Notebook(title=SEED_NOTEBOOK, topic="upgrade rehearsal", owner_id=user.id)
        session.add(notebook)
        session.commit()
        session.refresh(notebook)
        session.add(
            Source(
                notebook_id=notebook.id,
                title="Rehearsal source",
                reference="https://rehearsal.invalid/source",
                summary="Representative collected material.",
            )
        )
        session.add(
            Report(
                notebook_id=notebook.id,
                author_id=user.id,
                title=SEED_TITLE,
                body_md="Representative finished product for the upgrade rehearsal.",
            )
        )
        session.commit()
        _seed_attachment(session, notebook.id)
    print("seeded a user, notebook, source, report and a file-backed attachment")
    return 0


SEED_ATTACHMENT = "Rehearsal attachment"
SEED_BYTES = b"%PDF-1.4 rehearsal attachment\n"


def _seed_attachment(session, notebook_id: int) -> None:
    """Write a real blob and the row that points at it.

    A backup rehearsal that restores only the database cannot show that a
    deployment is recoverable: the rows reference objects, and
    ``iceberg-verify-files`` has nothing to check unless one exists. Seeding an
    attachment makes the database and the object store one consistency set, the
    way RELEASING.md says they must be backed up.
    """

    import hashlib

    from iceberg.models import Attachment, utcnow
    from iceberg.services import storage

    digest = hashlib.sha256(SEED_BYTES).hexdigest()
    key = storage.new_key(".pdf", sha256=digest)
    staged = Path(tempfile.gettempdir()) / f"rehearsal-{digest[:12]}.pdf"
    staged.write_bytes(SEED_BYTES)
    try:
        storage.get_store("local").put_file(
            "attachment", key, staged, sha256=digest, content_type="application/pdf"
        )
    finally:
        staged.unlink(missing_ok=True)
    session.add(
        Attachment(
            notebook_id=notebook_id,
            title=SEED_ATTACHMENT,
            original_filename="rehearsal.pdf",
            stored_filename=key,
            storage_backend="local",
            storage_key=key,
            content_sha256=digest,
            storage_finalized_at=utcnow(),
            content_type="application/pdf",
            file_size=len(SEED_BYTES),
        )
    )
    session.commit()


def stage_verify() -> int:
    """Prove the seeded rows survived the upgrade (or the restore)."""

    from sqlmodel import select

    from iceberg.models import Notebook, Report, Source

    with _session() as session:
        notebook = session.exec(
            select(Notebook).where(Notebook.title == SEED_NOTEBOOK)
        ).first()
        report = session.exec(select(Report).where(Report.title == SEED_TITLE)).first()
        source = session.exec(select(Source).where(Source.title == "Rehearsal source")).first()
    from iceberg.models import Attachment

    with _session() as session:
        attachment = session.exec(
            select(Attachment).where(Attachment.title == SEED_ATTACHMENT)
        ).first()
        stored = None
        if attachment is not None:
            from iceberg.services import storage

            try:
                stored = storage.get_store(attachment.storage_backend).read_bytes(
                    "attachment", attachment.storage_key,
                    expected_sha256=attachment.content_sha256,
                )
            except Exception as error:  # noqa: BLE001 - reported, not raised
                print(f"attachment object unreadable: {error}", file=sys.stderr)
    missing = [
        name
        for name, row in (
            ("notebook", notebook),
            ("report", report),
            ("source", source),
            ("attachment row", attachment),
        )
        if row is None
    ]
    if attachment is not None and stored != SEED_BYTES:
        # The row surviving while its object does not is the failure this stage
        # exists to catch: a restore that looks complete and is not.
        missing.append("attachment object")
    if missing:
        print(f"FAIL: seeded {', '.join(missing)} missing after migration", file=sys.stderr)
        return 1
    print(
        "verified: seeded notebook, source, report and attachment are readable, "
        "and the attachment's bytes match its recorded digest"
    )
    return 0


def _revision_chain() -> list[str]:
    """Migration revisions in dependency order, oldest first."""

    versions = ROOT / "src" / "iceberg" / "migrations" / "versions"
    down: dict[str, str | None] = {}
    for path in versions.glob("*.py"):
        text = path.read_text()
        revision = re.search(r'^revision(?::\s*str)?\s*=\s*["\'](.+?)["\']', text, re.M)
        parent = re.search(r"^down_revision.*?=\s*(.+)$", text, re.M)
        if not revision:
            continue
        raw = (parent.group(1).strip() if parent else "None").strip("\"'")
        down[revision.group(1)] = None if raw in {"None", ""} else raw
    children = {parent: rev for rev, parent in down.items() if parent is not None}
    roots = [rev for rev, parent in down.items() if parent is None]
    if not roots:
        return sorted(down)
    chain = [roots[0]]
    while chain[-1] in children:
        chain.append(children[chain[-1]])
    return chain


_DESTRUCTIVE = ("drop_table", "drop_column", "drop_constraint", "drop_index")


def _classify(path: Path) -> tuple[str, str]:
    """Classify one migration's ``downgrade()`` for the rollback boundary."""

    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = [
                item
                for item in node.body
                if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant))
            ]
            if not body or all(isinstance(item, ast.Pass) for item in body):
                return "no-op", "downgrade restores nothing (data-only change)"
            source = ast.dump(node)
            if any(call in source for call in _DESTRUCTIVE):
                return "lossy", "downgrade drops schema added by this release"
            return "reversible", "downgrade reverses the schema change"
    return "missing", "migration defines no downgrade()"


def stage_rollback_report() -> int:
    """Say plainly what a rollback would and would not restore."""

    print("Rollback boundary by migration (oldest first):")
    order = {revision: index for index, revision in enumerate(_revision_chain())}
    versions = ROOT / "src" / "iceberg" / "migrations" / "versions"
    rows = []
    for path in sorted(versions.glob("*.py")):
        revision = re.search(r'^revision(?::\s*str)?\s*=\s*["\'](.+?)["\']', path.read_text(), re.M)
        if not revision:
            continue
        verdict, note = _classify(path)
        rows.append((order.get(revision.group(1), 10_000), revision.group(1), verdict, note, path.name))
    for _, revision, verdict, note, name in sorted(rows):
        print(f"  {revision:<16} {verdict:<11} {note}  [{name}]")
    lossy = [row for row in rows if row[2] in {"lossy", "no-op", "missing"}]
    print()
    print(
        f"{len(lossy)} of {len(rows)} migrations do not fully restore the prior state on "
        "downgrade. Roll back by restoring the pre-upgrade backup, not by downgrading."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", required=True, choices=("previous", "verify", "rollback-report")
    )
    args = parser.parse_args()
    if args.stage == "rollback-report":
        # The only stage that needs no database.
        return stage_rollback_report()
    if not os.environ.get("ICEBERG_DATABASE_URL"):
        print("ICEBERG_DATABASE_URL is required for this stage", file=sys.stderr)
        return 2
    return stage_previous() if args.stage == "previous" else stage_verify()


if __name__ == "__main__":
    raise SystemExit(main())
