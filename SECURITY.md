# Security Policy

Iceberg is a cyber threat intelligence platform, so we take the security of the
project itself seriously. Thank you for helping keep it and its users safe.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.**

Instead, use GitHub's **private vulnerability reporting**:

1. Go to the [**Security** tab](https://github.com/IcebergAI/iceberg/security) of
   this repository.
2. Click **Report a vulnerability** and complete the advisory form.

This opens a private channel visible only to the maintainers. If you cannot use
private reporting, open a regular issue that says only *"I would like to report a
security issue"* (no details) and a maintainer will arrange a private channel.

Please include, as far as you can:

- the affected version / commit and component (API, portal, a specific service),
- a description of the issue and its impact,
- reproduction steps or a proof of concept,
- any suggested remediation.

## What to expect

- **Acknowledgement** within 5 working days.
- An initial **assessment and severity triage** within 10 working days.
- Coordinated disclosure: we will agree a disclosure timeline with you, fix the
  issue in a private branch, release a patched version, and credit you in the
  advisory (unless you prefer to remain anonymous).

## Scope

In scope: the Iceberg application code in this repository (API, portal, services,
authentication/authorization, audit/SIEM, outbound integrations).

Out of scope: vulnerabilities in third-party dependencies (please report those
upstream; we track them via `pip-audit` and Dependabot), and issues that require
a misconfigured deployment contrary to the guidance in the README (e.g. running
with `ICEBERG_DEV_AUTH=true` in production, which the app already refuses).

## Supported versions

Iceberg is pre-1.0 and under active development. Security fixes are applied to the
`main` branch; there is no long-term support branch yet. Deploy from a recent
`main` (or a tagged release once available) to receive fixes.
