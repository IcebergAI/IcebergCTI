"""Wheel packaging regression tests.

The portal runs fine from an editable checkout even when package-data metadata is
wrong, so this builds and imports a real wheel from outside the source tree.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        args,
        capture_output=True,
        text=True,
        **kwargs,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_wheel_install_includes_portal_static_and_typst_assets(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    install_target = tmp_path / "install"
    outside_repo = tmp_path / "outside"
    wheelhouse.mkdir()
    install_target.mkdir()
    outside_repo.mkdir()

    # Setuptools may leave an untracked build/ directory when building from the
    # checkout. Remove only the directory created by this test.
    build_dir = _ROOT / "build"
    had_build_dir = build_dir.exists()
    try:
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                str(_ROOT),
                "--no-deps",
                "--wheel-dir",
                str(wheelhouse),
            ],
            cwd=_ROOT,
        )
    finally:
        if build_dir.exists() and not had_build_dir:
            shutil.rmtree(build_dir)

    wheel = next(wheelhouse.glob("iceberg-*.whl"))
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_target),
            str(wheel),
        ]
    )

    check_script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        from types import SimpleNamespace

        import iceberg
        from iceberg.models import Role
        from iceberg.rendering import typst
        from iceberg.templating import TEMPLATES_DIR, templates

        package_dir = Path(iceberg.__file__).resolve().parent
        install_target = Path(os.environ["ICEBERG_TEST_INSTALL_TARGET"]).resolve()
        assert install_target in package_dir.parents, (package_dir, install_target)

        expected = [
            "templates/base.html",
            "templates/login.html",
            "static/assets.lock.json",
            "static/css/iceberg.css",
            "static/css/vendor/tailwind.css",
            "static/js/vendor/alpine.min.js",
            "typst/product.typ",
        ]
        missing = [rel for rel in expected if not (package_dir / rel).is_file()]
        assert not missing, missing
        assert any((package_dir / "static/fonts").glob("*.woff2"))

        assert Path(TEMPLATES_DIR).resolve() == (package_dir / "templates").resolve()
        assert Path(typst._TEMPLATE).resolve() == (package_dir / "typst" / "product.typ").resolve()

        request = SimpleNamespace(url=SimpleNamespace(path="/auth/login"))
        html = templates.env.get_template("login.html").render(
            request=request,
            oidc_enabled=False,
            dev_login_enabled=True,
            default_name="Tester",
            default_email="analyst@example.com",
            default_role="ANALYST",
            roles=list(Role),
        )
        assert "Sign in" in html
        assert "/static/css/iceberg.css" in html
        assert "/static/" in html
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(install_target)
    env["ICEBERG_TEST_INSTALL_TARGET"] = str(install_target)
    env["ICEBERG_DATABASE_URL"] = "sqlite://"
    env["ICEBERG_DEV_AUTH"] = "true"
    env["ICEBERG_ENVIRONMENT"] = "dev"
    env["ICEBERG_SECRET_KEY"] = "test-secret-0123456789abcdef0123456789"

    _run([sys.executable, "-c", check_script], cwd=outside_repo, env=env)
