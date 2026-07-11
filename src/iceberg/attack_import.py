"""CLI for an explicit, reviewed MITRE Enterprise ATT&CK bundle import.

Example:

    iceberg-import-attack --file enterprise-attack.json --update \
      --sha256 <pinned-file-sha256>

The command intentionally accepts a local file only; it does not fetch TAXII,
GitHub, MISP, or arbitrary URLs on behalf of the server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sqlmodel import Session

from .db import engine, run_migrations
from .services.attack_import import import_enterprise_bundle, parse_enterprise_bundle


def _read_bundle(path: str) -> tuple[dict, str]:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read ATT&CK bundle: {source}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("ATT&CK bundle is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("ATT&CK bundle must be a JSON object")
    return payload, hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="iceberg-import-attack",
        description="Import a reviewed local MITRE Enterprise ATT&CK STIX bundle.",
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Pinned local Enterprise ATT&CK STIX JSON bundle; URLs are not accepted.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh existing ATT&CK tags and retire revoked/deprecated techniques.",
    )
    parser.add_argument(
        "--sha256",
        help="Optional expected SHA-256 of the reviewed bundle (recommended).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and summarize the file without changing the database.",
    )
    args = parser.parse_args(argv)

    try:
        bundle, digest = _read_bundle(args.file)
    except ValueError as exc:
        parser.error(str(exc))
    if args.sha256 and digest.lower() != args.sha256.strip().lower():
        parser.error("ATT&CK bundle SHA-256 does not match --sha256")

    techniques = parse_enterprise_bundle(bundle)
    if not techniques:
        parser.error("No Enterprise ATT&CK attack-pattern objects were found")
    if args.check:
        print(f"Validated {len(techniques)} Enterprise ATT&CK technique(s); sha256={digest}")
        return 0

    run_migrations()
    with Session(engine) as session:
        result = import_enterprise_bundle(session, bundle, update=args.update)
    print(
        f"ATT&CK import: {result.created} created, {result.updated} updated, "
        f"{result.retired} retired ({result.discovered} discovered; sha256={digest})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
