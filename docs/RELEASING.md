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

## Deploying a release

The Kubernetes manifests under [`deploy/k8s/`](../deploy/k8s/) reference
`ghcr.io/icebergai/icebergcti`. In production, pin an **immutable digest** rather than `:latest` —
[`deploy/k8s/release.sh`](../deploy/k8s/release.sh) takes an `IMAGE=ghcr.io/icebergai/icebergcti@sha256:<digest>`
(runs the migration Job, then rolls the Deployment). Get the digest from the published GHCR image
or the GitHub Release notes.

Verify the signature and provenance before deploying:

```bash
cosign verify ghcr.io/icebergai/icebergcti@sha256:<digest> \
  --certificate-identity-regexp 'https://github.com/IcebergAI/IcebergCTI/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/icebergai/icebergcti@sha256:<digest> --repo IcebergAI/IcebergCTI
```
