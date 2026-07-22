---
title: Security
icon: material/shield-lock-outline
---

# Security

Iceberg is built for security-conscious environments: SSO-only authentication,
least-privilege roles, marking-gated egress, and an audit trail designed for a
SIEM.

## Authentication

Authentication is **multi-provider OIDC** — Microsoft Entra, Authentik,
Auth0 and Okta, simultaneously — admin-configured at `/admin/oidc` (env
seeds it). After login Iceberg mints its own short-lived **JWT**, sent as a
Bearer header by API clients or carried in a session cookie by the portal; a
per-user `token_version` supports logout and revocation, and the JWT and
session cookie are signed with **purpose-separated keys** derived from
`ICEBERG_SECRET_KEY`.

Users are keyed on the immutable `(auth_provider, issuer, sub)` triple —
email is non-identifying, so the same person under two IdPs is two accounts,
and a spoofed issuer can't inherit another provider's users. A per-provider
`role_map` maps IdP groups to roles, defaulting to least-privilege
STAKEHOLDER; a missing or unverified email is rejected. Per-provider client
secrets are **env-only**.

A **dev-login bypass** (`ICEBERG_DEV_AUTH=true`) issues a role JWT without an
IdP for local development; it is disabled in prod.

## Roles

| Role | Capability |
|---|---|
| `ADMIN` | Everything, plus admin configuration surfaces |
| `ANALYST` | Collect, author, publish, push to MISP |
| `REVIEWER` | As analyst, plus the review gate |
| `STAKEHOLDER` | Read-only: feed, published reports, own requirements, feedback |

Notebook access is **role-wide** for writers, not owner-scoped — `owner_id`
is provenance only; the one owner/admin gate is deleting a whole notebook.

## TLP as an egress gate

TLP is a display and dissemination-routing marking — never an in-portal read
gate. Where it *does* gate is at the boundary: content above the configured
ceiling cannot leave through **AI assist** (`ICEBERG_AI_MAX_TLP`),
**dissemination** (`ICEBERG_DISSEMINATION_MAX_TLP`), or the **MISP push**
(`ICEBERG_MISP_MAX_TLP`).

## Hardening

- **CSRF** — a same-origin middleware rejects cookie-authenticated
  state-changing requests whose `Origin`/`Referer` mismatch the host;
  Bearer and anonymous requests are exempt; logout is POST-only.
- **Strict CSP** — `script-src 'self'` with no `unsafe-inline`/`unsafe-eval`:
  a vendored CSP build of Alpine, SRI-pinned self-hosted assets, no CDN
  contact at runtime.
- **SSRF guard** — the one server-side fetcher (RSS and writer-triggered
  TAXII/MISP pull) is http(s)-only, rejects private/loopback addresses,
  bounds bytes and timeouts, and re-validates every redirect hop.
- **Upload validation** — MIME whitelist, size caps, and magic-byte content
  sniffing.
- **Rate limiting** and an oversized-body rejection before anything buffers;
  security response headers set by the app itself.
- **Immutable publications** — stakeholder, PDF and MISP output serve from a
  frozen publication snapshot; an optimistic-lock version column turns a
  stale write into a 409.
- **Prod guards** — boot refuses SQLite, a weak secret key, or wildcard
  proxy trust in production.

## Audit and SIEM

Security-relevant events are captured to a **structured-JSON audit log**
(OWASP application-logging shape) with the real client IP, and can be
forwarded to a SIEM via stdout/file, syslog, or an HTTP event collector —
configured at `/admin/audit`. The audit detail JSON never carries secrets or
PII payloads.

## Supply chain

CI runs ruff, bandit, vulture, mypy, pip-audit, CodeQL SAST, a Trivy image
scan, and a zizmor + actionlint audit of the workflows themselves; all
GitHub Actions are SHA-pinned. Release images ship an SBOM and SLSA
provenance and are cosign-signed. See
[`SECURITY.md`](https://github.com/IcebergAI/IcebergCTI/blob/main/SECURITY.md)
for the vulnerability disclosure policy.
