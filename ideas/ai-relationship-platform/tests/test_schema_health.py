"""Packaged Alembic metadata readiness tests."""

from app.services.schema_health import expected_schema_heads


def test_expected_schema_heads_match_current_migration() -> None:
    expected_schema_heads.cache_clear()
    assert expected_schema_heads() == ("20260805_13",)
