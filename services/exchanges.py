"""
services/exchanges.py — CCXT exchange balance fetching with caching + retry
"""
import asyncio
import logging
import ccxt.async_support as ccxt
from services.cache import cache_set, cache_get, cache_get_fallback

log = logging.getLogger(__name__)

SUPPORTED_EXCHANGES: dict[str, str] = {
    "binance":  "binance",
    "coinbase": "coinbase",
    "kraken":   "kraken",
    "kucoin":   "kucoin",
    "bybit":    "bybit",
    "okx":      "okx",
    "bitget":   "bitget",
    "gate":     "gate",
    "mexc":     "mexc",
    "huobi":    "htx",
}

EXCHANGES_NEEDING_PASSWORD = {"kucoin", "okx", "bitget"}


def _strip_ld(asset: str) -> str:
    if asset.startswith("LD") and len(asset) > 3:
        underlying = asset[2:]
        if underlying.isupper() and 2 <= len(underlying) <= 10:
            return underlying
    return asset


def _merge_balances(raw_totals: dict[str, float]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for asset, amount in raw_totals.items():
        if amount and amount > 0:
            k = _strip_ld(asset)
            merged[k] = merged.get(k, 0) + amount
    return merged


def _build_exchange(exchange_id: str, api_key: str, api_secret: str, password: str | None):
    cls_name = SUPPORTED_EXCHANGES.get(exchange_id.lower())
    if not cls_name:
        raise ValueError(f"Exchange '{exchange_id}' not supported.")
    cfg = {
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
            # Prevent CCXT from loading exchangeInfo on every call.
            # On Render/Railway IPs, Binance blocks the SAPI capital/config
            # endpoint with 403. Pre-loading markets avoids that entirely.
            "fetchCurrencies": False,
        },
    }
    if password:
        cfg["password"] = password
    ex = getattr(ccxt, cls_name)(cfg)
    return ex


async def _fetch_binance_all(exchange) -> dict[str, float]:
    """
    Fetch Binance balances without triggering SAPI capital/config endpoints
    that are IP-restricted (blocked on cloud providers like Render/Railway).

    Strategy:
    - Load markets once (uses /api/v3/exchangeInfo — always allowed)
    - Fetch spot+earn balance (uses /api/v3/account — always allowed)
    - Fetch funding balance (uses /sapi/v1/asset/get-funding-asset — allowed)
    - Skip cross_margin (uses /sapi/v1/capital/config which is IP-restricted)
    """
    totals: dict[str, float] = {}

    # Pre-load markets to avoid implicit SAPI calls during fetch_balance
    try:
        await exchange.load_markets()
    except Exception as e:
        log.debug(f"[binance] load_markets: {str(e)[:60]}")

    async def _safe(params: dict, label: str):
        try:
            raw = await exchange.fetch_balance(params=params)
            count = 0
            for asset, amount in raw.get("total", {}).items():
                if amount and amount > 0:
                    totals[asset] = totals.get(asset, 0) + amount
                    count += 1
            log.info(f"[binance] {label}: {count} assets")
        except Exception as e:
            msg = str(e)
            if any(x in msg for x in ["403", "405", "deprecated", "capital/config"]):
                log.debug(f"[binance] {label} skipped (restricted/deprecated)")
            else:
                log.warning(f"[binance] {label} failed: {msg[:120]}")

    # spot+earn: uses /api/v3/account (never IP-restricted)
    # funding:   uses /sapi/v1/asset/get-funding-asset (generally allowed)
    # margin:    SKIPPED — requires /sapi/v1/capital/config (IP-restricted)
    await asyncio.gather(
        _safe({},                  "spot+earn"),
        _safe({"type": "funding"}, "funding"),
    )
    return totals


async def _fetch_generic(exchange) -> dict[str, float]:
    raw    = await exchange.fetch_balance()
    totals = {a: v for a, v in raw.get("total", {}).items() if v and v > 0}
    for account_type in ["funding"]:
        try:
            raw2 = await exchange.fetch_balance(params={"type": account_type})
            for asset, amount in raw2.get("total", {}).items():
                if amount and amount > 0:
                    totals[asset] = totals.get(asset, 0) + amount
        except Exception:
            pass
    return totals


async def _fetch_with_retry(exchange, exchange_id: str, max_retries: int = 2) -> dict[str, float]:
    """Fetch with exponential backoff retry on network errors."""
    for attempt in range(max_retries + 1):
        try:
            if exchange_id.lower() == "binance":
                return await _fetch_binance_all(exchange)
            else:
                return await _fetch_generic(exchange)
        except ccxt.NetworkError as e:
            if attempt < max_retries:
                wait = 2 ** attempt   # 1s, 2s
                log.warning(f"[{exchange_id}] network error, retrying in {wait}s: {e}")
                await asyncio.sleep(wait)
            else:
                raise
    return {}


async def get_exchange_balance(
    exchange_id: str, api_key: str, api_secret: str, password: str | None = None,
) -> dict:
    cache_key = f"{exchange_id}:{api_key[:8]}"
    cached = cache_get("exchange", cache_key)
    if cached:
        return cached

    exchange = None
    try:
        exchange = _build_exchange(exchange_id, api_key, api_secret, password)
        raw_totals = await _fetch_with_retry(exchange, exchange_id)
        merged   = _merge_balances(raw_totals)
        balances = [
            {"asset": asset, "free": total, "locked": 0.0, "total": total}
            for asset, total in merged.items()
            if total >= 1e-8
        ]
        balances.sort(key=lambda x: x["total"], reverse=True)
        result = {"exchange": exchange_id, "balances": balances, "error": None}
        cache_set("exchange", cache_key, result)
        return result

    except ccxt.AuthenticationError as e:
        return {"exchange": exchange_id, "balances": [], "error": f"Auth failed — check API keys. ({str(e)[:80]})"}
    except ccxt.NetworkError as e:
        fallback, age = cache_get_fallback("exchange", cache_key)
        if fallback:
            fallback["_stale"] = age
            return fallback
        return {"exchange": exchange_id, "balances": [], "error": f"Network error: {str(e)[:80]}"}
    except ccxt.ExchangeError as e:
        return {"exchange": exchange_id, "balances": [], "error": f"Exchange error: {str(e)[:80]}"}
    except Exception as e:
        fallback, age = cache_get_fallback("exchange", cache_key)
        if fallback:
            fallback["_stale"] = age
            return fallback
        return {"exchange": exchange_id, "balances": [], "error": str(e)[:120]}
    finally:
        if exchange:
            try:
                await exchange.close()
            except Exception:
                pass


async def get_all_exchange_balances(exchanges: list[dict]) -> list[dict]:
    tasks = [
        get_exchange_balance(ex["exchange_id"], ex["api_key"], ex["api_secret"], ex.get("api_password"))
        for ex in exchanges
    ]
    return list(await asyncio.gather(*tasks))