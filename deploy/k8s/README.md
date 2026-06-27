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
  # ICEBERG_SMTP_PASSWORD, ICEBERG_PROXY_USERNAME/PASSWORD
```

## PostgreSQL (recommended for production)

1. **Provision Postgres.** Prefer a **managed** instance. For demo/self-hosted,
   `kubectl apply -f postgres.yaml` (single-node StatefulSet + headless Service;
   supply `postgres-secret` — see the file).
2. **Point Iceberg at it.** Put `ICEBERG_DATABASE_URL` (with credentials) in
   `iceberg-secrets`, not the ConfigMap. URL form:
   `postgresql+psycopg://USER:PASS@HOST:5432/DBNAME`.
3. **Migrate.** `ICEBERG_AUTO_MIGRATE` stays `false`; run the migrate Job so the
   schema is owned by the deploy step:
   ```bash
   kubectl apply -f migrate-job.yaml
   kubectl wait --for=condition=complete job/iceberg-migrate
   ```
   The same migrations cover both backends — the SQLite-only FTS5 objects and the
   Postgres-only `search_vector` (tsvector + GIN) block are each dialect-guarded.
4. **Deploy.** `kubectl apply -f configmap.yaml -f service.yaml -f pvc.yaml -f deployment.yaml`.

## Apply order

```bash
kubectl apply -f configmap.yaml -f service.yaml -f pvc.yaml
kubectl apply -f secret.yaml          # from secret.example.yaml (sets ICEBERG_DATABASE_URL)
kubectl apply -f migrate-job.yaml     # alembic upgrade head
kubectl apply -f deployment.yaml
kubectl apply -f ingress.yaml         # optional — TLS exposure (edit host + secret first)
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
(`FORWARDED_ALLOW_IPS`, default `*` — scope it to the ingress-controller pod CIDR
for a stricter posture), so the request scheme is correct and the audit log
records the real client IP rather than the ingress pod's. Set
`ICEBERG_ENVIRONMENT=prod` for `Secure` cookies + HSTS.

## Backup & restore

Two stores hold all persistent state: **PostgreSQL** (every report, requirement,
tag, audit event, settings row) and the **`iceberg-data` PVC** (uploaded
attachments and figures, plus rendered PDFs under `/data`). Back up **both** —
the PDFs regenerate from a report, but attachments/figures are original material
with no other copy. The database is authoritative, so when in doubt dump it
first, the files second.

### PostgreSQL

Prefer a **managed** instance's automated backups / PITR. For the self-hosted
`postgres` StatefulSet (or any reachable instance), use `pg_dump`/`pg_restore`:

```bash
# Back up — a custom-format dump (compressed, restorable selectively).
kubectl exec postgres-0 -- \
  pg_dump -U iceberg -d iceberg -Fc > iceberg-$(date +%F).dump

# Restore into a fresh, empty database (scale the app to 0 first so nothing
# writes mid-restore: kubectl scale deploy/iceberg --replicas=0).
kubectl exec -i postgres-0 -- \
  pg_restore -U iceberg -d iceberg --clean --if-exists < iceberg-2026-06-27.dump
kubectl scale deploy/iceberg --replicas=1
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

# Restore: pipe a tar back in (the PVC must already exist; -i feeds stdin).
kubectl exec -i pvc-tool -- tar xf - -C /data < iceberg-data-2026-06-27.tar

kubectl delete pod pvc-tool
kubectl scale deploy/iceberg --replicas=1
```

Keep DB and file backups from the **same window** so a restored report's cited
attachments still resolve. (This is the same single-writer / local-filesystem
caveat as *Scaling caveat* below — there is one `iceberg-data` volume to snapshot
because there is exactly one replica.)

## Scaling caveat

Postgres removes the database single-writer bottleneck, but **uploads and
rendered PDFs are still written to the local `/data` PVC** (attachments, figures,
renders). Running more than one replica needs shared file storage (an RWX volume
or object storage) — a separate follow-on. Until then keep `replicas: 1` +
`Recreate`.

The pod runs as non-root (uid 10001) with a read-only root filesystem, dropped
capabilities and `RuntimeDefault` seccomp; `/tmp` and the Typst cache (`/cache`)
are `emptyDir` mounts, and `/data` is the PVC.
