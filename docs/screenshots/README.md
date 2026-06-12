# Regenerating the README screenshots

The images under [`docs/images/`](../images) and the sample PDF are generated from a
throwaway demo database so they can be refreshed when the UI changes.

Prerequisites (in the project venv):

```bash
pip install -e ".[dev]"
pip install playwright            # dev-only; not a runtime dependency
playwright install chromium
# the typst binary on PATH (for the PDF)
```

Then, from the **repository root**:

```bash
# 1. Build an isolated demo DB + render the sample PDFs
python docs/screenshots/seed_demo.py        # writes ./demo.db, ./demo_rendered, ./demo_attachments

# 2. Serve the portal against that demo DB
ICEBERG_DATABASE_URL="sqlite:///./demo.db" \
ICEBERG_ATTACHMENTS_DIR="./demo_attachments" \
ICEBERG_RENDER_OUTPUT_DIR="./demo_rendered" \
uvicorn iceberg.main:app --host 127.0.0.1 --port 8011 &

# 3. Capture the authenticated full-page screenshots
python docs/screenshots/capture.py docs/images

# 4. Rebuild the 2-up PDF composite
VOLT=$(ls demo_rendered/report-1-full-*.pdf | head -1)
cp "$VOLT" docs/sample-report-volt-typhoon.pdf
pdftoppm -png -r 150 -f 1 -l 2 "$VOLT" /tmp/pdf-page
# (the side-by-side docs/images/pdf-sample.png is assembled with Pillow — see git history)

# 5. Tidy up
rm -rf demo.db demo_attachments demo_rendered
```

`capture.py` authenticates by minting an Iceberg JWT per identity (analyst / admin /
stakeholder) and setting it as the `iceberg_session` cookie on the Playwright browser
context — no live IdP needed. The user ids it uses (1 / 4 / 5) match the seed order in
`seed_demo.py`; keep them in sync.
