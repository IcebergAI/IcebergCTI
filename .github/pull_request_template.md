<!--
Thanks for contributing to Iceberg! Please fill in the sections below.
For security vulnerabilities, do NOT open a PR — see SECURITY.md.
-->

## Summary

<!-- What does this change do, and why? -->

Closes #

## Changes

<!-- Bullet the notable changes. -->

-

## Testing

<!-- How did you verify this? New/updated tests? Manual checks? -->

- [ ] `uv run pytest` passes
- [ ] Static gates pass (`uv run pre-commit run --all-files`)
- [ ] Added/updated tests (a bug fix includes a regression test)

## Checklist

- [ ] Docs updated (`README.md` / `CLAUDE.md`) if behaviour, config, or
      architecture changed
- [ ] Migration added if a model changed (`alembic revision --autogenerate`)
- [ ] Security-sensitive changes (auth, egress, sanitisation, audit) are called
      out above with their reasoning
