"""
services/cache.py — TTL cache for balance results + rate limiter
"""
import time
import logging
from typing import Any
from collections import defaultdict

log = logging.getLogger(__name__)

# ── Balance cache ─────────────────────────────────────────────────────────────
_CACHE: dict[str, dict] = {}
CACHE_TTL    = 90   # serve fresh if within 90s
FALLBACK_TTL = 300  # use as fallback for up to 5min


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


# ── Rate limiter ──────────────────────────────────────────────────────────────
# {user_id: [timestamp, timestamp, ...]}
_rate_buckets: dict[int, list[float]] = defaultdict(list)


def is_rate_limited(user_id: int, max_per_minute: int = 4) -> bool:
    """
    Token-bucket rate limiter. Returns True if the user has exceeded
    max_per_minute calls in the last 60 seconds.
    """
    now = time.time()
    window = now - 60
    bucket = _rate_buckets[user_id]

    # Remove timestamps older than 60s
    _rate_buckets[user_id] = [t for t in bucket if t > window]

    if len(_rate_buckets[user_id]) >= max_per_minute:
        return True

    _rate_buckets[user_id].append(now)
    return False


def rate_limit_wait_seconds(user_id: int) -> int:
    """How many seconds until the user's oldest request falls out of the window."""
    bucket = _rate_buckets.get(user_id, [])
    if not bucket:
        return 0
    oldest = min(bucket)
    wait = int(60 - (time.time() - oldest)) + 1
    return max(0, wait)