# Iceberg Kubernetes Deployment

These manifests run Iceberg on **PostgreSQL** — the only supported deployment
datastore. (SQLite is the zero-dependency *local* dev/test default; the prod app
refuses to boot on it, and the image carries no SQLite fallback.) The Deployment
stays single replica (`ReadWriteOnce` PVC, `Recreate` rollout) until uploads/
renders move to shared storage — see *Scaling caveat*.

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
  # ICEBERG_RATE_LIMIT_REDIS_URL
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
4. **Deploy.** `kubectl apply -f configmap.yaml -f service.yaml -f pvc.yaml -f deployment.yaml`.

## Apply order

```bash
# Apply EITHER configmap.yaml (prod + Entra OIDC) OR configmap.beta.yaml (OIDC-free
# evaluation) — see "Authentication / Login". They share the ConfigMap name.
kubectl apply -f configmap.yaml -f service.yaml -f pvc.yaml
kubectl apply -f secret.yaml          # from secret.example.yaml (sets ICEBERG_DATABASE_URL)
IMAGE=ghcr.io/icebergai/icebergcti@sha256:<digest> RELEASE=<unique-id> ./release.sh
kubectl apply -f ingress.yaml         # optional — TLS exposure (edit host + secret first)
kubectl apply -f prune-cronjob.yaml   # optional — retention CronJobs (see "Retention")
```

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

Two stores hold all persistent state: **PostgreSQL** (every report, requirement,
tag, audit event, settings row) and the **`iceberg-data` PVC** (uploaded
attachments and figures, plus rendered PDFs under `/data`). Back up **both** —
the PDFs regenerate from a report, but attachments/figures are original material
with no other copy. Capture both while application writers are quiesced.

### PostgreSQL

Prefer a **managed** instance's automated backups / PITR. For the self-hosted
`postgres` StatefulSet (or any reachable instance), use `pg_dump`/`pg_restore`:

```bash
# Stop writers before either backup half; keep them stopped through both.
kubectl scale deploy/iceberg --replicas=0
kubectl rollout status deploy/iceberg --timeout=5m
# Back up — a custom-format dump (compressed, restorable selectively).
kubectl exec postgres-0 -- \
  pg_dump -U iceberg -d iceberg -Fc > iceberg-$(date +%F).dump

# Restore while writers remain stopped.
kubectl exec -i postgres-0 -- \
  pg_restore -U iceberg -d iceberg --clean --if-exists < iceberg-2026-06-27.dump
```

The dump is schema + data, so a restore lands at the schema version it was taken
at; run the migrate Job afterwards (`kubectl apply -f migrate-job.yaml`) if you
are restoring into a newer image. The SQLite FTS index has no Postgres analogue —
the `tsvector` column is generated, so search works immediately after restore.

### `iceberg-data` PVC (uploads + renders)

If your storage class supports `VolumeSnapshot`, that's the simplest route.
Otherwise tar the volume through a short-lived helper pod that mounts it (the app
is single-replica with a `Recreate` rollout, so scale it to 0 first for a
consistent copy):

```bash
kubectl scale deploy/iceberg --replicas=0

# Spin up a helper pod that mounts the PVC read/write.
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: pvc-tool
spec:
  containers:
    - name: tool
      image: busybox
      command: ["sleep", "3600"]
      volumeMounts: [{ name: data, mountPath: /data }]
  volumes:
    - name: data
      persistentVolumeClaim: { claimName: iceberg-data }
EOF
kubectl wait --for=condition=Ready pod/pvc-tool

# Back up: stream a tar of /data out through the helper.
kubectl exec pvc-tool -- tar cf - -C /data . > iceberg-data-$(date +%F).tar

# Restore replaces filesystem state rather than extracting over stale files.
kubectl exec pvc-tool -- sh -c 'rm -rf /data/attachments /data/figures /data/rendered && mkdir -p /data/attachments /data/figures /data/rendered'
kubectl exec -i pvc-tool -- tar xf - -C /data < iceberg-data-2026-06-27.tar

kubectl delete pod pvc-tool
```

After restore, run the unique release migration workflow and execute
`iceberg-verify-files` from the restored image with the data PVC mounted. Restart
only after database restore, filesystem replacement, migration, and verification
all succeed. The verifier reports missing attachment, figure, and retained-render
row IDs without exposing filenames or content.

## Scaling caveat

Postgres removes the database single-writer bottleneck, but **uploads and
rendered PDFs are still written to the local `/data` PVC** (attachments, figures,
renders). Running more than one replica needs shared file storage (an RWX volume
or object storage) — a separate follow-on. Until then keep `replicas: 1` +
`Recreate`.

The pod runs as non-root (uid 10001) with a read-only root filesystem, dropped
capabilities and `RuntimeDefault` seccomp; `/tmp` and the Typst cache (`/cache`)
are `emptyDir` mounts, and `/data` is the PVC.

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
windows so they don't grow without limit. Schedule the prune commands as
`CronJob`s — [`prune-cronjob.yaml`](prune-cronjob.yaml) ships both, using the
same image, ConfigMap and Secret as the app (each is one bounded pass per run):

- **`iceberg-prune-audit`** — deletes `AuditEvent` rows older than
  `ICEBERG_AUDIT_RETENTION_DAYS` (default 365) and un-ingested `FeedItem` rows
  older than `ICEBERG_FEED_ITEM_RETENTION_DAYS` (default 90). The SIEM is the
  long-term audit store; feed items captured into a notebook became durable
  `Source` rows and are never pruned. On a **public** instance the audit table is
  the fastest-growing (every scanned 401/403 lands there) — keep this scheduled.
- **`iceberg-prune-renders`** — applies the rendered-PDF policy
  (`ICEBERG_RENDER_RETENTION_KEEP` / `ICEBERG_RENDER_RETENTION_DAYS`).

Set any window to `0` to keep that store forever.
