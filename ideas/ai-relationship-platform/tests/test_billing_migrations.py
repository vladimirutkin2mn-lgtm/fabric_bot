"""Migration-chain regression using the CI PostgreSQL database."""

import os
import subprocess

import pytest

pytestmark = pytest.mark.postgres


def test_billing_migration_upgrade_downgrade_upgrade() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    environment = {
        **os.environ,
        "DATABASE_URL": url,
        "TELEGRAM_BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "CONTENT_ENCRYPTION_KEY": "migration-test-key",
        "APP_ENV": "test",
    }
    for arguments in (("upgrade", "head"), ("downgrade", "-1"), ("upgrade", "head")):
        subprocess.run(
            ("alembic", *arguments), check=True, env=environment, capture_output=True, text=True
        )
