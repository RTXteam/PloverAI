# request_with_retries: retry the idempotent RENCI reads on a transient
# network/timeout hiccup, but never retry a real (non-transient) error.
# backoff_base_s=0 keeps the tests instant (no real sleeping).

import logging

import httpx
import pytest

from code.http_retry import request_with_retries

_LOG = logging.getLogger("test_http_retry")


def test_returns_immediately_on_success():
    calls = []

    def send():
        calls.append(1)
        return "ok"

    assert request_with_retries(
        send, max_retries=2, service="X", logger=_LOG, backoff_base_s=0,
    ) == "ok"
    assert len(calls) == 1  # no retry when the first attempt works


def test_recovers_after_a_transient_error():
    calls = []

    def send():
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("transient")  # NetworkError subclass
        return "ok"

    assert request_with_retries(
        send, max_retries=2, service="X", logger=_LOG, backoff_base_s=0,
    ) == "ok"
    assert len(calls) == 2  # failed once, succeeded on the retry


def test_gives_up_and_reraises_after_max_retries():
    calls = []

    def send():
        calls.append(1)
        raise httpx.ReadTimeout("always slow")  # TimeoutException subclass

    with pytest.raises(httpx.ReadTimeout):
        request_with_retries(
            send, max_retries=2, service="X", logger=_LOG, backoff_base_s=0,
        )
    assert len(calls) == 3  # 1 initial + 2 retries, then propagate


def test_does_not_retry_non_transient_errors():
    calls = []

    def send():
        calls.append(1)
        raise ValueError("not a transport hiccup")

    with pytest.raises(ValueError):
        request_with_retries(
            send, max_retries=2, service="X", logger=_LOG, backoff_base_s=0,
        )
    assert len(calls) == 1  # propagates immediately, no retry
