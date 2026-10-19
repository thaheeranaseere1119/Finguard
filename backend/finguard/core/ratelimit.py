"""Fixed-window rate limiter: Redis when available, in-process fallback (single instance / tests)."""

from __future__ import annotations

import threading
import time
from functools import lru_cache

from finguard.core.config import get_settings

_lock = threading.Lock()
_mem: dict[str, tuple[int, float]] = {}


@lru_cache
def _redis():
    try:
        from redis import Redis

        r = Redis.from_url(get_settings().redis_url, socket_connect_timeout=0.3, socket_timeout=0.3)
        r.ping()
        return r
    except Exception:
        return None


def hit(key: str, limit: int, window_seconds: int) -> bool:
    """Record one hit; return False if the limit for the current window is exceeded."""
    if get_settings().environment == "test":
        return _mem_hit(key, limit, window_seconds)
    r = _redis()
    if r is not None:
        try:
            bucket = f"rl:{key}:{int(time.time() // window_seconds)}"
            n = r.incr(bucket)
            if n == 1:
                r.expire(bucket, window_seconds + 1)
            return n <= limit
        except Exception:
            pass
    return _mem_hit(key, limit, window_seconds)


def _mem_hit(key: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    with _lock:
        count, start = _mem.get(key, (0, now))
        if now - start >= window_seconds:
            count, start = 0, now
        count += 1
        _mem[key] = (count, start)
        return count <= limit


def reset() -> None:
    with _lock:
        _mem.clear()
