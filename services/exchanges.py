"""
services/exchanges.py
─────────────────────
CCXT exchange balance fetching with:
- Per-exchange cloud-safe fetch strategies (no IP-restricted endpoints)
- Balance result cache (prevents $0 glitch on concurrent requests)
- Exponential backoff retry on network errors
- LD* prefix stripping (Binance Simple Earn positions)

Cloud hosting note (Render / Railway / Fly.io):
  Many exchanges block certain SAPI endpoints from cloud provider IPs.
  Each exchange has a dedicated fetch function that avoids those endpoints.

  Binance:  skips capital/config (IP-restricted), uses /api/v3/account only
  Bybit:    skips /v5/asset/coin/query-info (IP-restricted), uses unified account
  OKX:      uses trading + funding accounts directly
  Others:   standard fetch_balance with cloud-safe options
"""
import asyncio
import logging

import ccxt.async_support as ccxt

from services.cache import cache_get, cache_get_fallback, cache_set

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

# Disable automatic metadata fetching that triggers IP-restricted SAPI endpoints
_CLOUD_SAFE_OPTIONS: dict = {
    "fetchCurrencies":         False,
    "adjustForTimeDifference": False,
}


# ── Exchange builder ───────────────────────────────────────────────────────────

def _build_exchange(
    exchange_id: str,
    api_key: str,
    api_secret: str,
    password: str | None,
) -> ccxt.Exchange:
    cls_name = SUPPORTED_EXCHANGES.get(exchange_id.lower())
    if not cls_name:
        raise ValueError(f"Exchange '{exchange_id}' not supported.")

    # Per-exchange options tuned for cloud hosting
    options: dict = {**_CLOUD_SAFE_OPTIONS, "defaultType": "spot"}

    if exchange_id == "bybit":
        options["unifiedMargin"] = False   # avoids coin/query-info endpoint

    cfg: dict = {
        "apiKey":          api_key,
        "secret":          api_secret,
        "enableRateLimit": True,
        "options":         options,
    }
    if password:
        cfg["password"] = password

    return getattr(ccxt, cls_name)(cfg)


# ── LD* prefix helpers (Binance Simple Earn) ──────────────────────────────────

def _strip_ld(asset: str) -> str:
    """LDUSDT → USDT, LDBTC → BTC, etc."""
    if asset.startswith("LD") and len(asset) > 3:
        underlying = asset[2:]
        if underlying.isupper() and 2 <= len(underlying) <= 10:
            return underlying
    return asset


def _merge_balances(raw: dict[str, float]) -> dict[str, float]:
    """Merge LD* earn positions into their underlying asset and remove dust."""
    merged: dict[str, float] = {}
    for asset, amount in raw.items():
        if amount and amount > 0:
            key = _strip_ld(asset)
            merged[key] = merged.get(key, 0) + amount
    return merged


# ── Per-exchange fetch strategies ─────────────────────────────────────────────

class GeoBlockedError(Exception):
    """Raised when an exchange returns HTTP 451 — regional block, not fixable by IP whitelisting."""
    pass


async def _fetch_binance(exchange: ccxt.Exchange) -> dict[str, float]:
    """
    Binance safe fetch for cloud IPs.

    Uses only:
      /api/v3/account              — spot + LD* flexible earn  (never IP-restricted)
      /sapi/v1/asset/get-funding-asset — funding wallet        (generally allowed)

    Skips:
      /sapi/v1/capital/config/getall   — IP-restricted on cloud providers
      /sapi/v1/lending/union/account   — deprecated (405)
      cross_margin                     — requires capital/config

    Note: Binance returns HTTP 451 ("Service unavailable from a restricted
    location") for entire hosting regions (AWS/GCP/Azure/Render datacenters
    in the US and elsewhere). This is a full geo-block applied before
    authentication and CANNOT be fixed by IP whitelisting on the API key.
    """
    totals: dict[str, float] = {}
    geo_blocked = False

    async def _safe(params: dict, label: str) -> None:
        nonlocal geo_blocked
        try:
            raw = await exchange.fetch_balance(params=params)
            count = 0
            for asset, amount in raw.get("total", {}).items():
                if amount and amount > 0:
                    totals[asset] = totals.get(asset, 0) + amount
                    count += 1
            if count:
                log.info(f"[binance] {label}: {count} assets")
        except Exception as exc:
            msg = str(exc)
            if "451" in msg or "restricted location" in msg.lower():
                geo_blocked = True
                log.warning(f"[binance] {label}: geo-blocked (451) — hosting region blocked by Binance")
            elif any(x in msg for x in ["403", "405", "capital/config", "deprecated", "query-info"]):
                log.debug(f"[binance] {label} skipped (cloud IP restricted or deprecated)")
            else:
                log.warning(f"[binance] {label} failed: {msg[:120]}")

    await asyncio.gather(
        _safe({},                  "spot+earn"),
        _safe({"type": "funding"}, "funding"),
    )

    if geo_blocked and not totals:
        raise GeoBlockedError(
            "Binance has geo-blocked this server's hosting region (HTTP 451). "
            "This cannot be fixed by IP whitelisting — Binance blocks the entire "
            "datacenter, not just your key. Try a VPS in a non-blocked region "
            "(e.g. outside the US), or use a different exchange."
        )

    return totals


async def _fetch_bybit(exchange: ccxt.Exchange) -> dict[str, float]:
    """
    Bybit safe fetch.
    /v5/asset/coin/query-info is blocked on cloud IPs -> use unified account.
    Tries both unified and spot account types since funds may be in either.
    """
    totals: dict[str, float] = {}
    last_error: str | None = None
    any_success = False

    for acct_type, label in [("unified", "unified"), ("spot", "spot")]:
        try:
            raw = await exchange.fetch_balance(params={"type": acct_type})
            any_success = True
            count = 0
            for asset, amount in raw.get("total", {}).items():
                if amount and amount > 0:
                    totals[asset] = totals.get(asset, 0) + amount
                    count += 1
            log.info(f"[bybit] {label}: {count} assets")
        except Exception as exc:
            msg = str(exc)
            last_error = msg[:150]
            if "403" in msg or "query-info" in msg:
                log.debug(f"[bybit] {label} skipped (cloud IP restricted): {msg[:100]}")
            else:
                log.warning(f"[bybit] {label} failed: {msg[:120]}")

    # If every account type failed outright (not just empty), surface the error
    # instead of silently returning {} -> "No balances found"
    if not any_success and last_error:
        raise ccxt.ExchangeError(f"All account types failed: {last_error}")

    return totals


async def _fetch_okx(exchange: ccxt.Exchange) -> dict[str, float]:
    """OKX: trading + funding accounts."""
    totals: dict[str, float] = {}

    for acct_type, label in [("trading", "trading"), ("funding", "funding")]:
        try:
            raw = await exchange.fetch_balance(params={"type": acct_type})
            count = 0
            for asset, amount in raw.get("total", {}).items():
                if amount and amount > 0:
                    totals[asset] = totals.get(asset, 0) + amount
                    count += 1
            if count:
                log.info(f"[okx] {label}: {count} assets")
        except Exception as exc:
            log.warning(f"[okx] {label} failed: {str(exc)[:100]}")

    return totals


async def _fetch_generic(exchange: ccxt.Exchange, exchange_id: str) -> dict[str, float]:
    """Generic single fetch_balance call, with spot fallback."""
    try:
        raw    = await exchange.fetch_balance()
        totals = {a: v for a, v in raw.get("total", {}).items() if v and v > 0}
        log.info(f"[{exchange_id}] spot: {len(totals)} assets")
        return totals
    except Exception as exc:
        msg = str(exc)
        # If it's an IP restriction, try explicit spot type
        if "403" in msg or "query-info" in msg or "capital/config" in msg:
            log.debug(f"[{exchange_id}] retrying with explicit spot type")
            raw    = await exchange.fetch_balance(params={"type": "spot"})
            totals = {a: v for a, v in raw.get("total", {}).items() if v and v > 0}
            return totals
        raise


async def _fetch_all(exchange: ccxt.Exchange, exchange_id: str) -> dict[str, float]:
    """Route to the right fetch strategy per exchange."""
    eid = exchange_id.lower()
    if eid == "binance":
        return await _fetch_binance(exchange)
    if eid == "bybit":
        return await _fetch_bybit(exchange)
    if eid == "okx":
        return await _fetch_okx(exchange)
    return await _fetch_generic(exchange, exchange_id)


# ── Public API ────────────────────────────────────────────────────────────────

async def get_exchange_balance(
    exchange_id: str,
    api_key: str,
    api_secret: str,
    password: str | None = None,
) -> dict:
    """
    Fetch all balances for one exchange connection.

    Returns:
    {
        "exchange": "binance",
        "balances": [{"asset": "USDT", "free": 100.0, "locked": 0.0, "total": 100.0}, ...],
        "error":    None,
        "_stale":   <int seconds>  # only present when serving cached data
    }
    """
    cache_key = f"{exchange_id}:{api_key[:8]}"

    # Serve from cache if fresh (< 90s old)
    cached = cache_get("exchange", cache_key)
    if cached:
        return cached

    exchange = None
    try:
        exchange = _build_exchange(exchange_id, api_key, api_secret, password)

        # Fetch with retry (exponential backoff on network errors)
        raw_totals: dict[str, float] = {}
        last_err: Exception | None = None

        for attempt in range(3):
            try:
                # Hard outer timeout — prevents a hung/leaked socket from
                # blocking the whole bot indefinitely (seen on Render free
                # tier after Binance 451 errors leave sessions half-open).
                raw_totals = await asyncio.wait_for(
                    _fetch_all(exchange, exchange_id), timeout=15.0
                )
                last_err = None
                break
            except asyncio.TimeoutError as exc:
                last_err = exc
                log.warning(f"[{exchange_id}] fetch timed out after 15s (attempt {attempt + 1}/3)")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except ccxt.NetworkError as exc:
                last_err = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)   # 1s, 2s
            except Exception as exc:
                last_err = exc
                break

        if last_err and not raw_totals:
            raise last_err

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

    except GeoBlockedError as exc:
        return {
            "exchange": exchange_id, "balances": [],
            "error": str(exc),
        }
    except ccxt.AuthenticationError as exc:
        return {
            "exchange": exchange_id, "balances": [],
            "error": f"Auth failed — check API keys. ({str(exc)[:80]})",
        }
    except ccxt.NetworkError as exc:
        fallback, age = cache_get_fallback("exchange", cache_key)
        if fallback:
            return {**dict(fallback), "_stale": age}
        return {"exchange": exchange_id, "balances": [], "error": f"Network error: {str(exc)[:80]}"}
    except ccxt.ExchangeError as exc:
        return {"exchange": exchange_id, "balances": [], "error": f"Exchange error: {str(exc)[:80]}"}
    except Exception as exc:
        fallback, age = cache_get_fallback("exchange", cache_key)
        if fallback:
            return {**dict(fallback), "_stale": age}
        return {"exchange": exchange_id, "balances": [], "error": str(exc)[:120]}
    finally:
        if exchange:
            try:
                await asyncio.wait_for(exchange.close(), timeout=5.0)
            except (Exception, asyncio.TimeoutError):
                pass


async def get_all_exchange_balances(exchanges: list[dict]) -> list[dict]:
    """Fetch all exchange balances concurrently."""
    tasks = [
        get_exchange_balance(
            ex["exchange_id"],
            ex["api_key"],
            ex["api_secret"],
            ex.get("api_password"),
        )
        for ex in exchanges
    ]
    return list(await asyncio.gather(*tasks))