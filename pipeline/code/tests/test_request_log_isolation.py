# spec for the per-request log streams of /api/v1/query/stream. every
# question runs in its own worker thread and streams its own log lines
# (the live trace) and stage events (the live graph) to its browser,
# while all of them log through the one shared "ploverai" logger.
#
#   - a handler filtered for request A receives A's records only, even
#     while request B logs on another thread at the same time.
#   - work a request hands to a thread pool (Stage 4's parallel entity
#     probes) still counts as that request's when wrapped in in_context.
#   - a record logged outside any request reaches no request's stream.

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from pipeline.code.logging_setup import RequestFilter, current_request, in_context


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _logger() -> logging.Logger:
    logger = logging.getLogger("ploverai-test-isolation")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


def test_parallel_requests_get_only_their_own_lines() -> None:
    logger = _logger()
    streams = {name: Collect() for name in ("A", "B")}
    for name, handler in streams.items():
        handler.addFilter(RequestFilter(name))
        logger.addHandler(handler)
    both_logging = threading.Barrier(2)

    def request(name: str) -> None:
        current_request.set(name)
        both_logging.wait()
        for i in range(50):
            logger.info(f"{name} line {i}")
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(in_context(lambda i: logger.info(f"{name} probe {i}")), range(3)))

    threads = [threading.Thread(target=request, args=(name,)) for name in streams]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for name, handler in streams.items():
        assert len(handler.messages) == 53
        assert all(m.startswith(f"{name} ") for m in handler.messages)
        assert sum("probe" in m for m in handler.messages) == 3


def test_a_record_outside_any_request_reaches_no_stream() -> None:
    logger = _logger()
    handler = Collect()
    handler.addFilter(RequestFilter("A"))
    logger.addHandler(handler)

    def outside() -> None:
        logger.info("server start-up line")

    t = threading.Thread(target=outside)
    t.start()
    t.join()
    assert handler.messages == []
