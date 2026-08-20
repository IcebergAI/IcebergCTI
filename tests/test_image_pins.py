"""Every input to the runtime image is pinned (#324).

The image is only auditable if building the same commit twice produces the same
thing. Digests cover the base images and the uv binary, a checksum covers the
Typst tarball — and `apt-get upgrade`, which is what keeps the Trivy gate green
between base rebuilds, is pinned to a snapshot.debian.org timestamp rather than
resolving against whatever Debian published this morning.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
SNAPSHOT_SCRIPT = ROOT / "docker" / "apt-snapshot.sh"


def test_the_apt_snapshot_is_pinned_to_a_timestamp():
    match = re.search(r"^ARG APT_SNAPSHOT=(\S+)", DOCKERFILE, re.M)
    assert match, "the image must declare a default APT snapshot"
    assert re.fullmatch(r"\d{8}T\d{6}Z", match.group(1)), (
        f"{match.group(1)!r} is not a snapshot.debian.org timestamp"
    )


def test_every_apt_stage_repoints_at_the_snapshot_first():
    """An `apt-get` that runs before the repoint would read the live mirror."""

    stages = [block for block in DOCKERFILE.split("\nFROM ")[1:]]
    for stage in stages:
        if "apt-get" not in stage:
            continue
        assert "apt-snapshot" in stage, "a stage runs apt-get without pinning it"
        assert stage.index("apt-snapshot") < stage.index("apt-get update"), (
            "apt-get runs before the snapshot is applied"
        )


def test_the_upgrade_is_not_left_reading_the_live_mirror():
    """The upgrade is the step that made the image unreproducible (#324)."""

    assert "apt-get upgrade" in DOCKERFILE, (
        "the upgrade closes the window between a Debian fix and a base rebuild; "
        "removing it re-opens the Trivy gate failure it was added for"
    )
    upgrade_stage = DOCKERFILE.split("\nFROM ")[-1]
    assert "apt-snapshot" in upgrade_stage


def test_the_snapshot_script_takes_the_suite_from_the_image():
    """Hard-coding the Debian suite would silently rot at the next base bump."""

    body = SNAPSHOT_SCRIPT.read_text()
    assert "VERSION_CODENAME" in body
    assert "snapshot.debian.org" in body
    # A snapshot's Release file is older than apt's freshness window by design.
    assert 'Acquire::Check-Valid-Until "false"' in body
    assert SNAPSHOT_SCRIPT.stat().st_mode & 0o111, "the script must be executable"


def test_the_built_image_records_which_snapshot_it_came_from():
    """`docker inspect` should answer this without the source tree."""

    assert 'LABEL io.iceberg.apt-snapshot="${APT_SNAPSHOT}"' in DOCKERFILE
