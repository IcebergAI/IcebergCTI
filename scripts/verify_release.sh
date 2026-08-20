#!/usr/bin/env bash
# Verify a published IcebergCTI release before deploying it (#314).
#
# Checks that the four things a release claims actually agree with each other:
#
#   digest      the tag resolves to an immutable digest
#   signature   cosign verifies it, issued to this repo's release workflow for this tag
#   provenance  the SLSA attestation verifies, and names the same source revision
#   version     the image's source revision is the commit the tag points at
#
# Usage:  scripts/verify_release.sh v0.1.0-beta.1 [IMAGE_REPO]
#
# Requires: cosign, gh, docker (or crane/skopeo for the digest step).
set -euo pipefail

TAG="${1:?usage: verify_release.sh <git tag> [image repo]}"
REPO_IMAGE="${2:-ghcr.io/icebergai/icebergcti}"
GITHUB_REPO="${GITHUB_REPOSITORY:-IcebergAI/IcebergCTI}"
IMAGE_TAG="${TAG#v}"

fact() { printf '  %-22s %s\n' "$1" "$2"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "Resolve the digest"
docker pull --quiet "$REPO_IMAGE:$IMAGE_TAG" >/dev/null
DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "$REPO_IMAGE:$IMAGE_TAG" | cut -d@ -f2)"
fact "image" "$REPO_IMAGE:$IMAGE_TAG"
fact "digest" "$DIGEST"

step "Verify the signature"
cosign verify "$REPO_IMAGE@$DIGEST" \
  --certificate-identity-regexp "^https://github\.com/${GITHUB_REPO}/\.github/workflows/release\.yml@refs/tags/${TAG}$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null
fact "signature" "verified for $TAG"

step "Verify the provenance"
gh attestation verify "oci://$REPO_IMAGE@$DIGEST" --repo "$GITHUB_REPO" >/dev/null
fact "provenance" "verified"

step "Agree on the source revision"
TAG_COMMIT="$(git rev-parse "${TAG}^{commit}")"
IMAGE_COMMIT="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$REPO_IMAGE:$IMAGE_TAG")"
fact "tag commit" "$TAG_COMMIT"
fact "image revision" "${IMAGE_COMMIT:-<unlabelled>}"
# An absent label is a failed check, not a skipped one. Treating "no claim" as
# "claim agrees" would let this script print Verified for an image whose source
# revision nobody can establish — the opposite of what it exists to say.
if [ -z "$IMAGE_COMMIT" ]; then
  echo "UNVERIFIABLE: the image carries no org.opencontainers.image.revision label," >&2
  echo "so the commit it was built from cannot be checked against the tag" >&2
  exit 1
fi
if [ "$IMAGE_COMMIT" != "$TAG_COMMIT" ]; then
  echo "MISMATCH: the image was built from a different commit than the tag" >&2
  exit 1
fi

step "Verified"
fact "release" "$TAG"
fact "deploy with" "$REPO_IMAGE@$DIGEST"
