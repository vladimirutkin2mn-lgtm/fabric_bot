"""Replaceable rate limiter behavior."""

from app.bot.rate_limit import FixedWindowRateLimiter


def test_rate_limiter_rejects_burst_and_recovers_after_window() -> None:
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=10)
    assert limiter.allow(123, now=0)
    assert limiter.allow(123, now=1)
    assert not limiter.allow(123, now=2)
    assert limiter.allow(123, now=11)


def test_rate_limiter_is_scoped_per_user() -> None:
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=10)
    assert limiter.allow(1, now=0)
    assert not limiter.allow(1, now=0)
    assert limiter.allow(2, now=0)
