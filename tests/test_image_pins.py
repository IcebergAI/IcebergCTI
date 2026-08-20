"""Every input to the runtime image is pinned (#324).

The image is only auditable if building the same commit twice produces the same
thing. Digests cover the base images and the uv binary, a checksum covers the
Typst tarball — and `apt-get upgrade`, which is what keeps the Trivy gate green
between base rebuilds, is pinned to a snapshot.debian.org timestamp rather than
resolving against whatever Debian published this morning.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

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

    # Compare the archive host exactly rather than looking for the name
    # somewhere in the script: a substring check would also pass for a
    # look-alike host that merely contains it.
    archive = re.search(r'^base="([^"]+)"', body, re.M)
    assert archive, "the script must define the snapshot archive base URL"
    assert urlparse(archive.group(1)).hostname == "snapshot.debian.org"


def test_the_snapshot_archive_is_fetched_over_a_bound_transport():
    """The timestamp is only trustworthy if the transport authenticates it.

    APT's signatures prove Debian published a Release set, not *which* one, and
    a snapshot must disable Check-Valid-Until (its Release is stale by design),
    which is the control that would otherwise catch a replay. TLS is what binds
    the answer to the snapshot that was asked for, so http here would let a
    network attacker serve a validly signed older archive state and leave the
    image with packages the pinned timestamp does not describe.
    """

    body = SNAPSHOT_SCRIPT.read_text()
    archive = re.search(r'^base="([^"]+)"', body, re.M)
    assert archive, "the script must define the snapshot archive base URL"
    assert urlparse(archive.group(1)).scheme == "https"
    assert "http://snapshot.debian.org" not in body
    # A snapshot's Release file is older than apt's freshness window by design.
    assert 'Acquire::Check-Valid-Until "false"' in body
    assert SNAPSHOT_SCRIPT.stat().st_mode & 0o111, "the script must be executable"


def test_the_built_image_records_which_snapshot_it_came_from():
    """`docker inspect` should answer this without the source tree."""

    assert 'LABEL io.iceberg.apt-snapshot="${APT_SNAPSHOT}"' in DOCKERFILE


CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Copied into the image but unable to change whether the build or the image scan
# succeeds, so the docker job is not worth running for an edit to it.
BUILD_FILTER_EXEMPT = {"README.md"}


def _build_affecting_pattern() -> str:
    match = re.search(r"grep -qE '\^\((.+?)\)'", CI_WORKFLOW.read_text())
    assert match, "the CI docker filter must be a single anchored grep pattern"
    return match.group(1)


def _copy_sources() -> list[str]:
    """Host paths the Dockerfile copies in, excluding stage/image copies."""

    sources = []
    for line in DOCKERFILE.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or "--from=" in stripped:
            continue
        # The final token is the destination inside the image.
        sources.extend(stripped.split()[1:-1])
    return sources


def test_ci_builds_the_image_when_any_copied_input_changes():
    """A build input CI does not watch is an input that ships unbuilt.

    `docker/apt-snapshot.sh` was exactly that: added with the snapshot pin,
    executed before every apt-get in both stages, and absent from the filter —
    so editing the script skipped the only job that builds the image.
    """

    pattern = re.compile(f"^({_build_affecting_pattern()})")
    for source in _copy_sources():
        if source in BUILD_FILTER_EXEMPT:
            continue
        path = ROOT / source
        # A directory changes via the files under it, so test a real member.
        if path.is_dir():
            member = next((p for p in sorted(path.rglob("*.py")) if p.is_file()), None)
            assert member, f"no representative file under {source}"
            source = str(member.relative_to(ROOT))
        assert pattern.match(source), (
            f"{source} is copied into the image but CI would not rebuild it"
        )
