"""
A short-lived in-memory cache, so a burst of related calls (deep_research, then
read_url on one of its pages, or a reworded search) doesn't hit the same engines
and sites again. It lives as long as the server process.
"""

import logging, os, threading, time
from collections import OrderedDict
from typing import Any, Hashable, Optional

log = logging.getLogger(__name__)


def _ttl() -> float:
    """Seconds an entry stays fresh. MCPSURFR_CACHE_TTL in the server's env changes it, 0 turns caching off."""
    raw = os.environ.get("MCPSURFR_CACHE_TTL", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 300.0
    except ValueError:
        log.warning("MCPSURFR_CACHE_TTL=%r isn't a number, using 300", raw)
        return 300.0


TTL = _ttl()


class TTLCache:
    """Least-recently-used, with expiry. Fetches run on worker threads, hence the lock."""

    def __init__(self, maxsize: int, ttl: float = TTL):
        self.maxsize, self.ttl = maxsize, ttl
        self._data: "OrderedDict[Hashable, tuple]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Optional[Any]:
        if self.ttl <= 0:
            return None
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
