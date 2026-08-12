# Guided demo workspace

The authenticated `/demo` route provides one resettable, fictional intelligence cycle. An
analyst starts the workspace, an ordinary stakeholder claims it with a one-time code, and an
ordinary reviewer opens the non-enumerable workspace link. The checklist deep-links into the
same requirement, notebook, report lifecycle, publication, feed, and feedback screens used for
normal work.

The demo uses only `urn:iceberg:demo:*` source references and visibly marks every linked screen
as synthetic. It does not call AI, ingest feeds, send email or webhooks, or add its rows to
operational dashboards. Reset deletes only roots carrying that workspace's durable marker and
removes their attachments, figures, and rendered products after the database transaction
commits.

## Reproducible browser acceptance

The browser test starts an isolated SQLite-backed application and drives separate analyst,
stakeholder, and reviewer browser contexts through the real authentication and demo routes. It
checks keyboard activation, desktop and 390-pixel mobile layouts, 44-pixel action targets,
horizontal overflow, browser console errors, and serious/critical Axe findings for every role.

```bash
uv sync --locked --extra dev
uv run playwright install --with-deps chromium
uv run pytest tests/browser/test_demo_workspace_browser.py -n0
```

Automated accessibility checks complement rather than replace keyboard and screen-reader
inspection. Before closing the roadmap issue, run the separately required moderated first-use
exercise with people unfamiliar with the repository and record its task-completion evidence.
