---
title: Integrations
icon: material/connection
---

# Integrations

Iceberg is API-first and designed to sit inside an existing security stack.
Every outbound HTTP call honours the **global outbound proxy** configured at
`/admin/proxy` (system / direct / explicit with no-proxy exclusions).
Publication-driven delivery — email notifications, publication webhooks — and
RSS polling ride a durable **outbox job queue** drained by the
`iceberg-worker` process, enqueued in the same transaction as their cause,
with lease, retry and backoff; interactive calls (the MISP push, AI assist,
writer-triggered TAXII/MISP pulls) run synchronously and report their result
directly.

## MISP (outbound push)

Iceberg stages **light-touch IOCs** as notebook entities; the authoritative
indicator store stays external. A writer pushes a report's **cited
indicators to MISP as one event** — lifecycle-gated (approved or published
reports only; published reports push from their frozen snapshot), idempotent
(re-push updates the same event), failure-isolated, and authenticated with an
env-only API key. Each indicator's TLP rides along as a per-attribute tag;
indicators above `ICEBERG_MISP_MAX_TLP` prompt the writer to confirm before
leaving the org. Configured at `/admin/misp`.

## TAXII 2.1 (serving and pull)

- **Serving** — published reports are available read-only in TAXII
  2.1-shaped collections under `/api/taxii2/`, as STIX 2.1 `report` objects
  with their cited indicators.
- **Pull** — writers trigger a TAXII 2.1 (or MISP) pull into a notebook,
  landing objects as sources and staged IOCs behind the SSRF guard.

## RSS / Atom ingestion

Admins configure external feeds at `/admin/feeds`; articles poll into a
writer-only **feed reader** where an analyst sends an article to a notebook
as an auto-graded source. Fetching is opt-in, timeout- and byte-bounded,
failure-isolated, and content is sanitised; feed URLs are admin-only — the
SSRF containment boundary.

## ATT&CK

An offline **MITRE Enterprise ATT&CK** bundle import
(`iceberg-import-attack`) creates technique tags. Techniques tagged across
reports drive a coverage heatmap and downloadable **ATT&CK Navigator
layers** — per report and per actor/malware/campaign entity.

## Governed AI assist

Optional AI-assist endpoints (summarise a source, suggest IOC candidates,
draft tradecraft phrasing) run behind an admin-configured provider
(`/admin/ai`) with an env-only key. Source content above
`ICEBERG_AI_MAX_TLP` never leaves for the provider; AI-suggested indicators
are normalised (refanged, type-constrained) and always land as *staged*
entities for analyst review, never directly in a product.

## Email, webhooks and SIEM

- **Email** — stakeholders get a notification when a product lands in their
  feed (SMTP, optional).
- **Publication webhook** — a signed webhook fires on publish for
  tag-subscription integrations, configured at `/admin/webhook` with an
  env-only token.
- **SIEM** — the structured audit log forwards via stdout/file, syslog, or
  HTTP event collector (see [Security](security.md#audit-and-siem)).

## API

The whole portal is backed by a JSON API under `/api/*`, authenticated with
Bearer JWTs — everything an analyst can do in the UI, a pipeline can do
against the API. Interactive docs (`/docs`, `/redoc`) are enabled outside
production.
