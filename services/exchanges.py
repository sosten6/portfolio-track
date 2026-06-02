"""
services/exchanges.py
─────────────────────
Fetches balances from exchanges via CCXT.

Binance sub-accounts fetched:
  spot+earn  — spot wallet + LD* flexible earn (single call, no deprecated endpoint)
  funding    — funding/payment wallet
  margin     — cross margin (skipped silently if not enabled)

The old "savings" / lending/union/account endpoint (type="savings") returned 405
since Binance deprecated it. It is intentionally removed here.
"""
import asyncio
import logging
import ccxt.async_support as ccxt

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_ld(asset: str) -> str:
    """Strip Binance Simple Earn LD prefix: LDUSDT → USDT, LDBTC → BTC."""
    if asset.startswith("LD") and len(asset) > 3:
        underlying = asset[2:]
        if underlying.isupper() and 2 <= len(underlying) <= 10:
            return underlying
    return asset


def _merge_balances(raw_totals: dict[str, float]) -> dict[str, float]:
    """Merge LD* earn positions into their underlying: LDUSDT+USDT → USDT."""
    merged: dict[str, float] = {}
    for asset, amount in raw_totals.items():
        if amount and amount > 0:
            merged[_strip_ld(asset)] = merged.get(_strip_ld(asset), 0) + amount
    return merged


def _build_exchange(exchange_id: str, api_key: str, api_secret: str, password: str | None):
    cls_name = SUPPORTED_EXCHANGES.get(exchange_id.lower())
    if not cls_name:
        raise ValueError(f"Exchange '{exchange_id}' not supported.")
    cfg = {
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
    if password:
        cfg["password"] = password
    return getattr(ccxt, cls_name)(cfg)


# ── Binance ───────────────────────────────────────────────────────────────────

async def _fetch_binance_all(exchange) -> dict[str, float]:
    """
    Fetch Binance balances from active sub-accounts only.
    Removed: type="savings" (deprecated, returns 405 on all accounts).
    spot+earn call already returns LD* prefixed flexible earn balances.
    """
    totals: dict[str, float] = {}

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
            # Only log as warning for unexpected errors, not deprecation noise
            msg = str(e)
            if "405" in msg or "deprecated" in msg.lower():
                log.debug(f"[binance] {label} skipped (deprecated): {msg[:80]}")
            else:
                log.warning(f"[binance] {label} failed: {msg[:120]}")

    await asyncio.gather(
        _safe({},                       "spot+earn"),   # covers spot + LD* flexible earn
        _safe({"type": "funding"},      "funding"),     # funding wallet
        _safe({"type": "cross_margin"}, "margin"),      # margin (skipped if not enabled)
    )
    return totals


# ── Generic ───────────────────────────────────────────────────────────────────

async def _fetch_generic(exchange) -> dict[str, float]:
    """Fetch balances for non-Binance exchanges. Tries funding sub-account too."""
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


# ── Public API ────────────────────────────────────────────────────────────────

async def get_exchange_balance(
    exchange_id: str,
    api_key: str,
    api_secret: str,
    password: str | None = None,
) -> dict:
    exchange = None
    try:
        exchange = _build_exchange(exchange_id, api_key, api_secret, password)

        raw_totals = (
            await _fetch_binance_all(exchange)
            if exchange_id.lower() == "binance"
            else await _fetch_generic(exchange)
        )

        merged   = _merge_balances(raw_totals)
        balances = [
            {"asset": asset, "free": total, "locked": 0.0, "total": total}
            for asset, total in merged.items()
            if total >= 1e-8
        ]
        balances.sort(key=lambda x: x["total"], reverse=True)
        return {"exchange": exchange_id, "balances": balances, "error": None}

    except ccxt.AuthenticationError as e:
        return {"exchange": exchange_id, "balances": [], "error": f"Auth failed — check API keys. ({str(e)[:80]})"}
    except ccxt.NetworkError as e:
        return {"exchange": exchange_id, "balances": [], "error": f"Network error: {str(e)[:80]}"}
    except ccxt.ExchangeError as e:
        return {"exchange": exchange_id, "balances": [], "error": f"Exchange error: {str(e)[:80]}"}
    except Exception as e:
        return {"exchange": exchange_id, "balances": [], "error": str(e)[:120]}
    finally:
        if exchange:
            try:
                await exchange.close()
            except Exception:
                pass


async def get_all_exchange_balances(exchanges: list[dict]) -> list[dict]:
    tasks = [
        get_exchange_balance(
            ex["exchange_id"], ex["api_key"],
            ex["api_secret"],  ex.get("api_password"),
        )
        for ex in exchanges
    ]
    return list(await asyncio.gather(*tasks))