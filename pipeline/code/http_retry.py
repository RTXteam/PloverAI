from __future__ import annotations

# time.sleep: stdlib. exponential backoff between retry attempts.
import time

# logging: stdlib. the caller passes its per-run logger so retries show up
# in the same log as the request that triggered them.
import logging

# collections.abc / typing: the generic callable + return type so the
# helper is reusable across clients without losing the response type.
from collections.abc import Callable
from typing import TypeVar

# httpx: third-party. the transient transport exceptions we retry on.
import httpx

T = TypeVar("T")

# Transient transport failures worth retrying: timeouts and network errors
# (connect / read / write). Deliberately NOT httpx.ProtocolError or HTTP
# 4xx/5xx — a bad status is surfaced by the caller's own status check, and
# retrying it blindly would just hide a real error.
_TRANSIENT: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
)


def request_with_retries(
    send: Callable[[], T],
    *,
    max_retries: int,
    service: str,
    logger: logging.Logger,
    backoff_base_s: float = 0.5,
) -> T:
    # Call send(), retrying on transient network/timeout errors with
    # exponential backoff. Used for the idempotent RENCI reads (NodeNorm,
    # NameRes) so a transient hang recovers automatically instead of
    # surfacing as a failed query. Non-transient httpx errors — and the last
    # transient one after max_retries is exhausted — propagate unchanged, so
    # the caller's existing error handling still runs.
    attempt = 0
    while True:
        try:
            return send()
        except _TRANSIENT as e:
            attempt += 1
            if attempt > max_retries:
                logger.warning(
                    f"{service}: transient {type(e).__name__} persisted after "
                    f"{max_retries} retries; giving up"
                )
                raise
            backoff = backoff_base_s * (2 ** (attempt - 1))
            logger.warning(
                f"{service}: transient {type(e).__name__}; "
                f"retry {attempt}/{max_retries} in {backoff:.1f}s"
            )
            time.sleep(backoff)
