# Releasing IcebergCTI

A release is **a git tag**. Pushing a `v*` tag to `main` is the whole release:
[`.github/workflows/release.yml`](../.github/workflows/release.yml) fires on the tag and does
the rest — verifies the tag, builds and pushes the container image to GHCR with an SBOM + SLSA
provenance attestation + a keyless cosign signature, and creates the GitHub Release.

The version lives in **one place**: `[project].version` in `pyproject.toml`. Everything else
(the tag, the image tags, the changelog heading) is derived from it.

## The two spellings of the same version

Python (PEP 440) and SemVer disagree on how to spell a pre-release, so the same version has two
forms and you must use the right one in the right place:

| `pyproject.toml` (PEP 440) | git tag / changelog heading (SemVer) |
|---|---|
| `0.1.0` | `v0.1.0` |
| `0.1.0b1` | `v0.1.0-beta.1` |
| `0.1.0rc1` | `v0.1.0-rc.1` |
| `0.1.0a1` | `v0.1.0-alpha.1` |

**pyproject gets the PEP 440 form; the tag and the changelog heading get the SemVer form.**
`release.yml` normalises the pyproject version to SemVer and **fails the release if the tag
disagrees**, so a tag can never ship an image labelled a different version.

## Cutting a release

1. **Bump the version** in `pyproject.toml` (PEP 440 form).
2. **Refresh the lockfile** — `uv lock`, and commit `uv.lock`. `uv.lock` records the project's
   *own* version, so skipping this makes CI's `uv sync --locked` / `uv lock --check` fail. This is
   the single most common way to break the build here.
3. **Close out the changelog.** Rename the `[Unreleased]` section to the released version and date
   it, then open a fresh `[Unreleased]` above it:

   ```markdown
   ## [Unreleased]

   ## [0.1.0] — 2026-07-21
   ```
4. **Open a PR** with the bump + lock + changelog, and merge it once CI is green.
5. **Tag the merge commit** on `main`, in the **SemVer** spelling, and push the tag:

   ```bash
   git checkout main && git pull
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

That's it. `release.yml` then:

- **verifies the tag matches `pyproject.toml`** (normalising PEP 440 → SemVer) and **refuses a
  commit that isn't on `main`** (so a tag on an unmerged branch can't publish the sole deployable
  image, bypassing review + CI);
- builds and pushes `ghcr.io/icebergai/icebergcti` with SemVer tags (`{{version}}`,
  `{{major}}.{{minor}}`, `type=sha`; `:latest` and `major.minor` only for a stable, non-pre-release
  tag) **with an SBOM and SLSA `mode=max` provenance**;
- **attests** the build provenance to the registry and **cosign-signs** the image (keyless / OIDC);
- creates the **GitHub Release** with auto-generated notes (a `-suffix` SemVer tag is marked
  `--prerelease`).

### Dry run

`release.yml` also has a `workflow_dispatch` trigger that does a **build-only dry run** — it builds
the image but does not push, sign, attest, or create a release. Use it to check the Dockerfile
builds cleanly under buildx before tagging.

## Before you tag: rehearse it

A release automation that has never been run is a plan, not a release. Rehearse the exact commit
you are about to tag (#314):

```bash
# In CI: Actions → "Release rehearsal" → Run workflow (on the commit to be tagged).
# Locally, with docker and a PostgreSQL you can throw away:
scripts/release_rehearsal.sh
```

It proves four things and stops at the first failure, printing a **rehearsal record** worth
attaching to the release:

1. **Clean install** — migrate an empty database to head, boot the image, `/healthz` + `/readyz`
   come up.
2. **Seeded upgrade** — stage a database at the *previous* schema revision, seed a user, notebook,
   source and report, migrate forward with this release's job, and assert the seeded rows are still
   readable. This is the upgrade an operator actually performs.
3. **Backup and verified restore** — `pg_dump` the upgraded database, restore into a fresh one, and
   run `iceberg-verify-files` plus the seeded-data check against the restored release.
4. **Rollback boundary** — classify every migration's `downgrade()` and report how many do not
   restore prior state.

`release.yml`'s `workflow_dispatch` **dry run** is the complementary check: it builds the image
under buildx without pushing, signing or releasing anything.

## After you tag: verify what was published

```bash
scripts/verify_release.sh v0.1.0-beta.1
```

It resolves the tag to an **immutable digest**, verifies the **cosign signature** was issued to
this repository's release workflow *for that tag*, verifies the **SLSA provenance** attestation,
and checks the image's `org.opencontainers.image.revision` label is the commit the tag points at —
so the artifact, its signature, its provenance and its source revision all agree, or the script
fails.

## Compatibility and support

**Versioning.** [SemVer](https://semver.org/). Until 1.0 the public surfaces — the JSON API, the
TAXII surface, the STIX bundle shape, environment variable names and the container's operational
contract — may change in a minor release; such changes are called out in the changelog. A
pre-release tag (`-beta.N`, `-rc.N`) never becomes `:latest` and is marked as a pre-release on
GitHub.

**Datastore.** PostgreSQL is the supported datastore for every container/production deployment.
SQLite is local dev/test only — the prod app refuses to boot on it and the image ships no SQLite
fallback.

**Upgrades.** One release forward at a time, migrations run as an explicit deploy step
(`ICEBERG_AUTO_MIGRATE=false`, the migration Job), and the schema is migrated *before* the new
image serves traffic. Skipping releases is not rehearsed and not supported.

**Rollback.** Most migrations add schema; their `downgrade()` drops it and takes the data in those
columns with it, and a data-only migration cannot restore what it rewrote at all. The supported
rollback is therefore **restore the pre-upgrade backup and redeploy the previous digest**, not
`alembic downgrade`. Run `python scripts/rehearsal_seed.py --stage rollback-report` for the
per-migration classification.

**Backups.** The database and the object prefix form **one consistency set** — a dump without its
matching object snapshot is not a backup. The full quiesce/backup/restore runbook is in
[`deploy/k8s/README.md`](../deploy/k8s/README.md#backup--restore); rehearse it with the release, not
after an incident.

### Known limitations of a beta

- The public API/TAXII/STIX surfaces are not yet frozen (see *Versioning* above).
- Downgrade is not supported; roll back by restore.
- Skipping intermediate releases on upgrade is untested.
- Optional integrations (AI providers, MISP, SIEM, S3-compatible storage, OIDC providers other than
  the ones exercised in CI) are validated against fixtures and test doubles, not against every
  vendor implementation.

## Deploying a release

The Kubernetes manifests under [`deploy/k8s/`](../deploy/k8s/) reference
`ghcr.io/icebergai/icebergcti`. In production, pin an **immutable digest** rather than `:latest` —
[`deploy/k8s/release.sh`](../deploy/k8s/release.sh) takes an `IMAGE=ghcr.io/icebergai/icebergcti@sha256:<digest>`
(runs the migration Job, then rolls the Deployment). Get the digest from the published GHCR image
or the GitHub Release notes.

Verify the signature and provenance before deploying:

```bash
cosign verify ghcr.io/icebergai/icebergcti@sha256:<digest> \
  --certificate-identity-regexp '^https://github\.com/IcebergAI/IcebergCTI/\.github/workflows/release\.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/icebergai/icebergcti@sha256:<digest> --repo IcebergAI/IcebergCTI
```
