---
title: Deployment
icon: material/rocket-launch-outline
---

# Deployment

Iceberg is a single FastAPI application serving both the JSON API (`/api/*`)
and the server-rendered portal, with PostgreSQL as the production datastore.
Three supported shapes:

## Local development

Zero dependencies beyond Python ≥ 3.14 — SQLite and on-disk working
directories:

```bash
uv sync --extra dev
cp .env.example .env            # adjust as needed
uv run uvicorn iceberg.main:app --reload
```

Open <http://localhost:8000> and use the **dev login**
(`ICEBERG_DEV_AUTH=true`, the default outside prod) — no IdP required.
Install the [`typst`](https://github.com/typst/typst) binary for PDF
rendering.

!!! note "SQLite is dev/test only"
    The app refuses to boot on a SQLite URL when `ICEBERG_ENVIRONMENT=prod`,
    and the Docker image ships no SQLite fallback — a container can't
    silently run without PostgreSQL.

## Docker Compose (single host)

```bash
docker compose up                                  # https://localhost via Caddy + PostgreSQL
ICEBERG_DOMAIN=intel.example.com docker compose up # automatic Let's Encrypt TLS
```

The default Compose stack runs four services on an internal network:

- **caddy** — the TLS front end and only public entry point (`:80`/`:443`,
  HTTP/3). Automatic HTTPS: a local-CA certificate for the default
  `localhost`, Let's Encrypt for a real domain. Runs non-root with all
  capabilities dropped except `NET_BIND_SERVICE`, on a read-only rootfs.
- **iceberg** — the app, publishing plain HTTP on loopback only
  (`127.0.0.1:8000`) as a local/debug side door. Started with
  `--proxy-headers`; `X-Forwarded-*` is trusted only from the Compose
  network, so audit logs record the real client IP.
- **postgres** — the datastore, on a named volume.
- **redis** — shared rate-limit buckets across uvicorn workers.

For production Compose, set `ICEBERG_ENVIRONMENT=prod` (Secure cookies +
HSTS), a real `ICEBERG_SECRET_KEY`, `ICEBERG_AUTO_MIGRATE=false`, and
non-default `POSTGRES_*` credentials.

## Kubernetes

Manifests live under
[`deploy/k8s/`](https://github.com/IcebergAI/IcebergCTI/tree/main/deploy/k8s):
an Ingress terminates TLS to a ClusterIP Service and a single-replica
Deployment (`Recreate`, non-root, read-only rootfs, dropped capabilities). A
migrate Job runs `alembic upgrade head` out of band; config comes from a
ConfigMap and secrets from a Secret. PostgreSQL is a managed instance or the
optional StatefulSet; uploads and rendered PDFs live on a `ReadWriteOnce`
`/data` PVC — the reason replicas stay at 1 until shared storage lands.

Pin the published image by immutable digest in production
(`deploy/k8s/release.sh`).

## Releases

A release is a git tag: pushing `v*` to `main` builds and pushes
`ghcr.io/icebergai/icebergcti` with an SBOM and SLSA provenance, attests the
provenance to the registry, signs the image keylessly with cosign, and cuts
the GitHub Release. See
[`docs/RELEASING.md`](https://github.com/IcebergAI/IcebergCTI/blob/main/docs/RELEASING.md).

## Backup and restore

Persistent state lives in two places — **PostgreSQL** (reports, requirements,
tags, audit events, settings) and the **`/data` volume** (attachments,
figures, rendered PDFs). Back up both while writers are stopped:

```bash
docker compose stop iceberg
docker compose exec postgres pg_dump -U iceberg -d iceberg -Fc > iceberg-$(date +%F).dump
docker run --rm -v iceberg_iceberg-data:/data -v "$PWD":/out busybox \
  tar cf /out/iceberg-data-$(date +%F).tar -C /data .
docker compose start iceberg
```

Restore keeps the app stopped, restores PostgreSQL, clears the working
directories before extracting the archive, applies migrations, and runs
`iceberg-verify-files` before restart. PDFs regenerate; attachments and
figures are original material.

## Configuration

All settings are environment variables with the `ICEBERG_` prefix (see
[`.env.example`](https://github.com/IcebergAI/IcebergCTI/blob/main/.env.example)).
Secrets are **env-only** — provider client secrets, API keys and tokens are
never stored in the database. Admins get a read-only **effective
configuration** view at `/admin/config` showing every resolved value, its
provenance, and secret redaction, plus a settings hub at `/admin` with
per-subsystem status tiles.

Production guards refuse to boot on a SQLite URL, a weak
`ICEBERG_SECRET_KEY`, or a wildcard `FORWARDED_ALLOW_IPS`.
