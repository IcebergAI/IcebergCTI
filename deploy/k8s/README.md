# Iceberg Kubernetes Deployment

These manifests run Iceberg on **PostgreSQL** — the only supported deployment
datastore. (SQLite is the zero-dependency *local* dev/test default; the prod app
refuses to boot on it, and the image carries no SQLite fallback.) Persistent files
live in a private S3/S3-compatible bucket, allowing a two-replica rolling Deployment
without filesystem affinity.

## Secrets

Copy the template and fill it in (never commit real values):

```bash
cp secret.example.yaml secret.yaml   # edit, then:
kubectl apply -f secret.yaml
```

Or create it imperatively:

```bash
kubectl create secret generic iceberg-secrets \
  --from-literal=ICEBERG_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  --from-literal=ICEBERG_DATABASE_URL="postgresql+psycopg://iceberg:CHANGEME@postgres:5432/iceberg"
  # plus any of: ICEBERG_OIDC_CLIENT_SECRET, ICEBERG_AUDIT_HTTP_TOKEN,
  # ICEBERG_MISP_API_KEY, ICEBERG_WEBHOOK_TOKEN, ICEBERG_AI_API_KEY,
  # ICEBERG_SMTP_PASSWORD, ICEBERG_PROXY_USERNAME/PASSWORD,
  # ICEBERG_RATE_LIMIT_REDIS_URL, ICEBERG_STORAGE_S3_ACCESS_KEY_ID,
  # ICEBERG_STORAGE_S3_SECRET_ACCESS_KEY, ICEBERG_STORAGE_S3_SESSION_TOKEN,
  # ICEBERG_METRICS_TOKEN
```

## Authentication / Login

A freshly-applied **prod** deployment ([configmap.yaml](configmap.yaml)) ships
`ICEBERG_ENVIRONMENT=prod` + `ICEBERG_DEV_AUTH=false` and **no OIDC** — which means
*no usable login path* until you configure Entra (the dev-login bypass is hard-disabled
in prod). Pick one of the two paths below. (The app also logs a warning on boot when it
detects this locked-out state.)

### Beta / evaluation login (no OIDC)

For a quick evaluation with **no Entra setup**, apply the eval overlay
[configmap.beta.yaml](configmap.beta.yaml) *instead of* `configmap.yaml`:

```bash
kubectl apply -f configmap.beta.yaml   # NOT configmap.yaml
```

Then browse to `/auth/login`, pick a role (ADMIN/ANALYST/REVIEWER/STAKEHOLDER) and enter —
no credentials required.

> ⚠️ **Evaluation only.** The overlay runs the non-prod environment to permit the
> dev-login bypass, which means **anyone reaching the portal can self-select any role
> (including ADMIN)**, session cookies are no longer `Secure`, HSTS is not sent, and the
> prod boot-guards (SQLite/weak-key rejection) are off. Use it only behind access
> controls you trust, never for real data or public exposure.

### Production login — Microsoft Entra OIDC

1. **Register an app** in *Entra ID → App registrations → New registration*.
2. **Redirect URI** (Web platform): `https://<your-host>/auth/callback`. This must equal
   `ICEBERG_OIDC_REDIRECT_URI`, which is `ICEBERG_PORTAL_BASE_URL` + `/auth/callback`.
3. **App roles.** Define app roles whose values are `ADMIN`, `ANALYST`, `REVIEWER`,
   `STAKEHOLDER` and assign users (Entra → *App roles* + *Enterprise application →
   Users and groups*). Iceberg reads the `roles` claim by default
   (`ICEBERG_OIDC_ROLE_CLAIM`); a missing/unrecognised role maps to read-only
   `STAKEHOLDER`, and a missing `email` claim is rejected.
4. **Wire the config.** In [configmap.yaml](configmap.yaml) set (uncomment) the OIDC
   block — `ICEBERG_OIDC_ENABLED=true`, `ICEBERG_OIDC_TENANT_ID`, `ICEBERG_OIDC_CLIENT_ID`,
   `ICEBERG_OIDC_REDIRECT_URI`. Put the **client secret** in the `iceberg-secrets` Secret
   as `ICEBERG_OIDC_CLIENT_SECRET` (see [secret.example.yaml](secret.example.yaml)) — it
   is never in the ConfigMap.

The login page then shows "Continue with Microsoft Entra ID" and the dev bypass stays off.

**Other providers (Authentik / Auth0 / Okta).** Iceberg supports multiple OIDC providers at
once. The Entra env values above **seed** the Entra provider on first boot; to add another IdP,
sign in as an admin, open **`/admin/oidc`**, enable the provider and fill in its client id +
locator + role map, and put its client secret in `iceberg-secrets` as
`ICEBERG_OIDC_<PROVIDER>_CLIENT_SECRET`. Each provider's redirect/callback URL is
`<base>/auth/oidc/<provider>/callback` (the legacy `/auth/callback` remains valid for Entra).

## PostgreSQL (recommended for production)

1. **Provision Postgres.** Prefer a **managed** instance. For demo/self-hosted,
   `kubectl apply -f postgres.yaml` (single-node StatefulSet + headless Service;
   supply `postgres-secret` — see the file).
2. **Point Iceberg at it.** Put `ICEBERG_DATABASE_URL` (with credentials) in
   `iceberg-secrets`, not the ConfigMap. URL form:
   `postgresql+psycopg://USER:PASS@HOST:5432/DBNAME`.
3. **Migrate and release.** `ICEBERG_AUTO_MIGRATE` stays `false`. Use a unique
   release id and the same immutable digest for migration and application:
   ```bash
   IMAGE=ghcr.io/icebergai/icebergcti@sha256:<digest> RELEASE=2026-07-11-1 ./release.sh
   ```
   The script refuses mutable images and reused release names, waits with a
   timeout, prints failed migration logs, and rolls out only after success.
   The same migrations cover both backends — the SQLite-only FTS5 objects and the
   Postgres-only `search_vector` (tsvector + GIN) block are each dialect-guarded.
4. **Storage.** Provision a private, preferably versioned bucket and a deployment-owned prefix.
   On AWS, use workload identity. A custom endpoint may use the env-only credentials from
   `iceberg-secrets`. Grant only bucket listing on the exact prefix and Get/Put/DeleteObject
   beneath it; enforce TLS and your organisation's encryption policy.
5. **Deploy.** Apply the service and release as shown below. `pvc.yaml` is used only when
   migrating legacy local data, not by a new S3-backed installation.

## Apply order

```bash
# Apply EITHER configmap.yaml (prod + Entra OIDC) OR configmap.beta.yaml (OIDC-free
# evaluation) — see "Authentication / Login". They share the ConfigMap name.
kubectl apply -f configmap.yaml -f service.yaml -f pdb.yaml
kubectl apply -f secret.yaml          # from secret.example.yaml (sets ICEBERG_DATABASE_URL)
IMAGE=ghcr.io/icebergai/icebergcti@sha256:<digest> RELEASE=<unique-id> ./release.sh
kubectl apply -f ingress.yaml         # optional — TLS exposure (edit host + secret first)
```

`release.sh` applies the migration, active storage check, Deployment, storage workers,
reconciliation and retention jobs, pinning every workload to the same `$IMAGE` digest. Do not
apply workload manifests containing the readable `:latest` placeholder directly.

## TLS / Ingress

Iceberg always runs behind a **TLS-terminating proxy**. In Kubernetes that's an
Ingress (or a cloud load balancer). [`ingress.yaml`](ingress.yaml) is a commented
ingress-nginx example routing to the `iceberg` Service on port 80 (→ container
8000) — edit the host and TLS secret name, then `kubectl apply -f ingress.yaml`.

TLS, two options:

- **cert-manager (recommended).** Install cert-manager + a `ClusterIssuer`, then
  uncomment the `cert-manager.io/cluster-issuer` annotation in `ingress.yaml`. It
  provisions and renews the cert into the `tls.secretName` Secret automatically —
  you don't pre-create it.
- **Bring your own cert.**
  `kubectl create secret tls iceberg-tls --cert=fullchain.pem --key=privkey.pem`
  and reference it from `tls.secretName`.

The container starts uvicorn with `--proxy-headers` and trusts `X-Forwarded-*`
only from `FORWARDED_ALLOW_IPS`. The ConfigMap contains an example ingress pod
CIDR; replace it with your cluster's narrow ingress-controller address/CIDR, so
the request scheme is correct and the audit log
records the real client IP rather than the ingress pod's. Set
`ICEBERG_ENVIRONMENT=prod` for `Secure` cookies + HSTS.

## Backup & restore

Two stores form one consistency set: **PostgreSQL** (domain rows, immutable object keys,
digests, tombstones and maintenance cursors) and the configured **private object prefix**
(attachments, figures and retained PDFs). A database dump without its matching object snapshot
is not a complete backup.

### Consistent backup

1. Quiesce the app, outbox and storage workers, RSS scheduling, render pruning, migration and
   reconciliation. Wait for active job/deletion leases to finish or expire.
2. Record the release digest, schema revision, bucket, prefix and UTC start time in a backup
   manifest. Do not record credentials or signed URLs.
3. Take a PostgreSQL custom-format dump, then copy/snapshot the exact object prefix while writers
   remain stopped. Prefer provider-native versioning/retention or copy into an immutable backup
   prefix with inventory and SHA-256 metadata preserved.
4. Record object count/bytes and both artifact identifiers in the manifest, then restart the
   workers and application only after both backup halves succeed.

For the supplied workloads, scale both writer Deployments to zero before dumping:

```bash
kubectl scale deploy/iceberg --replicas=0
kubectl scale deploy/iceberg-storage-worker --replicas=0
kubectl exec postgres-0 -- \
  pg_dump -U iceberg -d iceberg -Fc > iceberg-$(date +%F).dump
```

Provider-native object copy/snapshot commands are deliberately not embedded here: their
encryption, account and retention semantics are deployment-specific. Limit the operation to
`ICEBERG_STORAGE_S3_PREFIX`; never make the source or backup public.

### Verified restore

Restore into a **fresh database and fresh object prefix**, not over a partially running
deployment:

1. Keep all writers stopped. Restore the PostgreSQL dump and the matching object snapshot.
2. Set the ConfigMap/Secret to the restored destinations and run the release migration Job.
3. From the restored release, run `iceberg-verify-files` to stream and hash every referenced
   object, then `iceberg-storage-check` to prove PUT → HEAD → GET/SHA-256 → DELETE with current
   credentials. Run `iceberg-reconcile-storage --dry-run` and require zero missing/invalid
   references and no unexpected objects outside the configured grace window.
4. Start storage workers, then app replicas. Prove upload on one replica, download on another,
   and read availability after terminating either replica before admitting traffic.

Any missing object, digest/size mismatch, permission error or active-check failure is a failed
restore. The verifier reports row IDs and safe object identifiers without exposing filenames,
endpoints, credentials or object bytes.

**Major Postgres upgrades** (e.g. 17 → 18) ride the same dump/restore path — a
data directory initialised by one major version cannot be opened by the next.
Dump on the old image, restore into a fresh PVC on the new one. `postgres:18`
also relocated its data mount from `/var/lib/postgresql/data` to
`/var/lib/postgresql` (already reflected in `postgres.yaml`); a PVC carrying
17-era data is not reusable directly.

### Legacy local-storage migration and rollback

`pvc.yaml` and `storage-migrate-job.yaml` exist only for a controlled migration from the former
local backend. Quiesce all writers, retain the legacy PVC read-only, run
`iceberg-migrate-storage --destination s3 --dry-run`, then the bounded resumable copy job. Each
row is hashed, conditionally written and verified before its database reference changes; source
files are never deleted by migration. The Job uses `--until-complete`, so it succeeds only after
successive 500-row batches reach zero unfinished rows (or fails on a conflict/missing source).
Run deep verification and the active check before switching the Deployment to S3. Retain the PVC
through a soak period and at least one verified backup.

Rollback to an old local-only release requires a completed reverse migration and verification.
The Alembic downgrade guard rejects an unsafe downgrade while rows still reference S3 keys.

## Scaling and health

The app runs two pods with `RollingUpdate` (`maxUnavailable: 0`, `maxSurge: 1`), a PDB and topology
spread. Each pod runs one Uvicorn process so Prometheus observations are not split between hidden
process-local registries. Storage-deletion workers use database leases and idempotent tombstones;
the reconciliation CronJob uses durable cursors so bounded runs eventually cover the full prefix.

The pod runs as non-root (uid 10001) with a read-only root filesystem, dropped
capabilities and `RuntimeDefault` seccomp; `/tmp` and the Typst cache (`/cache`)
are `emptyDir` mounts.

`/healthz` is process-only liveness. `/readyz` checks database/schema state plus a bounded,
read-only store reachability probe. It returns a generic 503 without provider details. The
release-only `iceberg-storage-check` command performs the write/read/delete canary; readiness
does not write on every probe.

## Storage observability

`/metrics` is disabled by default and requires `ICEBERG_METRICS_TOKEN` in production. Exported
storage metrics use fixed `backend`, `kind`, `operation`, `outcome`, `direction`, `reason` and
`task` labels only—never bucket names, keys, endpoints, filenames, URLs or exception strings.
They cover operation latency/counts, bytes, integrity failures, deletion queue/failed age and
database-persisted migration/reconciliation last-run and last-success state. Alert on integrity
failures, terminal deletion failures, increasing queue age, readiness failures and stale
maintenance success.

## Rate limiting

Rate limiting is enabled automatically when `ICEBERG_ENVIRONMENT=prod`. Point
`ICEBERG_RATE_LIMIT_REDIS_URL` at a managed Redis instance so auth, AI, render,
outbound-test/push, and search buckets are shared across the container's uvicorn
workers. If the URL carries credentials, keep it in `iceberg-secrets`.

## Durable jobs (email / webhook / RSS)

Outbound work — dissemination emails, publication webhooks and RSS polls — is
written to a durable database outbox in the same transaction as the state that
caused it, and normally delivered by an in-process pass right after commit. If
the pod restarts before that pass runs, the rows wait in the queue. For
guaranteed delivery, schedule **`iceberg-worker`** (one bounded pass per run —
made for a `CronJob` using the same image, ConfigMap and Secret as the app) to
sweep anything left behind, and use `iceberg-worker --inspect` to review job
state. Jobs lease with expiry and retry with backoff, so several workers (or the
app plus a CronJob) can safely share the queue.

## Retention (bounding table + disk growth)

Three derived stores grow over the life of an instance and have retention
windows so they don't grow without limit. The prune commands run as `CronJob`s —
[`prune-cronjob.yaml`](prune-cronjob.yaml) ships both, using the same ConfigMap
and Secret as the app (each is one bounded pass per run). `release.sh` applies
them **pinned to the release's image digest** (the manifest carries a `:latest`
placeholder for readability only — don't apply it directly):

- **`iceberg-prune-audit`** — deletes `AuditEvent` rows older than
  `ICEBERG_AUDIT_RETENTION_DAYS` (default 365) and un-ingested `FeedItem` rows
  older than `ICEBERG_FEED_ITEM_RETENTION_DAYS` (default 90). The SIEM is the
  long-term audit store; feed items captured into a notebook became durable
  `Source` rows and are never pruned. On a **public** instance the audit table is
  the fastest-growing (every scanned 401/403 lands there) — keep this scheduled.
- **`iceberg-prune-renders`** — applies the rendered-PDF policy
  (`ICEBERG_RENDER_RETENTION_KEEP` / `ICEBERG_RENDER_RETENTION_DAYS`).

Set any window to `0` to keep that store forever.

Both deletes are **batched with a commit per batch**, so a first run on an
instance that predates retention makes durable progress instead of attempting one
enormous transaction that rolls back on failure.

**Scheduling caveats (#279).** Both CronJobs use `concurrencyPolicy: Forbid`, so
a run that never finishes would suppress every later run and stop retention
silently. Each therefore carries an `activeDeadlineSeconds` cap, a `backoffLimit`,
and `resources.requests` (a namespace with a `ResourceQuota` rejects pods that
omit requests — another silent-stop path). `iceberg-prune-audit` is DB-only and
deliberately mounts **no** `iceberg-data` volume; `iceberg-prune-renders` does
need it, and because `iceberg-data` is `ReadWriteOnce` and already attached to
the app pod, that job carries a **pod affinity onto `app: iceberg`** so it lands
on the same node instead of hanging in `ContainerCreating`. On a ReadWriteMany
storage class, or a single-node cluster, that affinity block can be removed.
