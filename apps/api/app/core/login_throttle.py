"""Short source-local login budget for the shipped single-worker API."""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field


class LoginThrottled(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after))


@dataclass
class _Bucket:
    failures: deque = field(default_factory=deque)
    in_flight: int = 0


@dataclass
class LoginAttempt:
    source: str
    bucket: _Bucket
    failed: bool = True


class LoginThrottle:
    def __init__(self, *, max_failures=5, window_seconds=60, max_sources=4096, clock=time.monotonic):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_sources = max_sources
        self.clock = clock
        self._lock = threading.Lock()
        self._sources = {}

    def _expire(self, bucket, now):
        while bucket.failures and bucket.failures[0] <= now - self.window_seconds:
            bucket.failures.popleft()

    def acquire(self, source):
        source = str(source or "unknown")[:100]
        with self._lock:
            now = self.clock()
            bucket = self._sources.get(source)
            if bucket is None:
                if len(self._sources) >= self.max_sources:
                    for key, candidate in list(self._sources.items()):
                        self._expire(candidate, now)
                        if not candidate.in_flight and not candidate.failures:
                            del self._sources[key]
                    if len(self._sources) >= self.max_sources:
                        raise LoginThrottled(1)
                bucket = self._sources[source] = _Bucket()
            self._expire(bucket, now)
            if len(bucket.failures) + bucket.in_flight >= self.max_failures:
                delay = bucket.failures[0] + self.window_seconds - now if bucket.failures else 1
                raise LoginThrottled(math.ceil(delay))
            bucket.in_flight += 1
            return LoginAttempt(source, bucket)

    def finish(self, attempt):
        with self._lock:
            bucket = attempt.bucket
            bucket.in_flight -= 1
            if attempt.failed:
                bucket.failures.append(self.clock())
            self._expire(bucket, self.clock())
            # Success never launders earlier failed attempts. Quiet successful
            # sources need no retained state; rejected attempts never extend TTL.
            if not bucket.failures and not bucket.in_flight and self._sources.get(attempt.source) is bucket:
                self._sources.pop(attempt.source, None)

    def clear(self):
        """Reset isolated test state; not exposed as an application operation."""
        with self._lock:
            self._sources.clear()


login_throttle = LoginThrottle()
