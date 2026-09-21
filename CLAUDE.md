# IcebergCTI contributor guide

IcebergCTI is a FastAPI cyber-threat-intelligence platform with a JSON API and server-rendered portal. It collects notebook material and publishes immutable intelligence products; it is not an authoritative IOC store.

## Essential invariants

- Keep one FastAPI application and preserve the authenticated API/portal boundaries. Production uses PostgreSQL; SQLite is local development/test only and is refused in production.
- Keep secrets environment-only and retain production startup guards. Every outbound HTTP call uses the global proxy and the durable outbox; network fetches must remain SSRF-safe.
- Published products are immutable snapshots. Preserve optimistic-lock conflicts as 409 and do not derive published output from mutable notebook material.
- Dissemination can only narrow its authorization-derived recipient set; TLP gates egress/dissemination rather than in-portal reading.
- Preserve role-wide writer notebook access, centralized authorization, audit behavior, strict CSP, and the documented middleware ordering.
- Keep uploads bounded and magic-byte validated. Do not log secrets or sensitive payloads.
- Releases require the documented signed, attested tag workflow; never publish or deploy without explicit authority.

## Conditional references

Read the detailed source only when relevant:

- [Development reference](docs/DEVELOPMENT_REFERENCE.md) retains the full architecture, domain-model, tooling, test, release, and maintenance guidance formerly in this root file.
- [Architecture](ARCHITECTURE.md) for subsystem contracts and test-coverage details.
- [Release procedure](docs/RELEASING.md) for versioning, rehearsal, signing, publication, or verification.
- [Demo workspace guide](docs/demo-workspace.md) for demo data and reset behavior.

## Validation and publication

Use focused, resource-bounded checks for the affected surface; documentation-only work needs link and diff checks. Add focused regression tests for behavioral changes. Broad suites, release rehearsals, cloud publication, deployment, and any credential operation require explicit task authority. Report what was actually verified and label untested platforms or environments.
