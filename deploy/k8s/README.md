# Iceberg Kubernetes Deployment

These manifests run Iceberg as a single-replica SQLite deployment. Use a
`ReadWriteOnce` PVC and `Recreate` rollout strategy so two pods never write the
same SQLite file concurrently.

Apply secrets separately:

```bash
kubectl create secret generic iceberg-secrets \
  --from-literal=ICEBERG_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  --from-literal=ICEBERG_OIDC_CLIENT_SECRET="" \
  --from-literal=ICEBERG_SMTP_PASSWORD="" \
  --from-literal=ICEBERG_AUDIT_HTTP_TOKEN="" \
  --from-literal=ICEBERG_AI_API_KEY="" \
  --from-literal=ICEBERG_WEBHOOK_TOKEN=""
```

Run migrations as the provided job before starting or upgrading the deployment.
Horizontal scale requires a network database and object storage for uploads.
