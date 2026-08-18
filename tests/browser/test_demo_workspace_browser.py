"""Real-browser acceptance for the guided demo's three ordinary roles."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


def _chromium_installed() -> bool:
    try:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:  # pragma: no cover - no driver or no browser build
        return False


# The guided-demo acceptance needs a real Chromium (CI installs it with
# `playwright install --with-deps chromium`). A developer running the documented
# `uv run pytest` without that one-off install should get a skip, not a failure.
pytestmark = pytest.mark.skipif(
    not _chromium_installed(),
    reason="Chromium is not installed; run `uv run playwright install chromium`",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live_demo_server(tmp_path_factory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("demo-browser")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "ICEBERG_SECRET_KEY": "browser-test-secret-0123456789abcdef",
            "ICEBERG_DATABASE_URL": f"sqlite:///{root / 'demo.db'}",
            "ICEBERG_DEV_AUTH": "true",
            "ICEBERG_ENVIRONMENT": "dev",
            "ICEBERG_ATTACHMENTS_DIR": str(root / "attachments"),
            "ICEBERG_FIGURES_DIR": str(root / "figures"),
            "ICEBERG_RENDER_OUTPUT_DIR": str(root / "renders"),
            "PYTHONPATH": str(Path.cwd() / "src"),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "iceberg.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"demo browser server exited early: {stderr}")
            try:
                with urllib.request.urlopen(f"{base}/readyz", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("demo browser server did not become ready")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _login(context: BrowserContext, base: str, role: str, email: str) -> Page:
    response = context.request.post(
        f"{base}/auth/dev-login",
        form={"role": role, "email": email, "name": f"Demo {role.title()}"},
        headers={"Origin": base},
    )
    assert response.ok, response.text()
    return context.new_page()


def _assert_accessible(page: Page) -> None:
    # The roadmap item owns the demo surface; the legacy application shell has
    # separate contrast debt and is outside this narrowly scoped acceptance.
    results = Axe().run(page, context=".demo-shell").response["violations"]
    blockers = [
        violation
        for violation in results
        if violation.get("impact") in {"serious", "critical"}
    ]
    assert not blockers, [
        (
            violation["id"],
            violation.get("impact"),
            [node["target"] for node in violation["nodes"]],
        )
        for violation in blockers
    ]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for height in page.locator(".demo-action").evaluate_all(
        "elements => elements.map(element => element.getBoundingClientRect().height)"
    ):
        assert height >= 44


def _keyboard_activate(page: Page, accessible_name: str) -> None:
    for _ in range(30):
        page.keyboard.press("Tab")
        if (
            page.evaluate("document.activeElement && document.activeElement.innerText")
            == accessible_name
        ):
            page.keyboard.press("Enter")
            return
    raise AssertionError(f"keyboard focus never reached {accessible_name!r}")


def test_each_role_keyboard_mobile_and_axe(live_demo_server: str):
    base = live_demo_server
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch()
        analyst_context = browser.new_context(viewport={"width": 1440, "height": 900})
        analyst = _login(
            analyst_context, base, "ANALYST", "browser-analyst@example.com"
        )
        analyst.on(
            "console",
            lambda message: (
                errors.append(message.text) if message.type == "error" else None
            ),
        )
        analyst.goto(f"{base}/demo")
        _keyboard_activate(analyst, "Start guided demo")
        analyst.locator("code").wait_for()
        join_code = analyst.locator("code").inner_text()
        reset_action = analyst.locator('form[action$="/reset"]').get_attribute("action")
        assert reset_action is not None
        public_id = reset_action.split("/")[2]
        _assert_accessible(analyst)

        stakeholder_context = browser.new_context(
            viewport={"width": 1440, "height": 900}
        )
        stakeholder = _login(
            stakeholder_context,
            base,
            "STAKEHOLDER",
            "browser-stakeholder@example.com",
        )
        stakeholder.on(
            "console",
            lambda message: (
                errors.append(message.text) if message.type == "error" else None
            ),
        )
        stakeholder.goto(f"{base}/demo")
        stakeholder.get_by_label("Workspace ID").fill(public_id)
        stakeholder.get_by_label("One-time join code").fill(join_code)
        stakeholder.get_by_role("button", name="Join guided demo").press("Enter")
        stakeholder.wait_for_url(f"**/demo/{public_id}?joined=1")
        assert stakeholder.get_by_role("heading", name="Your next step").is_visible()
        assert stakeholder.get_by_role("link", name="Open requirement").is_visible()
        _assert_accessible(stakeholder)

        reviewer_context = browser.new_context(viewport={"width": 1440, "height": 900})
        reviewer = _login(
            reviewer_context, base, "REVIEWER", "browser-reviewer@example.com"
        )
        reviewer.on(
            "console",
            lambda message: (
                errors.append(message.text) if message.type == "error" else None
            ),
        )
        reviewer.goto(f"{base}/demo/{public_id}")
        assert reviewer.get_by_role("link", name="Review report").is_visible()
        _assert_accessible(reviewer)

        analyst.set_viewport_size({"width": 390, "height": 844})
        analyst.goto(f"{base}/demo/{public_id}")
        assert analyst.get_by_role("link", name="Open report editor").is_visible()
        _assert_accessible(analyst)

        browser.close()
    assert errors == []
