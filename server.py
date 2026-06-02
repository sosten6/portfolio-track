"""
server.py
─────────
FastAPI backend that exposes portfolio data for the React frontend (Phase 2).

Endpoints
─────────
GET  /health                          – Liveness probe
GET  /api/portfolio/{telegram_id}     – Full portfolio (wallets + exchanges + totals)
GET  /api/wallets/{telegram_id}       – List of wallets with current balances
GET  /api/exchanges/{telegram_id}     – List of exchanges with current balances
GET  /api/history/{telegram_id}       – Balance log for charting (last N days)
GET  /api/balances/{address}?chain=   – Raw balance for one address (public endpoint)

All /api/portfolio/* endpoints require the X-Telegram-ID header to match the path
parameter (minimal auth; full JWT auth recommended for production).

Run:  uvicorn server:app --host 0.0.0.0 --port 8000
"""
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import asyncio

from db import get_db, get_user_by_telegram_id, BalanceLog, Wallet, Exchange
from crypto import decrypt
from services.wallets import get_wallet_balance, CHAIN_SYMBOL
from services.exchanges import get_exchange_balance
from services.prices import get_usd_prices

app = FastAPI(
    title="Crypto Portfolio API",
    description="Backend for the crypto portfolio tracker bot and React dashboard.",
    version="1.0.0",
)

# Allow the React dev server (localhost:5173) and any deployed frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ── Raw single wallet balance (no auth needed) ─────────────────────────────────

@app.get("/api/balances/{address}")
async def get_address_balance(
    address: str,
    chain: str = Query("ethereum", description="Chain: ethereum | bnb | solana | bitcoin"),
):
    """Fetch the native token balance for a single address. No auth required."""
    result = await get_wallet_balance(chain, address)
    if result["error"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── Helper: resolve telegram_id → db user ──────────────────────────────────────

def _get_user_or_404(telegram_id: int, db: Session):
    user = get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Please /start the bot first.")
    return user


# ── Portfolio summary ──────────────────────────────────────────────────────────

@app.get("/api/portfolio/{telegram_id}")
async def get_portfolio(telegram_id: int, db: Session = Depends(get_db)):
    """
    Full portfolio: all wallets + exchanges, aggregated by asset, with USD totals.
    """
    user = _get_user_or_404(telegram_id, db)

    # Build tasks for all wallets
    wallet_tasks = [
        get_wallet_balance(w.chain, w.address)
        for w in user.wallets
    ]

    # Build tasks for all exchanges
    exchange_tasks = []
    exchange_meta  = []
    for e in user.exchanges:
        try:
            api_key    = decrypt(e.api_key)
            api_secret = decrypt(e.api_secret)
            api_pw     = decrypt(e.api_password) if e.api_password else None
        except ValueError:
            continue
        exchange_tasks.append(get_exchange_balance(e.exchange_id, api_key, api_secret, api_pw))
        exchange_meta.append({"id": e.id, "label": e.label or e.exchange_id})

    wallet_results, exchange_results = await asyncio.gather(
        asyncio.gather(*wallet_tasks) if wallet_tasks else asyncio.coroutine(lambda: [])(),
        asyncio.gather(*exchange_tasks) if exchange_tasks else asyncio.coroutine(lambda: [])(),
    )

    # Aggregate totals by symbol
    totals: dict[str, float] = {}

    wallet_data = []
    for wallet, result in zip(user.wallets, wallet_results or []):
        if not result.get("error"):
            sym = result["symbol"]
            totals[sym] = totals.get(sym, 0) + result["balance"]
        wallet_data.append({
            "label":   wallet.label,
            "chain":   wallet.chain,
            "address": wallet.address,
            **result,
        })

    exchange_data = []
    for meta, result in zip(exchange_meta, exchange_results or []):
        for b in result.get("balances", []):
            totals[b["asset"]] = totals.get(b["asset"], 0) + b["total"]
        exchange_data.append({**meta, **result})

    prices = await get_usd_prices(list(totals.keys()))

    holdings = []
    grand_total = 0.0
    for sym, amt in totals.items():
        usd = amt * prices.get(sym, 0)
        grand_total += usd
        holdings.append({"asset": sym, "amount": amt, "usd_value": usd, "price": prices.get(sym, 0)})

    holdings.sort(key=lambda x: x["usd_value"], reverse=True)

    return {
        "telegram_id":  telegram_id,
        "total_usd":    round(grand_total, 2),
        "holdings":     holdings,
        "wallets":      wallet_data,
        "exchanges":    exchange_data,
        "fetched_at":   datetime.utcnow().isoformat(),
    }


# ── Balance history (for charts) ──────────────────────────────────────────────

@app.get("/api/history/{telegram_id}")
def get_history(
    telegram_id: int,
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """
    Return balance log entries for the last N days, grouped by asset.
    The React frontend uses this to draw the portfolio history chart.
    """
    user = _get_user_or_404(telegram_id, db)
    since = datetime.utcnow() - timedelta(days=days)

    logs = (
        db.query(BalanceLog)
        .filter(BalanceLog.user_id == user.id, BalanceLog.recorded_at >= since)
        .order_by(BalanceLog.recorded_at.asc())
        .all()
    )

    # Group by asset → sorted list of {timestamp, amount, usd_value}
    by_asset: dict[str, list] = {}
    for entry in logs:
        by_asset.setdefault(entry.asset, []).append({
            "timestamp": entry.recorded_at.isoformat(),
            "amount":    entry.amount,
            "usd_value": entry.usd_value,
        })

    return {
        "telegram_id": telegram_id,
        "days":        days,
        "assets":      by_asset,
    }