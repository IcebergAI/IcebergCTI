"""CLI: import the controlled tag taxonomy into the configured database.

    python -m iceberg.seed                 # import the bundled starter taxonomy
    python -m iceberg.seed --file tags.json # import a custom taxonomy file
    python -m iceberg.seed --update         # also refresh metadata on existing tags
    python -m iceberg.seed --list           # show the taxonomy without writing

The target database is the one configured via ``ICEBERG_DATABASE_URL``. Importing
is idempotent (tags are matched on kind + slug), so it is safe to re-run — e.g.
after adding entries to the catalog. First-run seeding still happens automatically
in ``init_db``; this command is the explicit, repeatable import step.
"""

import argparse
import json
from collections import Counter

from sqlmodel import Session

from .db import engine, run_migrations
from .services.tags import load_starter_tags, seed_default_taxonomy


def _load(path: str | None) -> list[dict]:
    if path:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return load_starter_tags()


def _summary(entries: list[dict]) -> str:
    by_kind = Counter(e["kind"] for e in entries)
    return ", ".join(f"{k}: {n}" for k, n in sorted(by_kind.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="iceberg.seed",
        description="Import the controlled tag taxonomy into the database.",
    )
    parser.add_argument(
        "--file",
        help="Path to a taxonomy JSON file (defaults to the bundled starter set).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh external_id/description on tags that already exist.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="Print a summary of the taxonomy without writing to the database.",
    )
    args = parser.parse_args(argv)

    entries = _load(args.file)
    summary = _summary(entries)

    if args.list_only:
        print(f"{len(entries)} tag(s) — {summary}")
        return 0

    run_migrations()  # ensure the schema exists / is up to date
    with Session(engine) as session:
        created = seed_default_taxonomy(session, entries, update=args.update)
    print(
        f"Imported {created} new tag(s); {len(entries) - created} already present "
        f"({len(entries)} total — {summary})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
