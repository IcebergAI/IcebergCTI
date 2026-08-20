"""The beta release's rehearsal and verification tooling (#314).

The rehearsal exists to catch an unshippable release *before* a tag is pushed, so
the tooling itself is covered here rather than trusted to work on the day.
"""

import re
import subprocess
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from iceberg import db
from iceberg.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "scripts" / "rehearsal_seed.py"
SCRIPTS = (ROOT / "scripts" / "release_rehearsal.sh", ROOT / "scripts" / "verify_release.sh")


def _run(*args, env=None, cwd=ROOT):
    return subprocess.run(
        [sys.executable, str(SEED), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


# --------------------------------------------------------------------------- #
# The migration history the rehearsal walks
# --------------------------------------------------------------------------- #
def test_the_migration_history_is_a_single_ordered_chain():
    """A second head would break both the rehearsal and a real upgrade."""

    sys.path.insert(0, str(ROOT / "scripts"))
    import rehearsal_seed

    chain = rehearsal_seed._revision_chain()
    versions = list((ROOT / "src" / "iceberg" / "migrations" / "versions").glob("*.py"))

    assert len(chain) == len(versions), "every migration must be reachable from the root"
    assert len(set(chain)) == len(chain), "revision ids must be unique"


def test_the_rollback_report_classifies_every_migration():
    result = _run("--stage", "rollback-report")

    assert result.returncode == 0, result.stderr
    body = result.stdout
    # The data-only backfill restores nothing; a table-creating migration is lossy.
    assert re.search(r"b8c9d0e1f2a3\s+no-op", body)
    assert re.search(r"e1f2a3b4c5d6\s+lossy", body)
    assert "Roll back by restoring the pre-upgrade backup" in body
    assert "missing" not in body, "every migration should define a downgrade()"


def test_the_rollback_report_needs_no_database(monkeypatch):
    """It is a static reading of the migration files, so it always works."""

    env = {"PATH": "/usr/bin:/bin"}
    result = _run("--stage", "rollback-report", env=env)

    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# The upgrade the rehearsal performs
# --------------------------------------------------------------------------- #
def test_seeded_data_survives_the_upgrade_to_head(tmp_path, monkeypatch):
    """Stage at the previous revision, seed, migrate forward, and read it back.

    This is the rehearsal's second stage, run here against SQLite so it is part
    of the ordinary suite; CI runs the same stages against PostgreSQL.
    """

    url = f"sqlite:///{tmp_path / 'rehearsal.db'}"
    env = {
        "PATH": "/usr/bin:/bin",
        "ICEBERG_DATABASE_URL": url,
        "ICEBERG_SECRET_KEY": "rehearsal-secret-0123456789abcdef0123456789",
        "ICEBERG_ENVIRONMENT": "dev",
        "ICEBERG_ATTACHMENTS_DIR": str(tmp_path / "attachments"),
    }

    staged = _run("--stage", "previous", env=env)
    assert staged.returncode == 0, staged.stderr
    assert "staged at" in staged.stdout

    monkeypatch.setattr(get_settings(), "database_url", url)
    command.upgrade(db.alembic_config(), "head")

    verified = _run("--stage", "verify", env=env)
    assert verified.returncode == 0, verified.stderr
    assert "are readable" in verified.stdout

    engine = sa.create_engine(url)
    with engine.connect() as conn:
        revision = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    engine.dispose()
    sys.path.insert(0, str(ROOT / "scripts"))
    import rehearsal_seed

    assert revision == rehearsal_seed._revision_chain()[-1]


def test_verify_fails_loudly_when_the_data_is_not_there(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    env = {
        "PATH": "/usr/bin:/bin",
        "ICEBERG_DATABASE_URL": url,
        "ICEBERG_SECRET_KEY": "rehearsal-secret-0123456789abcdef0123456789",
        "ICEBERG_ENVIRONMENT": "dev",
    }
    command.upgrade(db.alembic_config(), "head")  # schema only, no seed

    result = _run("--stage", "verify", env=env)

    assert result.returncode != 0


def test_a_stage_needing_a_database_says_so(tmp_path):
    result = _run("--stage", "verify", env={"PATH": "/usr/bin:/bin"})

    assert result.returncode == 2
    assert "ICEBERG_DATABASE_URL is required" in result.stderr


# --------------------------------------------------------------------------- #
# The scripts themselves
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_the_release_scripts_are_executable_and_valid_shell(script):
    assert script.exists(), script
    assert script.stat().st_mode & 0o111, f"{script.name} must be executable"
    checked = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    body = script.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    # Fail fast: a rehearsal that continues past a failed step proves nothing.
    assert "set -euo pipefail" in body


def test_verification_refuses_an_image_that_makes_no_revision_claim():
    """A missing assertion is a failed check, not a skipped one.

    Treating "no label" as "label agrees" would print Verified for an image
    whose source commit nobody can establish.
    """

    body = (ROOT / "scripts" / "verify_release.sh").read_text()
    assert 'if [ -z "$IMAGE_COMMIT" ]; then' in body
    assert "UNVERIFIABLE" in body
    # ... and the mismatch branch must no longer be conditional on the label
    # being present, which is what let an empty one through.
    assert '[ -n "$IMAGE_COMMIT" ] && [ "$IMAGE_COMMIT" != "$TAG_COMMIT" ]' not in body


def test_the_backup_rehearsal_covers_the_object_store():
    """Restoring rows whose blobs are gone is a restore that only looks complete."""

    body = (ROOT / "scripts" / "release_rehearsal.sh").read_text()
    assert "rehearsal-objects.tar" in body, "objects are never archived"
    assert body.count("rehearsal-objects.tar") >= 2, "archived but never restored"
    assert "objects_dir rehearsal_restore" in body
    # The image and the seeder have to share one object root, or the seeded
    # blob is invisible to the verifier running inside the container.
    assert "ICEBERG_ATTACHMENTS_DIR=/data/attachments" in body
    assert 'ICEBERG_ATTACHMENTS_DIR="$(objects_dir "$1")"' in body


def _seed_module():
    """Import the seeder as a module so its pure logic can be exercised."""

    spec = importlib.util.spec_from_file_location("rehearsal_seed", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_rehearsal_starts_from_the_previous_release_when_one_exists(monkeypatch):
    """The supported upgrade path is "from the last release".

    Entering at the penultimate *migration* is a different, weaker claim: a
    release carrying three migrations would leave two of them untested.
    """

    seed = _seed_module()
    monkeypatch.setattr(seed, "_previous_release", lambda: ("v9.9.9", "deadbeef"))

    target, origin, caveat = seed._staging_target()

    assert target == "deadbeef"
    assert "v9.9.9" in origin
    assert caveat == "", "starting from a real release needs no caveat"


def test_without_a_release_tag_the_rehearsal_says_what_it_did_not_prove(monkeypatch):
    seed = _seed_module()
    monkeypatch.setattr(seed, "_previous_release", lambda: None)
    monkeypatch.setattr(seed, "_revision_chain", lambda: ["one", "two", "three"])

    target, origin, caveat = seed._staging_target()

    assert target == "two", "should stage at the penultimate migration"
    assert "no previous release tag" in origin
    assert "not an upgrade from a released version" in caveat


def test_the_previous_release_is_read_from_the_tag_not_the_work_tree():
    """The chain is reconstructed from git objects, so it reflects that tag."""

    seed = _seed_module()

    assert seed._revisions_at("HEAD") == seed._revision_chain()
    assert seed._revisions_at("v0.0.0-does-not-exist") is None


# --------------------------------------------------------------------------- #
# The version the release will carry
# --------------------------------------------------------------------------- #
_PRERELEASE = {"a": "alpha", "b": "beta", "rc": "rc"}


def _pep440_to_semver(version: str) -> str:
    """The same normalisation `release.yml` applies before comparing the tag."""

    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    assert match, f"{version} is not a release-workflow-acceptable version"
    base, kind, number = match.groups()
    if not kind:
        return f"v{base}"
    return f"v{base}-{_PRERELEASE[kind]}.{number}"


def test_the_project_version_and_changelog_agree():
    """`release.yml` refuses a tag that disagrees with pyproject; catch it here."""

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    semver = _pep440_to_semver(version)
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert f"## [{semver.lstrip('v')}]" in changelog, (
        f"CHANGELOG has no heading for {semver}"
    )
    assert f"[{semver.lstrip('v')}]: https://github.com/IcebergAI/IcebergCTI" in changelog


def test_the_lockfile_records_the_project_version():
    """`uv sync --locked` fails in CI when the bump skips `uv lock`."""

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    lock = (ROOT / "uv.lock").read_text()

    assert re.search(rf'name = "iceberg"\nversion = "{re.escape(version)}"', lock)
