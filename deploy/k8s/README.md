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
```

## Scaling caveat

Postgres removes the database single-writer bottleneck, but **uploads and
rendered PDFs are still written to the local `/data` PVC** (attachments, figures,
renders). Running more than one replica needs shared file storage (an RWX volume
or object storage) — a separate follow-on. Until then keep `replicas: 1` +
`Recreate`.

The pod runs as non-root (uid 10001) with a read-only root filesystem, dropped
capabilities and `RuntimeDefault` seccomp; `/tmp` and the Typst cache (`/cache`)
are `emptyDir` mounts, and `/data` is the PVC.
