from __future__ import annotations


# public_guard.py — the admission check of the public site, which has no
# login. every question asks admit() first and calls release() when its
# run ends. the rules and their numbers come from the public: block of
# config.yaml; spec: tests/test_public_guard.py.
#
# state lives in memory. that is enough because the service runs as a
# single uvicorn worker (deploy/ploverai-api.service.template); a restart
# resets the counters, which only ever errs towards letting people in.

# math: ceil for whole-second Retry-After values.
import math
# threading: the plain endpoint runs in FastAPI's thread pool and the
# streaming endpoint releases from its worker thread, so every read and
# write of the counters happens under one lock.
import threading
# time: the default clock. epoch seconds, so the UTC day is now // 86400.
import time
# collections.deque: per-address timestamps of admitted questions, oldest
# first, trimmed to the last hour on every check.
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

# PublicLimits: the numbers, read from config.yaml.
from .config import PublicLimits

HOUR_S = 3600
DAY_S = 86400
# how long a caller should wait when every slot is taken. a question
# takes 30-60 s, so half a minute is a fair first retry.
BUSY_RETRY_S = 30


@dataclass(frozen=True)
class Admission:
    allowed: bool
    status: int          # 200 when allowed, otherwise the HTTP status to send
    reason: str          # shown to the visitor when refused
    retry_after_s: int   # 0 when allowed; the Retry-After header otherwise


ALLOWED = Admission(allowed=True, status=200, reason="", retry_after_s=0)


class PublicGuard:
    def __init__(self, limits: PublicLimits, clock: Callable[[], float] = time.time) -> None:
        self._limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: dict[str, deque[float]] = {}
        self._in_flight: dict[str, int] = {}
        self._in_flight_total = 0
        self._day = -1
        self._day_count = 0

    def admit(self, client_ip: str) -> Admission:
        with self._lock:
            now = self._clock()
            day = int(now // DAY_S)
            if day != self._day:
                self._day = day
                self._day_count = 0
            recent = self._trimmed_recent(client_ip, now)

            if self._day_count >= self._limits.questions_per_day:
                return Admission(
                    allowed=False,
                    status=503,
                    reason=(
                        "the public site's daily limit of "
                        f"{self._limits.questions_per_day} questions is reached"
                    ),
                    retry_after_s=math.ceil((day + 1) * DAY_S - now),
                )
            if self._in_flight.get(client_ip, 0) >= self._limits.concurrent_runs_per_ip:
                return Admission(
                    allowed=False,
                    status=429,
                    reason="one question at a time: wait for the current one to finish",
                    retry_after_s=BUSY_RETRY_S,
                )
            if self._in_flight_total >= self._limits.concurrent_runs:
                return Admission(
                    allowed=False,
                    status=503,
                    reason="the service is busy with other questions",
                    retry_after_s=BUSY_RETRY_S,
                )
            if len(recent) >= self._limits.questions_per_ip_per_hour:
                return Admission(
                    allowed=False,
                    status=429,
                    reason=(
                        f"limit of {self._limits.questions_per_ip_per_hour} "
                        "questions per hour from one address reached"
                    ),
                    # with a limit of 0 nothing was ever admitted, so
                    # there is no oldest question to wait for.
                    retry_after_s=math.ceil(recent[0] + HOUR_S - now) if recent else HOUR_S,
                )

            recent.append(now)
            self._recent[client_ip] = recent
            self._in_flight[client_ip] = self._in_flight.get(client_ip, 0) + 1
            self._in_flight_total += 1
            self._day_count += 1
            return ALLOWED

    def release(self, client_ip: str) -> None:
        with self._lock:
            running = self._in_flight.get(client_ip, 0)
            if running == 0:
                return
            if running == 1:
                del self._in_flight[client_ip]
            else:
                self._in_flight[client_ip] = running - 1
            self._in_flight_total -= 1

    def _trimmed_recent(self, client_ip: str, now: float) -> deque[float]:
        # drop timestamps older than one hour. an address with nothing
        # left is forgotten, so the dict does not grow with every visitor.
        recent = self._recent.get(client_ip, deque())
        while recent and recent[0] <= now - HOUR_S:
            recent.popleft()
        if not recent:
            self._recent.pop(client_ip, None)
        return recent
