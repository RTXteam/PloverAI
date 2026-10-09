# spec for public_guard.PublicGuard, the admission check of the public
# site (no login). every question asks the guard first; a refusal must
# not use up any quota.
#
#   - per address: at most questions_per_ip_per_hour in any sliding
#     3600 s window (429, retry after the oldest one leaves the window),
#     and at most concurrent_runs_per_ip in flight (429, retry 30 s).
#   - all visitors: at most concurrent_runs in flight (503, retry 30 s),
#     and at most questions_per_day per UTC day (503, retry at the next
#     UTC midnight).
#   - release() frees one in-flight slot; releasing an unknown address
#     or releasing twice never goes below zero.

from __future__ import annotations

from datetime import UTC, datetime

from pipeline.code.config import PublicLimits
from pipeline.code.public_guard import PublicGuard

MIDNIGHT = datetime(2026, 9, 30, tzinfo=UTC).timestamp()


class FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_guard(
    clock: FakeClock,
    *,
    per_hour: int = 3,
    per_day: int = 100,
    concurrent: int = 3,
    concurrent_per_ip: int = 1,
) -> PublicGuard:
    limits = PublicLimits(
        models=("m8",),
        questions_per_ip_per_hour=per_hour,
        questions_per_day=per_day,
        concurrent_runs=concurrent,
        concurrent_runs_per_ip=concurrent_per_ip,
    )
    return PublicGuard(limits, clock=clock)


def ask_and_finish(guard: PublicGuard, ip: str) -> int:
    admission = guard.admit(ip)
    if admission.allowed:
        guard.release(ip)
    return admission.status


def test_hourly_limit_per_address_with_exact_retry() -> None:
    clock = FakeClock(MIDNIGHT + 10 * 3600)
    guard = make_guard(clock, per_hour=3)
    for minute in range(3):
        clock.now = MIDNIGHT + 10 * 3600 + minute * 60
        assert ask_and_finish(guard, "1.1.1.1") == 200
    clock.now = MIDNIGHT + 10 * 3600 + 5 * 60
    refused = guard.admit("1.1.1.1")
    assert refused.allowed is False
    assert refused.status == 429
    assert refused.reason == "limit of 3 questions per hour from one address reached"
    # the first question (at +0 s) leaves the window at +3600 s
    assert refused.retry_after_s == 3600 - 5 * 60


def test_hourly_window_slides() -> None:
    clock = FakeClock(MIDNIGHT + 3600)
    guard = make_guard(clock, per_hour=2)
    assert ask_and_finish(guard, "1.1.1.1") == 200
    clock.now += 100
    assert ask_and_finish(guard, "1.1.1.1") == 200
    clock.now += 100
    assert ask_and_finish(guard, "1.1.1.1") == 429
    clock.now = MIDNIGHT + 3600 + 3600
    assert ask_and_finish(guard, "1.1.1.1") == 200
    assert ask_and_finish(guard, "1.1.1.1") == 429


def test_addresses_are_counted_separately() -> None:
    clock = FakeClock(MIDNIGHT)
    guard = make_guard(clock, per_hour=1)
    assert ask_and_finish(guard, "1.1.1.1") == 200
    assert ask_and_finish(guard, "2.2.2.2") == 200
    assert ask_and_finish(guard, "1.1.1.1") == 429


def test_one_question_at_a_time_per_address() -> None:
    clock = FakeClock(MIDNIGHT)
    guard = make_guard(clock, per_hour=10, concurrent_per_ip=1)
    assert guard.admit("1.1.1.1").allowed is True
    busy = guard.admit("1.1.1.1")
    assert (busy.status, busy.retry_after_s) == (429, 30)
    assert busy.reason == "one question at a time: wait for the current one to finish"
    guard.release("1.1.1.1")
    assert guard.admit("1.1.1.1").allowed is True


def test_global_concurrency_cap() -> None:
    clock = FakeClock(MIDNIGHT)
    guard = make_guard(clock, concurrent=2)
    assert guard.admit("1.1.1.1").allowed is True
    assert guard.admit("2.2.2.2").allowed is True
    full = guard.admit("3.3.3.3")
    assert (full.status, full.retry_after_s) == (503, 30)
    assert full.reason == "the service is busy with other questions"
    guard.release("1.1.1.1")
    assert guard.admit("3.3.3.3").allowed is True


def test_daily_cap_resets_at_utc_midnight() -> None:
    clock = FakeClock(MIDNIGHT + 23 * 3600)
    guard = make_guard(clock, per_hour=100, per_day=2)
    assert ask_and_finish(guard, "1.1.1.1") == 200
    assert ask_and_finish(guard, "2.2.2.2") == 200
    clock.now = MIDNIGHT + 23 * 3600 + 1800
    capped = guard.admit("3.3.3.3")
    assert (capped.status, capped.retry_after_s) == (503, 1800)
    assert capped.reason == "the public site's daily limit of 2 questions is reached"
    clock.now = MIDNIGHT + 24 * 3600
    assert ask_and_finish(guard, "3.3.3.3") == 200


def test_refusals_do_not_use_quota() -> None:
    clock = FakeClock(MIDNIGHT)
    guard = make_guard(clock, per_hour=2, concurrent_per_ip=1)
    assert guard.admit("1.1.1.1").allowed is True
    # refused because the first one is still running; must not count
    assert guard.admit("1.1.1.1").status == 429
    assert guard.admit("1.1.1.1").status == 429
    guard.release("1.1.1.1")
    assert ask_and_finish(guard, "1.1.1.1") == 200
    assert ask_and_finish(guard, "1.1.1.1") == 429


def test_release_never_goes_below_zero() -> None:
    clock = FakeClock(MIDNIGHT)
    guard = make_guard(clock, per_hour=10, concurrent=1)
    guard.release("9.9.9.9")
    assert guard.admit("1.1.1.1").allowed is True
    guard.release("1.1.1.1")
    guard.release("1.1.1.1")
    assert guard.admit("2.2.2.2").allowed is True
    assert guard.admit("3.3.3.3").status == 503


def test_zero_limits_close_the_site_without_crashing() -> None:
    clock = FakeClock(MIDNIGHT)
    closed_hourly = make_guard(clock, per_hour=0).admit("1.1.1.1")
    assert (closed_hourly.status, closed_hourly.retry_after_s) == (429, 3600)
    closed_daily = make_guard(clock, per_day=0).admit("1.1.1.1")
    assert (closed_daily.status, closed_daily.retry_after_s) == (503, 86400)
