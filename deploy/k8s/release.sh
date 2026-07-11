#!/bin/sh
set -eu

: "${IMAGE:?Set IMAGE to the immutable application image digest}"
: "${RELEASE:?Set RELEASE to a unique release identifier}"
: "${MIGRATION_TIMEOUT:=10m}"

case "$IMAGE" in
  *@sha256:*) ;;
  *) echo "IMAGE must include an immutable @sha256 digest" >&2; exit 2 ;;
esac
case "$RELEASE" in
  *[!a-z0-9-]*|'') echo "RELEASE must contain only lowercase letters, digits, and hyphens" >&2; exit 2 ;;
esac

job="iceberg-migrate-$RELEASE"
if kubectl get job "$job" >/dev/null 2>&1; then
  echo "Migration job $job already exists; choose a new RELEASE" >&2
  exit 2
fi

sed -e "s/name: iceberg-migrate/name: $job/" \
    -e "s|image: ghcr.io/theslopbucket/iceberg:latest|image: $IMAGE|" \
    "$(dirname "$0")/migrate-job.yaml" | kubectl create -f -

if ! kubectl wait --for=condition=complete --timeout="$MIGRATION_TIMEOUT" "job/$job"; then
  kubectl logs "job/$job" --all-containers=true || true
  exit 1
fi

kubectl set image deployment/iceberg "iceberg=$IMAGE"
kubectl rollout status deployment/iceberg --timeout=10m
