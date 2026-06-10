"""Configuration safety: production must refuse an unsafe JWT signing key."""

import pytest
from pydantic import ValidationError

from iceberg.config import _INSECURE_DEFAULT_SECRET, Settings


def test_prod_rejects_public_default_secret():
    with pytest.raises(ValidationError):
        Settings(environment="prod", secret_key=_INSECURE_DEFAULT_SECRET)


def test_prod_rejects_short_secret():
    with pytest.raises(ValidationError):
        Settings(environment="prod", secret_key="too-short")


def test_prod_accepts_strong_secret():
    settings = Settings(environment="prod", secret_key="x" * 40)
    assert settings.is_prod
    assert not settings.dev_login_enabled  # dev login is always off in prod


def test_dev_tolerates_default_secret():
    settings = Settings(environment="dev", secret_key=_INSECURE_DEFAULT_SECRET)
    assert not settings.is_prod
