"""
services/wallets.py — multi-chain balance fetching
"""
import asyncio
import os
import aiohttp
from web3 import Web3
import config
from services.cache import cache_set, cache_get, cache_get_fallback

RPC_TIMEOUT = 8

CHAIN_CONFIG: dict[str, dict] = {
    "ethereum": {"rpc": config.ETH_RPC_URL,     "symbol": "ETH",  "type": "evm",     "label": "Ethereum",  "emoji": "⟠"},
    "bnb":      {"rpc": config.BNB_RPC_URL,      "symbol": "BNB",  "type": "evm",     "label": "BNB Chain", "emoji": "🟡"},
    "polygon":  {"rpc": config.POLYGON_RPC_URL,  "symbol": "POL",  "type": "evm",     "label": "Polygon",   "emoji": "🟣"},
    "arbitrum": {"rpc": config.ARBITRUM_RPC_URL, "symbol": "ETH",  "type": "evm",     "label": "Arbitrum",  "emoji": "🔵"},
    "optimism": {"rpc": config.OPTIMISM_RPC_URL, "symbol": "ETH",  "type": "evm",     "label": "Optimism",  "emoji": "🔴"},
    "base":     {"rpc": config.BASE_RPC_URL,     "symbol": "ETH",  "type": "evm",     "label": "Base",      "emoji": "🔷"},
    "avalanche":{"rpc": config.AVAX_RPC_URL,     "symbol": "AVAX", "type": "evm",     "label": "Avalanche", "emoji": "🔺"},
    "solana":   {"rpc": config.SOLANA_RPC_URL,   "symbol": "SOL",  "type": "solana",  "label": "Solana",    "emoji": "◎"},
    "bitcoin":  {"rpc": config.BITCOIN_API_URL,  "symbol": "BTC",  "type": "bitcoin", "label": "Bitcoin",   "emoji": "₿"},
}

CHAIN_SYMBOL = {k: v["symbol"] for k, v in CHAIN_CONFIG.items()}
EVM_CHAINS   = [k for k, v in CHAIN_CONFIG.items() if v["type"] == "evm"]


def detect_address_type(address: str) -> str:
    address = address.strip()
    if address.startswith("0x") and len(address) == 42:
        return "evm"
    if address.startswith("bc1") or (len(address) in range(25, 36) and address[0] in "13"):
        return "bitcoin"
    if 32 <= len(address) <= 44 and not address.startswith("0x"):
        return "solana"
    return "unknown"


async def _get_evm_balance(rpc_url: str, address: str) -> float:
    # If rpc_url contains a placeholder (empty key), rebuild it with the live env var
    api_key = os.environ.get("ALCHEMY_API_KEY", "") or config.ALCHEMY_API_KEY
    if "alchemy.com/v2/" in rpc_url and api_key:
        # Ensure the key in the URL is the current live value, not a stale import-time value
        base = rpc_url.split("/v2/")[0]
        rpc_url = f"{base}/v2/{api_key}"
    loop = asyncio.get_event_loop()
    def _fetch():
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": RPC_TIMEOUT}))
        checksum = Web3.to_checksum_address(address)
        wei = w3.eth.get_balance(checksum)
        return float(Web3.from_wei(wei, "ether"))
    return await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=RPC_TIMEOUT + 2)


async def _get_erc20_tokens(chain: str, address: str) -> list[dict]:
    # Read key at call time (not import time) so Railway env vars are always fresh
    api_key = os.environ.get("ALCHEMY_API_KEY", "") or config.ALCHEMY_API_KEY
    if not api_key:
        return []
    alchemy_networks = {
        "ethereum": f"eth-mainnet.g.alchemy.com/v2/{api_key}",
        "polygon":  f"polygon-mainnet.g.alchemy.com/v2/{api_key}",
        "arbitrum": f"arb-mainnet.g.alchemy.com/v2/{api_key}",
        "optimism": f"opt-mainnet.g.alchemy.com/v2/{api_key}",
        "base":     f"base-mainnet.g.alchemy.com/v2/{api_key}",
    }
    network = alchemy_networks.get(chain)
    if not network:
        return []
    url = f"https://{network}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "alchemy_getTokenBalances", "params": [address, "erc20"]}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        token_balances = data.get("result", {}).get("tokenBalances", [])
        non_zero = [t for t in token_balances if t.get("tokenBalance") and t["tokenBalance"] != "0x0"]
        tokens = []
        for tb in non_zero[:50]:
            raw_bal = int(tb["tokenBalance"], 16)
            if raw_bal == 0:
                continue
            meta_payload = {"jsonrpc": "2.0", "id": 1, "method": "alchemy_getTokenMetadata", "params": [tb["contractAddress"]]}
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=meta_payload) as resp:
                        meta = (await resp.json()).get("result", {})
            except Exception:
                continue
            decimals = meta.get("decimals") or 18
            symbol   = meta.get("symbol") or "?"
            balance  = raw_bal / (10 ** decimals)
            if balance < 1e-8:
                continue
            tokens.append({"symbol": symbol, "name": meta.get("name") or symbol, "balance": balance})
        return tokens
    except Exception:
        return []


async def _get_sol_balance(rpc_url: str, address: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]}
    timeout = aiohttp.ClientTimeout(total=RPC_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(rpc_url, json=payload) as resp:
            data = await resp.json()
    return data["result"]["value"] / 1_000_000_000


async def _get_btc_balance(api_url: str, address: str) -> float:
    url = f"{api_url}/address/{address}"
    timeout = aiohttp.ClientTimeout(total=RPC_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status == 400:
                raise ValueError(f"Invalid Bitcoin address: {address}")
            data = await resp.json()
    stats    = data.get("chain_stats", {})
    satoshis = stats.get("funded_txo_sum", 0) - stats.get("spent_txo_sum", 0)
    return satoshis / 100_000_000


async def get_wallet_balance(chain: str, address: str, include_tokens: bool = True) -> dict:
    cache_key = f"{chain}:{address}"
    cached = cache_get("wallet", cache_key)
    if cached:
        return cached

    cfg = CHAIN_CONFIG.get(chain.lower())
    if not cfg:
        return {"chain": chain, "symbol": "?", "address": address, "balance": 0.0,
                "error": f"Unsupported chain: {chain}", "label": None, "tokens": []}
    try:
        if cfg["type"] == "evm":
            balance = await _get_evm_balance(cfg["rpc"], address)
            tokens  = await _get_erc20_tokens(chain, address) if include_tokens else []
        elif cfg["type"] == "solana":
            balance = await _get_sol_balance(cfg["rpc"], address)
            tokens  = []
        elif cfg["type"] == "bitcoin":
            balance = await _get_btc_balance(cfg["rpc"], address)
            tokens  = []
        else:
            raise ValueError(f"Unknown type: {cfg['type']}")

        result = {"chain": chain, "symbol": cfg["symbol"], "address": address,
                  "balance": balance, "error": None, "label": cfg["label"], "tokens": tokens}
        cache_set("wallet", cache_key, result)
        return result
    except asyncio.TimeoutError:
        fallback, age = cache_get_fallback("wallet", cache_key)
        if fallback:
            return {**dict(fallback), "_stale": age}
        return {"chain": chain, "symbol": cfg["symbol"], "address": address,
                "balance": 0.0, "error": "RPC timed out", "label": cfg["label"], "tokens": []}
    except Exception as exc:
        fallback, age = cache_get_fallback("wallet", cache_key)
        if fallback:
            return {**dict(fallback), "_stale": age}
        return {"chain": chain, "symbol": cfg["symbol"], "address": address,
                "balance": 0.0, "error": str(exc)[:100], "label": cfg["label"], "tokens": []}


async def get_all_wallet_balances(wallets: list[dict], include_tokens: bool = True) -> list[dict]:
    tasks   = [get_wallet_balance(w["chain"], w["address"], include_tokens) for w in wallets]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    for result, wallet in zip(results, wallets):
        result["label"] = wallet.get("label") or result.get("label")
    return list(results)