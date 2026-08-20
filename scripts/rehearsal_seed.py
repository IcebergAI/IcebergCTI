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
import sys
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


def stage_previous() -> int:
    """Bring an empty database to the previous revision and seed it."""

    from alembic import command
    from sqlmodel import select

    from iceberg import db
    from iceberg.models import Notebook, Report, Role, Source, User

    revisions = _revision_chain()
    if len(revisions) < 2:
        print("only one migration exists; seeding at head instead", file=sys.stderr)
        target = revisions[-1]
    else:
        target = revisions[-2]
    command.upgrade(db.alembic_config(), target)
    print(f"staged at previous revision {target}")

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
    print("seeded a user, notebook, source and report")
    return 0


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
    missing = [
        name
        for name, row in (("notebook", notebook), ("report", report), ("source", source))
        if row is None
    ]
    if missing:
        print(f"FAIL: seeded {', '.join(missing)} missing after migration", file=sys.stderr)
        return 1
    print("verified: seeded notebook, source and report are readable at head")
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
