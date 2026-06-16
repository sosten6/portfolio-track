"""
services/cache.py — TTL cache for balance results + rate limiter
"""
import time
from typing import Any
from collections import defaultdict

_CACHE: dict[str, dict] = {}
CACHE_TTL    = 90
FALLBACK_TTL = 300


def _key(kind: str, identifier: str) -> str:
    return f"{kind}:{identifier}"


def cache_set(kind: str, identifier: str, data: Any) -> None:
    _CACHE[_key(kind, identifier)] = {"data": data, "ts": time.time()}


def cache_get(kind: str, identifier: str, max_age: float = CACHE_TTL) -> Any | None:
    entry = _CACHE.get(_key(kind, identifier))
    if entry and (time.time() - entry["ts"]) <= max_age:
        return entry["data"]
    return None


def cache_get_fallback(kind: str, identifier: str) -> tuple[Any | None, int]:
    entry = _CACHE.get(_key(kind, identifier))
    if entry:
        age = int(time.time() - entry["ts"])
        if age <= FALLBACK_TTL:
            return entry["data"], age
    return None, 0


_rate_buckets: dict[int, list[float]] = defaultdict(list)


def is_rate_limited(user_id: int, max_per_minute: int = 4) -> bool:
    now = time.time()
    window = now - 60
    _rate_buckets[user_id] = [t for t in _rate_buckets[user_id] if t > window]
    if len(_rate_buckets[user_id]) >= max_per_minute:
        return True
    _rate_buckets[user_id].append(now)
    return False


def rate_limit_wait_seconds(user_id: int) -> int:
    bucket = _rate_buckets.get(user_id, [])
    if not bucket:
        return 0
    oldest = min(bucket)
    return max(0, int(60 - (time.time() - oldest)) + 1)