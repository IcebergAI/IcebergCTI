"""Capture authenticated full-page portal screenshots for the README (Playwright).

Mints an Iceberg JWT per identity, sets it as the portal session cookie on a
browser context, and captures full-page 2x screenshots. Throwaway helper.
"""

import os
import sys

os.environ.setdefault("ICEBERG_DATABASE_URL", "sqlite:///./demo.db")

from playwright.sync_api import sync_playwright  # noqa: E402

from iceberg.auth.tokens import create_access_token  # noqa: E402

BASE = "http://127.0.0.1:8011"

IDENTITIES = {
    "analyst": dict(user_id=1, email="alex.mercer@iceberg.intel",
                    role="ANALYST", name="Alex Mercer"),
    "admin": dict(user_id=4, email="sam.okafor@iceberg.intel",
                  role="ADMIN", name="Sam Okafor"),
    "stakeholder": dict(user_id=5, email="morgan.reyes@northwind.example",
                        role="STAKEHOLDER", name="Morgan Reyes"),
}

# (identity, path, out, viewport_width, clip_height)
# clip_height None -> full_page capture; a number -> capture just that viewport
# height (for pages whose full length is dominated by a long list/sidebar).
SHOTS = [
    ("analyst", "/", "dashboard.png", 1400, None),
    ("analyst", "/reports", "reports-list.png", 1400, None),
    ("analyst", "/reports/1", "report-view.png", 1180, None),
    ("analyst", "/reports/4/edit", "report-editor.png", 1440, None),
    ("analyst", "/search?q=phishing", "search.png", 1400, 1000),
    ("analyst", "/requirements", "tasking-board.png", 1400, None),
    ("admin", "/admin/tags", "taxonomy.png", 1340, 1120),
    ("stakeholder", "/feed", "feed.png", 1180, 600),
    ("analyst", "/feeds", "feed-reader.png", 1340, None),
    ("admin", "/admin/feeds", "admin-feeds.png", 1340, None),
    ("admin", "/admin/proxy", "outbound-proxy.png", 1340, None),
]


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "docs/images"
    only = sys.argv[2] if len(sys.argv) > 2 else None
    os.makedirs(outdir, exist_ok=True)

    tokens = {
        name: create_access_token(user_id=i["user_id"], email=i["email"],
                                  role=i["role"], name=i["name"])
        for name, i in IDENTITIES.items()
    }

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for ident, path, out, width, clip_h in SHOTS:
            if only and only not in out:
                continue
            ctx = browser.new_context(
                viewport={"width": width, "height": clip_h or 900},
                device_scale_factor=2,
            )
            ctx.add_cookies([{
                "name": "iceberg_session", "value": tokens[ident], "url": BASE,
            }])
            page = ctx.new_page()
            page.goto(BASE + path, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(900)  # let webfonts + Alpine settle
            outpath = os.path.join(outdir, out)
            page.screenshot(path=outpath, full_page=clip_h is None)
            print(f"  {outpath}")
            ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
