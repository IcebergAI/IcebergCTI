#!/usr/bin/env bash
# Rehearse a release against PostgreSQL before it is tagged (#314).
#
# Four things are proven, in order; any failure stops the run:
#
#   1. clean install   — migrate an empty database to head, boot the image, probes ready
#   2. seeded upgrade  — stage a database at the previous schema, seed it, migrate forward
#   3. backup/restore  — dump, restore into a fresh database, verify file references
#   4. rollback bound  — report which migrations do not restore prior state on downgrade
#
# The output is the rehearsal record: version, schema revisions at each step and the
# outcome of every check, so it can be attached to the release.
#
# Usage:  scripts/release_rehearsal.sh [IMAGE]
#
# IMAGE defaults to iceberg:rehearsal and is built from the working tree if absent.
# Requires docker plus a reachable PostgreSQL:
#   REHEARSAL_PG_URL (default postgresql://postgres:postgres@127.0.0.1:5432/postgres)
set -euo pipefail

IMAGE="${1:-iceberg:rehearsal}"
PG_ADMIN_URL="${REHEARSAL_PG_URL:-postgresql://postgres:postgres@127.0.0.1:5432/postgres}"
PG_BASE="${PG_ADMIN_URL%/*}"
SECRET="${ICEBERG_SECRET_KEY:-rehearsal-secret-0123456789abcdef0123456789}"
CONTAINER="iceberg-rehearsal-$$"
WORKDIR="$(mktemp -d)"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fact() { printf '  %-24s %s\n' "$1" "$2"; }

pg() { docker run --rm --network host postgres:18 psql "$1" -qtAX -c "$2"; }
app_db_url() { echo "postgresql+psycopg://${PG_BASE#*://}/$1"; }

objects_dir() { # objects_dir <db> — the object root paired with one database
  echo "$WORKDIR/objects/$1"
}

run_in_image() { # run_in_image <db> <command...>
  local db="$1"; shift
  mkdir -p "$(objects_dir "$db")"
  docker run --rm --network host \
    -e ICEBERG_ENVIRONMENT=prod \
    -e ICEBERG_SECRET_KEY="$SECRET" \
    -e ICEBERG_DATABASE_URL="$(app_db_url "$db")" \
    -e ICEBERG_AUTO_MIGRATE=false \
    -e ICEBERG_ATTACHMENTS_DIR=/data/attachments \
    -v "$(objects_dir "$db"):/data/attachments" \
    "$IMAGE" "$@"
}

seed_stage() { # seed_stage <db> <stage>
  mkdir -p "$(objects_dir "$1")"
  ICEBERG_DATABASE_URL="$(app_db_url "$1")" \
  ICEBERG_SECRET_KEY="$SECRET" \
  ICEBERG_ENVIRONMENT=dev \
  ICEBERG_ATTACHMENTS_DIR="$(objects_dir "$1")" \
    python3 scripts/rehearsal_seed.py --stage "$2"
}

schema_revision() { # schema_revision <db>
  pg "$PG_BASE/$1" "SELECT version_num FROM alembic_version" | tr -d '[:space:]'
}

recreate_db() { # recreate_db <db>
  pg "$PG_ADMIN_URL" "DROP DATABASE IF EXISTS $1" >/dev/null
  pg "$PG_ADMIN_URL" "CREATE DATABASE $1" >/dev/null
}

# --------------------------------------------------------------------------- #
log "Release under rehearsal"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -t "$IMAGE" .
fi
VERSION="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)"
fact "version (PEP 440)" "$VERSION"
fact "image" "$IMAGE"
fact "image id" "$(docker image inspect --format '{{.Id}}' "$IMAGE")"

# --------------------------------------------------------------------------- #
log "1/4 Clean install"
recreate_db rehearsal_clean
run_in_image rehearsal_clean iceberg-migrate
fact "schema revision" "$(schema_revision rehearsal_clean)"

docker run -d --name "$CONTAINER" --network host \
  -e ICEBERG_ENVIRONMENT=prod \
  -e ICEBERG_SECRET_KEY="$SECRET" \
  -e ICEBERG_DATABASE_URL="$(app_db_url rehearsal_clean)" \
  -e ICEBERG_AUTO_MIGRATE=false \
  "$IMAGE" >/dev/null
ready=""
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/readyz >/dev/null 2>&1; then ready=yes; break; fi
  sleep 2
done
if [ -z "$ready" ]; then
  echo "readiness probe never came up" >&2
  docker logs "$CONTAINER" >&2 || true
  exit 1
fi
curl -fsS http://127.0.0.1:8000/healthz >/dev/null
fact "probes" "healthz + readyz ready"
docker rm -f "$CONTAINER" >/dev/null

# --------------------------------------------------------------------------- #
log "2/4 Upgrade from a seeded previous schema"
# The rehearsed upgrade is the one operators run: a database already carrying data
# at the previous release's schema, migrated forward by this release's job.
recreate_db rehearsal_upgrade
seed_stage rehearsal_upgrade previous
BEFORE="$(schema_revision rehearsal_upgrade)"
run_in_image rehearsal_upgrade iceberg-migrate
AFTER="$(schema_revision rehearsal_upgrade)"
seed_stage rehearsal_upgrade verify
fact "schema before" "$BEFORE"
fact "schema after" "$AFTER"
fact "seeded data" "survived the upgrade"

# --------------------------------------------------------------------------- #
log "3/4 Backup and verified restore"
# The database and the object store are one consistency set: restoring rows
# whose blobs are gone is a restore that looks complete and is not, so both are
# backed up and both are restored before anything is verified.
docker run --rm --network host postgres:18 \
  pg_dump "$PG_BASE/rehearsal_upgrade" -Fc > "$WORKDIR/rehearsal.dump"
tar -C "$(objects_dir rehearsal_upgrade)" -cf "$WORKDIR/rehearsal-objects.tar" .
recreate_db rehearsal_restore
docker run --rm --network host -i postgres:18 \
  pg_restore -d "$PG_BASE/rehearsal_restore" --no-owner < "$WORKDIR/rehearsal.dump"
rm -rf "$(objects_dir rehearsal_restore)"
mkdir -p "$(objects_dir rehearsal_restore)"
tar -C "$(objects_dir rehearsal_restore)" -xf "$WORKDIR/rehearsal-objects.tar"
run_in_image rehearsal_restore iceberg-verify-files
seed_stage rehearsal_restore verify
fact "dump size" "$(wc -c < "$WORKDIR/rehearsal.dump") bytes"
fact "object archive" "$(wc -c < "$WORKDIR/rehearsal-objects.tar") bytes"
fact "restored schema" "$(schema_revision rehearsal_restore)"
fact "file references" "verified against the restored objects"

# --------------------------------------------------------------------------- #
log "4/4 Rollback boundary"
python3 scripts/rehearsal_seed.py --stage rollback-report

log "Rehearsal record"
fact "version" "$VERSION"
fact "clean install" "pass"
fact "seeded upgrade" "pass ($BEFORE -> $AFTER)"
fact "backup/restore" "pass"
