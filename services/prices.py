"""
services/prices.py
──────────────────
USD price lookup via CoinGecko free API + Binance public ticker fallback.

Strategy:
1. Stablecoins → 1.0 (no API call)
2. Known symbols → CoinGecko (cached 60s)
3. Unknown symbols → Binance public ticker (no key needed)
4. Still unknown → 0.0

This ensures assets like LAYER, KITE, SXT etc. get real prices
even if they're not in the hardcoded CoinGecko map.
"""
import asyncio
import time
import logging
import aiohttp

log = logging.getLogger(__name__)

STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDS", "PYUSD", "USDP", "FRAX"}

# CoinGecko IDs for the most common assets
SYMBOL_TO_CG_ID: dict[str, str] = {
    "BTC":   "bitcoin",
    "ETH":   "ethereum",
    "BNB":   "binancecoin",
    "SOL":   "solana",
    "XRP":   "ripple",
    "ADA":   "cardano",
    "DOGE":  "dogecoin",
    "POL":   "matic-network",
    "MATIC": "matic-network",
    "DOT":   "polkadot",
    "AVAX":  "avalanche-2",
    "LINK":  "chainlink",
    "UNI":   "uniswap",
    "ATOM":  "cosmos",
    "LTC":   "litecoin",
    "TRX":   "tron",
    "TON":   "the-open-network",
    "SHIB":  "shiba-inu",
    "PEPE":  "pepe",
    "SUI":   "sui",
    "APT":   "aptos",
    "OP":    "optimism",
    "ARB":   "arbitrum",
    "INJ":   "injective-protocol",
    "NEAR":  "near",
    "FIL":   "filecoin",
    "AAVE":  "aave",
    "MKR":   "maker",
    "WLD":   "worldcoin-wld",
    "FET":   "fetch-ai",
    "RNDR":  "render-token",
    "IMX":   "immutable-x",
    "GRT":   "the-graph",
    "SAND":  "the-sandbox",
    "MANA":  "decentraland",
    "AXS":   "axie-infinity",
    "CHZ":   "chiliz",
    "ENS":   "ethereum-name-service",
    "LDO":   "lido-dao",
    "CRV":   "curve-dao-token",
    "SUSHI": "sushi",
    "CAKE":  "pancakeswap-token",
    "ALGO":  "algorand",
    "VET":   "vechain",
    "ICP":   "internet-computer",
    "XLM":   "stellar",
    "ETC":   "ethereum-classic",
    "BCH":   "bitcoin-cash",
    "SEI":   "sei-network",
    "SXT":   "space-and-time",
    "LAYER": "layer-zero",         # may need updating
    "KITE":  "kite-ai",            # may need updating
    "ZKC":   "zkcross",            # may need updating
}

# Cache
_cg_cache: dict[str, float]    = {}
_cg_cache_ts: float            = 0
_binance_cache: dict[str, float] = {}
_binance_cache_ts: float       = 0
_CACHE_TTL = 60  # seconds


async def _fetch_coingecko(symbols: list[str]) -> dict[str, float]:
    """Fetch prices from CoinGecko for known symbols."""
    global _cg_cache, _cg_cache_ts

    now = time.time()
    if now - _cg_cache_ts < _CACHE_TTL and all(s in _cg_cache for s in symbols):
        return {s: _cg_cache[s] for s in symbols}

    ids = ",".join(SYMBOL_TO_CG_ID[s] for s in symbols if s in SYMBOL_TO_CG_ID)
    if not ids:
        return {}

    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {s: _cg_cache.get(s, 0.0) for s in symbols}
                data = await resp.json()

        cg_to_sym = {v: k for k, v in SYMBOL_TO_CG_ID.items()}
        for cg_id, price_data in data.items():
            sym = cg_to_sym.get(cg_id)
            if sym:
                _cg_cache[sym] = price_data.get("usd", 0.0)
        _cg_cache_ts = now

    except Exception as e:
        log.warning(f"CoinGecko fetch failed: {e}")

    return {s: _cg_cache.get(s, 0.0) for s in symbols}


async def _fetch_binance_prices(symbols: list[str]) -> dict[str, float]:
    """
    Use Binance public ticker API (no key needed) to price any symbol.
    Tries SYMBOL+USDT pair first, then SYMBOL+BTC.
    """
    global _binance_cache, _binance_cache_ts

    now = time.time()
    needed = [s for s in symbols if s not in _binance_cache or now - _binance_cache_ts >= _CACHE_TTL]
    if not needed:
        return {s: _binance_cache.get(s, 0.0) for s in symbols}

    # Fetch all 24hr tickers in one call (Binance allows this)
    url = "https://api.binance.com/api/v3/ticker/price"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {s: _binance_cache.get(s, 0.0) for s in symbols}
                tickers = await resp.json()

        # Build a lookup: "BTCUSDT" → price
        ticker_map: dict[str, float] = {}
        for t in tickers:
            try:
                ticker_map[t["symbol"]] = float(t["price"])
            except Exception:
                pass

        # Get BTC price in USDT so we can convert BTC pairs
        btc_price = ticker_map.get("BTCUSDT", 0.0)

        for sym in needed:
            # Try USDT pair first
            price = ticker_map.get(f"{sym}USDT", 0.0)
            if not price and btc_price:
                # Try BTC pair → convert to USD
                btc_pair = ticker_map.get(f"{sym}BTC", 0.0)
                if btc_pair:
                    price = btc_pair * btc_price
            if price:
                _binance_cache[sym] = price

        _binance_cache_ts = now

    except Exception as e:
        log.warning(f"Binance ticker fetch failed: {e}")

    return {s: _binance_cache.get(s, 0.0) for s in symbols}


async def get_usd_prices(symbols: list[str]) -> dict[str, float]:
    """
    Return {SYMBOL: usd_price} for a list of symbols.
    Falls back through: stablecoin → CoinGecko → Binance ticker → 0.0
    """
    if not symbols:
        return {}

    result: dict[str, float] = {}
    need_cg      = []
    need_binance = []

    for sym in symbols:
        if not sym or sym.startswith("LD"):
            result[sym] = 0.0
        elif sym in STABLECOINS:
            result[sym] = 1.0
        elif sym in SYMBOL_TO_CG_ID:
            need_cg.append(sym)
        else:
            need_binance.append(sym)

    # Fetch CoinGecko and Binance concurrently
    cg_prices, bn_prices = await asyncio.gather(
        _fetch_coingecko(need_cg)      if need_cg      else asyncio.sleep(0, result={}),
        _fetch_binance_prices(need_binance) if need_binance else asyncio.sleep(0, result={}),
    )

    result.update(cg_prices)
    result.update(bn_prices)

    # Anything still missing → try Binance as a second-chance for CoinGecko failures
    still_missing = [s for s in need_cg if result.get(s, 0) == 0]
    if still_missing:
        fallback = await _fetch_binance_prices(still_missing)
        for s, p in fallback.items():
            if p > 0:
                result[s] = p

    return result