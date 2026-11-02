from people_archive.rate_limit import (
    MAX_RATE_PER_SECOND,
    MIN_RATE_PER_SECOND,
    AdaptiveRateLimiter,
)


def test_rate_limited_response_lowers_the_rate_and_pauses_once_per_window() -> None:
    limiter = AdaptiveRateLimiter(rate_per_second=4.0)

    limiter.record_rate_limited()
    rate_after_first = limiter.rate_per_second
    limiter.record_rate_limited()

    assert rate_after_first < 4.0
    assert limiter.rate_per_second == rate_after_first


def test_sustained_success_raises_the_rate_up_to_the_cap() -> None:
    limiter = AdaptiveRateLimiter(rate_per_second=MAX_RATE_PER_SECOND / 1.05)

    for _ in range(2000):
        limiter.record_success()

    assert limiter.rate_per_second == MAX_RATE_PER_SECOND


def test_rate_never_drops_below_the_floor() -> None:
    limiter = AdaptiveRateLimiter(rate_per_second=MIN_RATE_PER_SECOND)

    limiter.record_rate_limited()

    assert limiter.rate_per_second == MIN_RATE_PER_SECOND


def test_wait_for_slot_returns_immediately_when_interrupted() -> None:
    limiter = AdaptiveRateLimiter(rate_per_second=0.001)
    limiter.interrupt_waits()

    limiter.wait_for_slot()
    limiter.wait_for_slot()
