"""
bot.py — Crypto Portfolio Tracker Bot
Last confirmed working base + 7 critical fixes:
  1. Balance result cache (via services/cache.py)
  2. ERC-20 token balances (via updated services/wallets.py)
  3. /cancel command — escapes any FSM state
  4. Rate limiting — max 4 /balances or /portfolio calls per minute
  5. DB error recovery — graceful startup failure message
  6. BalanceLog pruning — auto-daily via notifications.py
  7. /refresh command — clears cache and re-fetches live
"""
import asyncio
import os
import logging
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

import config
from db import (
    AsyncSessionLocal, init_db,
    User, Wallet, Exchange, UserSettings,
    get_user_by_telegram_id, create_or_update_user,
    get_wallet_exists, get_or_create_settings, group_wallets_by_address,
)
from crypto import encrypt, decrypt
from services.wallets import (
    get_all_wallet_balances, CHAIN_CONFIG, CHAIN_SYMBOL, EVM_CHAINS, detect_address_type,
)
from services.exchanges import get_all_exchange_balances, SUPPORTED_EXCHANGES
from services.prices import get_usd_prices
from services.notifications import poll_and_notify
from services.cache import is_rate_limited, rate_limit_wait_seconds

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot    = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

SUPPORTED_CHAINS = list(CHAIN_CONFIG.keys())

EXCHANGE_GUIDES: dict[str, tuple[str, str]] = {
    "binance":  ("https://www.binance.com/en/my/settings/api-management",
                 "1. Profile → API Management → Create API\n2. Choose System generated\n3. Enable Read Info ONLY\n4. Complete 2FA → copy Key and Secret"),
    "coinbase": ("https://www.coinbase.com/settings/api",
                 "1. Settings → API → New API Key\n2. Permission: view ONLY\n3. Complete 2FA → copy Key and Secret"),
    "kraken":   ("https://www.kraken.com/u/security/api",
                 "1. Settings → API → Add key\n2. Permission: Query Funds ONLY\n3. Generate → copy API Key and Private Key"),
    "kucoin":   ("https://www.kucoin.com/account/api",
                 "1. Account → API Management → Create API\n2. Set passphrase (save it!)\n3. Permission: General ONLY\n4. Copy Key, Secret AND Passphrase"),
    "bybit":    ("https://www.bybit.com/app/user/api-management",
                 "1. Account → API Management → Create New Key\n2. Permissions: Read-Only\n3. Complete 2FA → copy keys"),
    "okx":      ("https://www.okx.com/account/my-api",
                 "1. Account → API → Create APIs\n2. Set passphrase (save it!)\n3. Permission: Read ONLY\n4. Copy Key, Secret AND Passphrase"),
    "bitget":   ("https://www.bitget.com/account/newapi",
                 "1. Profile → API Management → Create API\n2. Set passphrase\n3. Permission: Read-Only\n4. Copy Key, Secret AND Passphrase"),
    "gate":     ("https://www.gate.io/myaccount/apiv4keys",
                 "1. Account → API Management → Create API Key\n2. Permission: Read account info ONLY\n3. Copy Key and Secret"),
    "mexc":     ("https://www.mexc.com/user/openapi",
                 "1. Profile → API → Create\n2. Permission: Account Read ONLY\n3. Copy Access Key and Secret Key"),
    "huobi":    ("https://www.htx.com/en-us/user/api_management",
                 "1. Account → API Management → Create\n2. Permission: Read Only\n3. Copy Access Key and Secret Key"),
}
EXCHANGES_NEEDING_PASSWORD = {"kucoin", "okx", "bitget"}


# ── MarkdownV2 helpers ────────────────────────────────────────────────────────

def md(text) -> str:
    text = str(text)
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def bold(text) -> str:   return f"*{md(text)}*"
def code(text) -> str:   return f"`{md(text)}`"

def fmt_usd(amount: float) -> str:
    if amount < 0.01:
        return "\\<\\$0\\.01"
    return md(f"${amount:,.2f}")

def fmt_num(amount: float, decimals: int = 6) -> str:
    return md(f"{amount:.{decimals}f}")

def check_allowed(user_id: int) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    return user_id in config.ALLOWED_USER_IDS


# ── Keyboards ─────────────────────────────────────────────────────────────────

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Balances"),      KeyboardButton(text="📊 Portfolio")],
        [KeyboardButton(text="➕ Add Wallet"),    KeyboardButton(text="➕ Add Exchange")],
        [KeyboardButton(text="📋 My Wallets"),    KeyboardButton(text="📋 My Exchanges")],
        [KeyboardButton(text="🗑 Remove Wallet"),  KeyboardButton(text="🗑 Remove Exchange")],
        [KeyboardButton(text="⚙️ Settings"),      KeyboardButton(text="❓ Help")],
    ],
    resize_keyboard=True,
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💵 Min Balance Filter"),  KeyboardButton(text="🔔 Alert Threshold")],
        [KeyboardButton(text="💸 Min Alert Amount"),    KeyboardButton(text="🔕 Toggle Notifications")],
        [KeyboardButton(text="📊 View My Settings"),    KeyboardButton(text="🔙 Back to Menu")],
    ],
    resize_keyboard=True,
)

def cancel_kb(back_to_settings: bool = False) -> ReplyKeyboardMarkup:
    btn = "🔙 Back to Settings" if back_to_settings else "❌ Cancel"
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=btn)]], resize_keyboard=True)


# ── FSM states ────────────────────────────────────────────────────────────────

class AddWallet(StatesGroup):
    entering_address = State()
    choosing_chains  = State()
    entering_label   = State()

class AddChainToWallet(StatesGroup):
    choosing_wallet  = State()
    choosing_chains  = State()

class AddExchange(StatesGroup):
    choosing_exchange = State()
    entering_key      = State()
    entering_secret   = State()
    entering_password = State()
    entering_label    = State()

class RemovingWallet(StatesGroup):
    choosing_wallet  = State()
    choosing_action  = State()

class RemovingExchange(StatesGroup):
    waiting_for_choice = State()

class Settings(StatesGroup):
    min_balance     = State()
    alert_threshold = State()
    min_alert_usd   = State()

_EVM_LABEL_TO_CHAIN = {
    f"{CHAIN_CONFIG[c]['emoji']} {CHAIN_CONFIG[c]['label']}": c
    for c in EVM_CHAINS
}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _load_settings(telegram_id: int) -> UserSettings | None:
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, telegram_id)
        if not user:
            return None
        return await get_or_create_settings(db, user)

async def _save_setting(telegram_id: int, **kwargs) -> UserSettings | None:
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, telegram_id)
        if not user:
            return None
        s = await get_or_create_settings(db, user)
        for key, value in kwargs.items():
            setattr(s, key, value)
        await db.commit()
        await db.refresh(s)
        return s

def _settings_summary(s: UserSettings) -> str:
    notif = "ON 🔔" if s.notifications_enabled else "OFF 🔕"
    return (
        f"⚙️ *Your Settings*\n\n"
        f"💵 *Min balance:* {fmt_usd(s.min_balance_usd)}\n"
        f"    _Hides assets below this in /balances \\& /portfolio_\n\n"
        f"🔔 *Alert threshold:* {md(f'{s.notify_threshold_pct:.1f}')}%\n"
        f"    _Notifies when balance changes by this %_\n\n"
        f"💸 *Min alert amount:* {fmt_usd(s.notify_min_usd)}\n"
        f"    _Alerts only if move is worth at least this_\n\n"
        f"📳 *Notifications:* {md(notif)}\n"
    )

def _chain_tags(chains: list[str]) -> str:
    return "".join(CHAIN_CONFIG.get(c, {}).get("emoji", "") for c in sorted(chains))

def _chain_names(chains: list[str]) -> str:
    return ", ".join(CHAIN_CONFIG.get(c, {}).get("label", c) for c in sorted(chains))

def _build_wallet_groups(wallets: list) -> list[dict]:
    raw = group_wallets_by_address(wallets)
    return [{"address": addr, **info} for addr, info in raw.items()]

def _evm_chains_not_tracked(tracked_chains: list[str]) -> list[str]:
    return [c for c in EVM_CHAINS if c not in tracked_chains]

async def _get_user_portfolio(telegram_id: int) -> tuple[list, list, float]:
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, telegram_id)
        if not user:
            return [], [], 1.0
        wallets = [{"chain": w.chain, "address": w.address, "label": w.label} for w in user.wallets]
        exchanges = []
        for e in user.exchanges:
            try:
                exchanges.append({
                    "exchange_id":  e.exchange_id,
                    "api_key":      decrypt(e.api_key),
                    "api_secret":   decrypt(e.api_secret),
                    "api_password": decrypt(e.api_password) if e.api_password else None,
                    "label":        e.label,
                })
            except Exception:
                pass
        s = await get_or_create_settings(db, user)
        return wallets, exchanges, s.min_balance_usd


# ── FIX 4: Rate limit guard ───────────────────────────────────────────────────

def rate_limited(func):
    """Reject if user exceeds RATE_LIMIT_PER_MINUTE calls to expensive commands."""
    async def wrapper(message: Message, state: FSMContext):
        if is_rate_limited(message.from_user.id, config.RATE_LIMIT_PER_MINUTE):
            wait = rate_limit_wait_seconds(message.from_user.id)
            await message.answer(
                f"⏳ Too many requests\\. Try again in *{wait}s*\\.",
                parse_mode="MarkdownV2",
            )
            return
        return await func(message, state)
    wrapper.__name__ = func.__name__
    return wrapper


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not check_allowed(message.from_user.id):
        return await message.answer("⛔ You are not authorised to use this bot.")
    await state.clear()
    async with AsyncSessionLocal() as db:
        user = await create_or_update_user(
            db, telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        await get_or_create_settings(db, user)
    name = md(message.from_user.first_name or "there")
    await message.answer(
        f"👋 Welcome, *{name}*\\!\n\n"
        "I track your crypto balances across wallets and exchanges\\.\n\n"
        "  • *➕ Add Wallet* — paste any wallet address\n"
        "  • *➕ Add Exchange* — connect Binance, Bybit, OKX, etc\\.\n"
        "  • *💰 Balances* — live balances with USD values\n"
        "  • *⚙️ Settings* — filter dust, set alert thresholds\n\n"
        "_API keys are encrypted at rest\\. Use /cancel anytime to go back\\._",
        parse_mode="MarkdownV2",
        reply_markup=MAIN_MENU,
    )


# ── FIX 3: /cancel — universal FSM escape ────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer(
            "❌ Cancelled\\. Back to main menu\\.",
            parse_mode="MarkdownV2",
            reply_markup=MAIN_MENU,
        )
    else:
        await message.answer("Nothing to cancel\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── /help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
@router.message(F.text == "❓ Help")
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "*Commands*\n\n"
        "💰 /balances — Live wallet \\& exchange balances\n"
        "📊 /portfolio — Total USD value by asset\n"
        "🔄 /refresh — Force fresh data \\(bypasses cache\\)\n"
        "📋 /mywallets — View wallets grouped by address\n"
        "📋 /myexchanges — List connected exchanges\n"
        "➕ /addwallet — Track a blockchain wallet\n"
        "➕ /addchain — Add chains to existing wallet\n"
        "🗑 /removewallet — Remove a wallet or chain\n"
        "➕ /addexchange — Connect an exchange\n"
        "🗑 /removeexchange — Disconnect an exchange\n"
        "⚙️ /settings — Filters, alert thresholds\n"
        "❌ /cancel — Cancel any ongoing action\n\n"
        "*Chains:* ETH, BNB, Polygon, Arbitrum, Optimism, Base, Avalanche, Solana, Bitcoin\n"
        "*Exchanges:* Binance, Coinbase, Kraken, KuCoin, Bybit, OKX, Bitget, Gate, MEXC, HTX",
        parse_mode="MarkdownV2",
        reply_markup=MAIN_MENU,
    )


# ── FIX 7: /refresh ──────────────────────────────────────────────────────────

@router.message(Command("refresh"))
async def cmd_refresh(message: Message, state: FSMContext):
    await state.clear()
    # Clear ALL cached balance entries so next fetch is live
    from services.cache import _CACHE
    keys_to_remove = [k for k in list(_CACHE.keys())
                      if k.startswith("wallet:") or k.startswith("exchange:")]
    for k in keys_to_remove:
        _CACHE.pop(k, None)
    count = len(keys_to_remove)
    await message.answer(
        f"🔄 Cleared {count} cached entr{'y' if count==1 else 'ies'}\\. "
        f"Use *💰 Balances* to fetch fresh data\\.",
        parse_mode="MarkdownV2",
        reply_markup=MAIN_MENU,
    )


# ── /balances — FIX 1 (cache), FIX 2 (ERC-20), FIX 4 (rate limit) ───────────

@router.message(Command("balances"))
@router.message(F.text == "💰 Balances")
@rate_limited
async def cmd_balances(message: Message, state: FSMContext):
    await state.clear()
    wallets, exchanges, min_usd = await _get_user_portfolio(message.from_user.id)

    if not wallets and not exchanges:
        return await message.answer(
            "No wallets or exchanges added yet\\.\n"
            "Use *➕ Add Wallet* or *➕ Add Exchange* to get started\\.",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )

    filter_note = f" _\\(hiding below {fmt_usd(min_usd)}\\)_" if min_usd > 0 else ""
    status_msg  = await message.answer(
        f"⏳ Fetching balances{filter_note}\\.\\.\\.", parse_mode="MarkdownV2"
    )

    try:
        lines, hidden_count = [], 0
        has_stale = False

        # ── Wallets ──────────────────────────────────────────────────────
        if wallets:
            try:
                wallet_results = await asyncio.wait_for(
                    get_all_wallet_balances(wallets), timeout=25.0
                )
            except asyncio.TimeoutError:
                wallet_results = [
                    {**w, "balance": 0.0,
                     "symbol": CHAIN_CONFIG.get(w["chain"], {}).get("symbol", "?"),
                     "error": "Timed out — try /refresh", "tokens": []}
                    for w in wallets
                ]

            # Collect all symbols including ERC-20 tokens
            all_syms = list({r["symbol"] for r in wallet_results if not r.get("error")})
            for r in wallet_results:
                for t in r.get("tokens", []):
                    if t["symbol"] not in all_syms:
                        all_syms.append(t["symbol"])
            prices = await get_usd_prices(all_syms) if all_syms else {}

            addr_groups: dict[str, list] = defaultdict(list)
            for r in wallet_results:
                addr_groups[r["address"]].append(r)

            lines.append("🏦 *Wallets*")
            for addr, results in addr_groups.items():
                sample_label = next((r["label"] for r in results if r.get("label")), None)
                all_chains   = [r["chain"] for r in results]
                tags = _chain_tags(all_chains)
                lbl  = md(sample_label) if sample_label else md(addr[:6] + "…" + addr[-4:])
                lines.append(f"\n  {tags} {bold(lbl)}")

                for r in results:
                    cfg   = CHAIN_CONFIG.get(r["chain"], {})
                    emoji = cfg.get("emoji", "")
                    cname = md(cfg.get("label", r["chain"]))

                    if r.get("error"):
                        stale = f" _{md('(cached ' + str(r['_stale']) + 's ago)')}_" if r.get("_stale") else ""
                        lines.append(f"    {emoji} {cname}: ❌ {md(r['error'][:60])}{stale}")
                    else:
                        if r.get("_stale"):
                            has_stale = True
                        bal = r["balance"]
                        sym = r["symbol"]
                        usd = bal * prices.get(sym, 0)
                        stale = f" _\\(cached\\)_" if r.get("_stale") else ""
                        if usd < min_usd and bal > 0 and min_usd > 0:
                            hidden_count += 1
                        else:
                            usd_str = f" \\({fmt_usd(usd)}\\)" if usd >= 0.01 else ""
                            lines.append(f"    {emoji} {cname}: `{fmt_num(bal)} {md(sym)}`{usd_str}{stale}")

                        # FIX 2: ERC-20 tokens
                        for tok in r.get("tokens", []):
                            t_sym = tok["symbol"]
                            t_bal = tok["balance"]
                            t_usd = t_bal * prices.get(t_sym, 0)
                            if t_usd < min_usd and min_usd > 0:
                                hidden_count += 1
                                continue
                            t_usd_str = f" \\({fmt_usd(t_usd)}\\)" if t_usd >= 0.01 else ""
                            lines.append(f"       `{md(t_sym)}` {fmt_num(t_bal)}{t_usd_str}")

        # ── Exchanges ─────────────────────────────────────────────────────
        if exchanges:
            if lines:
                lines.append("")
            lines.append("💱 *Exchanges*")
            try:
                exchange_results = await asyncio.wait_for(
                    get_all_exchange_balances(exchanges), timeout=25.0
                )
            except asyncio.TimeoutError:
                exchange_results = [
                    {"exchange": e["exchange_id"], "balances": [],
                     "error": "Timed out — try /refresh"}
                    for e in exchanges
                ]

            for i, r in enumerate(exchange_results):
                lbl = md(exchanges[i].get("label") or r["exchange"].upper())
                stale = f" _\\(cached {r['_stale']}s ago\\)_" if r.get("_stale") else ""
                if r.get("_stale"):
                    has_stale = True
                if r.get("error"):
                    lines.append(f"  {bold(lbl)}\n    ❌ {md(r['error'][:80])}")
                    continue
                if not r["balances"]:
                    lines.append(f"  {bold(lbl)}\n    _No balances found_")
                    continue
                lines.append(f"\n  {bold(lbl)}{stale}")
                syms   = [b["asset"] for b in r["balances"]]
                prices = await get_usd_prices(syms) if syms else {}
                ex_hidden = 0
                for b in r["balances"]:
                    sym, tot = b["asset"], b["total"]
                    usd = tot * prices.get(sym, 0)
                    if usd < min_usd and tot > 0 and min_usd > 0:
                        ex_hidden += 1; hidden_count += 1; continue
                    usd_str    = f" \\({fmt_usd(usd)}\\)" if usd >= 0.01 else ""
                    locked_str = f" \\[locked: {fmt_num(b.get('locked',0),4)}\\]" if b.get("locked",0) > 0 else ""
                    lines.append(f"    • `{md(sym)}` {fmt_num(tot)}{locked_str}{usd_str}")
                if ex_hidden:
                    lines.append(f"    _\\+ {ex_hidden} asset\\(s\\) below {fmt_usd(min_usd)} hidden_")

        # Footer
        footer = []
        if hidden_count > 0:
            footer.append(f"_⚙️ {hidden_count} asset\\(s\\) below {fmt_usd(min_usd)} hidden — /settings_")
        if has_stale:
            footer.append("_Some data served from cache — use /refresh for live data_")
        if footer:
            lines.append("\n" + "\n".join(footer))

        text = "\n".join(lines) if lines else "No balances to show\\."
        if len(text) > 4000:
            text = text[:4000] + "\n\n_\\.\\.\\. use /portfolio for summary_"

        await status_msg.delete()
        await message.answer(text, parse_mode="MarkdownV2", reply_markup=MAIN_MENU)

    except Exception as e:
        log.exception("Balances error")
        await status_msg.delete()
        await message.answer(
            f"❌ Error fetching balances: {md(str(e)[:120])}",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )


# ── /portfolio ─────────────────────────────────────────────────────────────

@router.message(Command("portfolio"))
@router.message(F.text == "📊 Portfolio")
@rate_limited
async def cmd_portfolio(message: Message, state: FSMContext):
    await state.clear()
    wallets, exchanges, min_usd = await _get_user_portfolio(message.from_user.id)

    if not wallets and not exchanges:
        return await message.answer(
            "No wallets or exchanges added yet\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU
        )

    filter_note = f" _\\(hiding below {fmt_usd(min_usd)}\\)_" if min_usd > 0 else ""
    status_msg  = await message.answer(
        f"⏳ Calculating portfolio{filter_note}\\.\\.\\.", parse_mode="MarkdownV2"
    )

    try:
        totals: dict[str, float] = {}
        has_stale = False

        if wallets:
            try:
                results = await asyncio.wait_for(get_all_wallet_balances(wallets), timeout=25.0)
            except asyncio.TimeoutError:
                results = []
            for r in results:
                if r.get("_stale"):
                    has_stale = True
                if not r.get("error") and r["balance"] > 0:
                    totals[r["symbol"]] = totals.get(r["symbol"], 0) + r["balance"]
                for t in r.get("tokens", []):
                    if t["balance"] > 0:
                        totals[t["symbol"]] = totals.get(t["symbol"], 0) + t["balance"]

        if exchanges:
            try:
                results = await asyncio.wait_for(get_all_exchange_balances(exchanges), timeout=25.0)
            except asyncio.TimeoutError:
                results = []
            for r in results:
                if r.get("_stale"):
                    has_stale = True
                for b in r.get("balances", []):
                    totals[b["asset"]] = totals.get(b["asset"], 0) + b["total"]

        if not totals:
            await status_msg.delete()
            return await message.answer(
                "No balances found\\. Try /refresh if this seems wrong\\.",
                parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
            )

        prices      = await get_usd_prices(list(totals.keys()))
        rows        = [(sym, amt, amt * prices.get(sym, 0)) for sym, amt in totals.items()]
        grand_total = sum(r[2] for r in rows)
        rows.sort(key=lambda x: x[2], reverse=True)

        visible, hidden_count, hidden_usd = [], 0, 0.0
        for sym, amt, usd in rows:
            if usd < min_usd and min_usd > 0:
                hidden_count += 1; hidden_usd += usd
            else:
                visible.append((sym, amt, usd))

        lines = ["📊 *Portfolio summary*\n"]
        for sym, amt, usd in visible:
            pct    = (usd / grand_total * 100) if grand_total else 0
            filled = int(pct / 5)
            bar    = "▓" * filled + "░" * (20 - filled)
            lines.append(
                f"`{md(sym):<6}` {fmt_num(amt, 4)}\n"
                f"  {fmt_usd(usd)} \\({md(f'{pct:.1f}')}%\\)\n"
                f"  `{bar}`\n"
            )

        if hidden_count:
            lines.append(
                f"_\\+ {hidden_count} asset\\(s\\) worth {fmt_usd(hidden_usd)} hidden "
                f"\\(below {fmt_usd(min_usd)} filter\\)_\n"
            )

        lines.append(f"\n💵 *Total: {fmt_usd(grand_total)} USD*")
        if has_stale:
            lines.append("_Some data from cache — use /refresh for live data_")
        if min_usd > 0:
            lines.append("_⚙️ Adjust filter in /settings_")

        await status_msg.delete()
        await message.answer("\n".join(lines), parse_mode="MarkdownV2", reply_markup=MAIN_MENU)

    except Exception as e:
        log.exception("Portfolio error")
        await status_msg.delete()
        await message.answer(
            f"❌ Error: {md(str(e)[:120])}", parse_mode="MarkdownV2", reply_markup=MAIN_MENU
        )


# ── /mywallets ────────────────────────────────────────────────────────────────

@router.message(Command("mywallets"))
@router.message(F.text == "📋 My Wallets")
async def cmd_my_wallets(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user    = await get_user_by_telegram_id(db, message.from_user.id)
        wallets = list(user.wallets) if user else []

    if not wallets:
        return await message.answer(
            "No wallets tracked yet\\. Use *➕ Add Wallet*\\.",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )

    groups = _build_wallet_groups(wallets)
    lines  = [f"📋 *Your wallets* \\({len(groups)} address{'es' if len(groups)!=1 else ''}\\)\n"]
    for i, g in enumerate(groups, 1):
        lbl     = md(g["label"]) if g["label"] else "_no label_"
        chains  = g["chains"]
        tags    = _chain_tags(chains)
        names   = md(_chain_names(chains))
        short   = md(g["address"][:6] + "…" + g["address"][-4:])
        missing = _evm_chains_not_tracked(chains) if any(
            CHAIN_CONFIG.get(c, {}).get("type") == "evm" for c in chains
        ) else []
        add_hint = f"\n   _\\+ {len(missing)} more EVM chain{'s' if len(missing)!=1 else ''} available_" if missing else ""
        lines.append(f"{i}\\. {lbl}\n   `{short}`\n   {tags} {names}{add_hint}")

    lines.append("\n_Use /addchain to track more chains on an existing wallet_")
    await message.answer("\n\n".join(lines), parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── /myexchanges ──────────────────────────────────────────────────────────────

@router.message(Command("myexchanges"))
@router.message(F.text == "📋 My Exchanges")
async def cmd_my_exchanges(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user      = await get_user_by_telegram_id(db, message.from_user.id)
        exchanges = list(user.exchanges) if user else []

    if not exchanges:
        return await message.answer(
            "No exchanges connected\\. Use *➕ Add Exchange*\\.",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )

    lines = [f"📋 *Your exchanges* \\({len(exchanges)}\\)\n"]
    for i, e in enumerate(exchanges, 1):
        lbl   = md(e.label) if e.label else "_no label_"
        added = md(e.added_at.strftime("%Y-%m-%d") if e.added_at else "unknown")
        pw    = " \\+ passphrase" if e.api_password else ""
        lines.append(f"{i}\\. {bold(e.exchange_id.upper())}\n   {lbl}\n   Added: {added}{md(pw)}")
    lines.append("\n_API keys stored encrypted\\._")
    await message.answer("\n\n".join(lines), parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── /addwallet ────────────────────────────────────────────────────────────────

@router.message(Command("addwallet"))
@router.message(F.text == "➕ Add Wallet")
async def cmd_add_wallet_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddWallet.entering_address)
    await message.answer(
        "📋 *Add a wallet*\n\n"
        "Paste your wallet address:\n"
        "  • `0x…` — ETH, BNB, Polygon, Arbitrum, Optimism, Base, Avalanche\n"
        "  • Solana address \\(32–44 chars\\)\n"
        "  • Bitcoin address \\(starts with 1, 3, or bc1\\)\n\n"
        "_One 0x address covers all EVM chains\\. Use /cancel to go back\\._",
        parse_mode="MarkdownV2",
        reply_markup=cancel_kb(),
    )

@router.message(AddWallet.entering_address)
async def add_wallet_address(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)

    address   = message.text.strip()
    addr_type = detect_address_type(address)

    if len(address) < 26 or addr_type == "unknown":
        return await message.answer(
            "⚠️ Could not recognise this address\\. Try again or /cancel\\.",
            parse_mode="MarkdownV2",
        )

    if addr_type == "evm":
        async with AsyncSessionLocal() as db:
            user    = await get_user_by_telegram_id(db, message.from_user.id)
            already = [w.chain for w in user.wallets if w.address.lower() == address.lower()] if user else []

        await state.update_data(address=address, addr_type="evm", selected_chains=[], already_tracked=already)
        await state.set_state(AddWallet.choosing_chains)
        await _show_evm_chain_picker(message, address, already_tracked=already, selected=[])

    elif addr_type == "solana":
        await state.update_data(address=address, addr_type="solana", chain="solana")
        await state.set_state(AddWallet.entering_label)
        await message.answer(
            f"◎ *Solana* address detected\\.\n`{md(address[:16])}…`\n\nGive it a label, or /skip:",
            parse_mode="MarkdownV2", reply_markup=cancel_kb(),
        )
    elif addr_type == "bitcoin":
        await state.update_data(address=address, addr_type="bitcoin", chain="bitcoin")
        await state.set_state(AddWallet.entering_label)
        await message.answer(
            f"₿ *Bitcoin* address detected\\.\n`{md(address[:16])}…`\n\nGive it a label, or /skip:",
            parse_mode="MarkdownV2", reply_markup=cancel_kb(),
        )

async def _show_evm_chain_picker(message: Message, address: str, already_tracked: list, selected: list):
    buttons = []
    for c in EVM_CHAINS:
        cfg   = CHAIN_CONFIG[c]
        label = f"{cfg['emoji']} {cfg['label']}"
        if c in already_tracked: label += " ✅"
        elif c in selected:       label += " 🔘"
        buttons.append([KeyboardButton(text=label)])
    buttons += [
        [KeyboardButton(text="🌐 All new EVM chains")],
        [KeyboardButton(text="✅ Done — save selected")],
        [KeyboardButton(text="❌ Cancel")],
    ]
    already_note  = f"\n_Already tracking: {md(_chain_names(already_tracked))}_" if already_tracked else ""
    selected_note = md(_chain_names(selected)) if selected else "none"
    await message.answer(
        f"EVM address: `{md(address[:6])}…{md(address[-4:])}`{already_note}\n"
        f"Selected: {selected_note}\n_Tap to toggle, then Done\\. Use /cancel to abort\\._",
        parse_mode="MarkdownV2",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )

@router.message(AddWallet.choosing_chains)
async def add_wallet_choose_chains(message: Message, state: FSMContext):
    text     = message.text
    data     = await state.get_data()
    selected: list = data.get("selected_chains", [])
    already: list  = data.get("already_tracked", [])
    address: str   = data["address"]

    if text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)

    if text == "🌐 All new EVM chains":
        selected = [c for c in EVM_CHAINS if c not in already]
        if not selected:
            return await message.answer("All EVM chains already tracked\\.", parse_mode="MarkdownV2")
        await state.update_data(selected_chains=selected)
        await state.set_state(AddWallet.entering_label)
        return await message.answer(
            f"✅ All {len(selected)} new EVM chains selected\\.\n\nGive a label, or /skip:",
            parse_mode="MarkdownV2", reply_markup=cancel_kb(),
        )

    if text == "✅ Done — save selected":
        if not selected:
            return await message.answer("Select at least one chain\\.", parse_mode="MarkdownV2")
        await state.update_data(selected_chains=selected)
        await state.set_state(AddWallet.entering_label)
        return await message.answer(
            f"✅ {md(_chain_names(selected))} selected\\.\n\nGive a label, or /skip:",
            parse_mode="MarkdownV2", reply_markup=cancel_kb(),
        )

    clean = text.replace(" ✅", "").replace(" 🔘", "").strip()
    chain = _EVM_LABEL_TO_CHAIN.get(clean)
    if chain:
        if chain in already:
            return await message.answer(f"{CHAIN_CONFIG[chain]['emoji']} Already tracked\\.", parse_mode="MarkdownV2")
        if chain in selected: selected.remove(chain)
        else: selected.append(chain)
        await state.update_data(selected_chains=selected)
        await _show_evm_chain_picker(message, address, already_tracked=already, selected=selected)
    else:
        await message.answer("Please use the buttons\\. Use /cancel to abort\\.", parse_mode="MarkdownV2")

@router.message(AddWallet.entering_label)
async def add_wallet_label(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    label     = None if message.text.strip() in ("/skip", "skip") else message.text.strip()[:64]
    data      = await state.get_data()
    addr_type = data["addr_type"]
    address   = data["address"]
    chains    = data.get("selected_chains", []) if addr_type == "evm" else [data["chain"]]
    added, skipped = [], []
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, message.from_user.id)
        if not user:
            await state.clear()
            return await message.answer("Please /start first\\.", parse_mode="MarkdownV2")
        for chain in chains:
            if await get_wallet_exists(db, user.id, chain, address):
                skipped.append(CHAIN_CONFIG[chain]["label"])
            else:
                db.add(Wallet(user_id=user.id, chain=chain, address=address, label=label))
                added.append(CHAIN_CONFIG[chain]["label"])
        await db.commit()
    await state.clear()
    parts = []
    if added:   parts.append(f"✅ Added on: {md(', '.join(added))}")
    if skipped: parts.append(f"⚠️ Already tracked: {md(', '.join(skipped))}")
    lbl_str = md(label) if label else "_no label_"
    await message.answer(
        f"`{md(address[:6])}…{md(address[-4:])}`\n" + "\n".join(parts) + f"\nLabel: {lbl_str}",
        parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
    )


# ── /addchain ─────────────────────────────────────────────────────────────────

@router.message(Command("addchain"))
@router.message(F.text == "➕ Add Chain")
async def cmd_add_chain_start(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user    = await get_user_by_telegram_id(db, message.from_user.id)
        wallets = list(user.wallets) if user else []
    if not wallets:
        return await message.answer("No wallets yet\\. Use *➕ Add Wallet* first\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    groups     = _build_wallet_groups(wallets)
    expandable = [g for g in groups
                  if any(CHAIN_CONFIG.get(c, {}).get("type") == "evm" for c in g["chains"])
                  and _evm_chains_not_tracked(g["chains"])]
    if not expandable:
        return await message.answer(
            "All EVM wallets already track all chains\\. Use *➕ Add Wallet* for a new address\\.",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )
    lines, buttons = ["Choose a wallet to add chains to:\n"], []
    for i, g in enumerate(expandable, 1):
        lbl   = g["label"] or (g["address"][:6] + "…" + g["address"][-4:])
        missing_count = len(_evm_chains_not_tracked(g["chains"]))
        lines.append(f"{i}\\. {_chain_tags(g['chains'])} {md(lbl)} _\\({missing_count} available\\)_")
        buttons.append([KeyboardButton(text=str(i))])
    buttons.append([KeyboardButton(text="❌ Cancel")])
    await state.set_state(AddChainToWallet.choosing_wallet)
    await state.update_data(expandable_groups=[(g["address"], g["chains"], g["label"]) for g in expandable])
    await message.answer("\n".join(lines), parse_mode="MarkdownV2", reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))

@router.message(AddChainToWallet.choosing_wallet)
async def add_chain_pick_wallet(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    data = await state.get_data()
    try:
        idx = int(message.text) - 1
        address, tracked, label = data["expandable_groups"][idx]
    except (ValueError, IndexError):
        return await message.answer("Invalid choice\\. Use the buttons\\.", parse_mode="MarkdownV2")
    await state.update_data(target_address=address, already_tracked=tracked, wallet_label=label, selected_chains=[])
    await state.set_state(AddChainToWallet.choosing_chains)
    await _show_evm_chain_picker(message, address, already_tracked=tracked, selected=[])

@router.message(AddChainToWallet.choosing_chains)
async def add_chain_pick_chains(message: Message, state: FSMContext):
    text     = message.text
    data     = await state.get_data()
    selected: list = data.get("selected_chains", [])
    already: list  = data["already_tracked"]
    address: str   = data["target_address"]
    label          = data.get("wallet_label")

    if text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    if text == "🌐 All new EVM chains":
        selected = [c for c in EVM_CHAINS if c not in already]
        if not selected:
            return await message.answer("All chains already tracked\\.", parse_mode="MarkdownV2")
        await _save_new_chains(message, state, address, selected, label)
        return
    if text == "✅ Done — save selected":
        if not selected:
            return await message.answer("Select at least one chain\\.", parse_mode="MarkdownV2")
        await _save_new_chains(message, state, address, selected, label)
        return
    clean = text.replace(" ✅", "").replace(" 🔘", "").strip()
    chain = _EVM_LABEL_TO_CHAIN.get(clean)
    if chain:
        if chain in already:
            return await message.answer(f"{CHAIN_CONFIG[chain]['emoji']} Already tracked\\.", parse_mode="MarkdownV2")
        if chain in selected: selected.remove(chain)
        else: selected.append(chain)
        await state.update_data(selected_chains=selected)
        await _show_evm_chain_picker(message, address, already_tracked=already, selected=selected)
    else:
        await message.answer("Please use the buttons\\.", parse_mode="MarkdownV2")

async def _save_new_chains(message: Message, state: FSMContext, address: str, chains: list, label):
    added = []
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, message.from_user.id)
        if not user:
            await state.clear()
            return await message.answer("Please /start first\\.", parse_mode="MarkdownV2")
        for chain in chains:
            if not await get_wallet_exists(db, user.id, chain, address):
                db.add(Wallet(user_id=user.id, chain=chain, address=address, label=label))
                added.append(CHAIN_CONFIG[chain]["label"])
        await db.commit()
    await state.clear()
    short = md(address[:6] + "…" + address[-4:])
    if added:
        await message.answer(f"✅ Added {md(', '.join(added))} to `{short}`", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    else:
        await message.answer("No new chains added \\(all already tracked\\)\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── /removewallet ─────────────────────────────────────────────────────────────

@router.message(Command("removewallet"))
@router.message(F.text == "🗑 Remove Wallet")
async def cmd_remove_wallet(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user    = await get_user_by_telegram_id(db, message.from_user.id)
        wallets = list(user.wallets) if user else []
    if not wallets:
        return await message.answer("You have no wallets to remove\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    groups  = _build_wallet_groups(wallets)
    lines   = ["Choose a wallet to remove:\n"]
    buttons = []
    for i, g in enumerate(groups, 1):
        lbl = g["label"] or (g["address"][:6] + "…" + g["address"][-4:])
        lines.append(f"{i}\\. {_chain_tags(g['chains'])} {md(lbl)}\n   {md(_chain_names(g['chains']))}")
        buttons.append([KeyboardButton(text=str(i))])
    buttons.append([KeyboardButton(text="❌ Cancel")])
    await state.set_state(RemovingWallet.choosing_wallet)
    await state.update_data(groups=[(g["address"], g["chains"], g["ids"], g["label"]) for g in groups])
    await message.answer("\n".join(lines), parse_mode="MarkdownV2", reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))

@router.message(RemovingWallet.choosing_wallet)
async def remove_wallet_pick(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    data = await state.get_data()
    try:
        idx = int(message.text) - 1
        address, chains, ids, label = data["groups"][idx]
    except (ValueError, IndexError):
        return await message.answer("Invalid choice\\. Use the buttons\\.", parse_mode="MarkdownV2")
    await state.update_data(target_address=address, target_chains=chains, target_ids=ids)
    await state.set_state(RemovingWallet.choosing_action)
    lbl_str = md(label) if label else md(address[:6] + "…" + address[-4:])
    if len(chains) == 1:
        buttons = [[KeyboardButton(text="🗑 Yes, remove it")], [KeyboardButton(text="❌ Cancel")]]
        await message.answer(
            f"Remove {bold(lbl_str)} on {md(CHAIN_CONFIG.get(chains[0], {}).get('label', chains[0]))}?",
            parse_mode="MarkdownV2",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
    else:
        chain_buttons = [[KeyboardButton(text=f"Remove {CHAIN_CONFIG[c]['emoji']} {CHAIN_CONFIG[c]['label']} only")] for c in chains]
        chain_buttons += [[KeyboardButton(text="🗑 Remove ALL chains")], [KeyboardButton(text="❌ Cancel")]]
        await message.answer(
            f"{_chain_tags(chains)} {bold(lbl_str)}\n{md(_chain_names(chains))}\n\nRemove which chain\\(s\\)?",
            parse_mode="MarkdownV2",
            reply_markup=ReplyKeyboardMarkup(keyboard=chain_buttons, resize_keyboard=True),
        )

@router.message(RemovingWallet.choosing_action)
async def remove_wallet_action(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    data   = await state.get_data()
    chains = data["target_chains"]
    ids    = data["target_ids"]
    chains_to_remove = []
    if message.text in ("🗑 Yes, remove it", "🗑 Remove ALL chains"):
        chains_to_remove = list(chains)
    else:
        for c in chains:
            cfg = CHAIN_CONFIG.get(c, {})
            if message.text == f"Remove {cfg.get('emoji','')} {cfg.get('label', c)} only":
                chains_to_remove = [c]
                break
    if not chains_to_remove:
        return await message.answer("Invalid choice\\. Use the buttons\\.", parse_mode="MarkdownV2")
    async with AsyncSessionLocal() as db:
        result  = await db.execute(select(Wallet).where(Wallet.id.in_(ids)))
        rows    = result.scalars().all()
        removed = []
        for w in rows:
            if w.chain in chains_to_remove:
                await db.delete(w)
                removed.append(CHAIN_CONFIG.get(w.chain, {}).get("label", w.chain))
        await db.commit()
    await state.clear()
    await message.answer(
        f"✅ Removed: {md(', '.join(removed))}", parse_mode="MarkdownV2", reply_markup=MAIN_MENU
    )


# ── /addexchange ──────────────────────────────────────────────────────────────

@router.message(Command("addexchange"))
@router.message(F.text == "➕ Add Exchange")
async def cmd_add_exchange_start(message: Message, state: FSMContext):
    await state.clear()
    ex_list = list(SUPPORTED_EXCHANGES.keys())
    rows    = [ex_list[i:i+3] for i in range(0, len(ex_list), 3)]
    buttons = [[KeyboardButton(text=e.capitalize()) for e in row] for row in rows]
    buttons.append([KeyboardButton(text="❌ Cancel")])
    await state.set_state(AddExchange.choosing_exchange)
    await message.answer(
        "Which exchange do you want to connect?\n\n"
        "_I'll show step\\-by\\-step how to create a read\\-only API key\\._",
        parse_mode="MarkdownV2",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )

@router.message(AddExchange.choosing_exchange)
async def add_exchange_choice(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    exchange_id = message.text.lower().strip()
    if exchange_id not in SUPPORTED_EXCHANGES:
        return await message.answer("Please choose one of the buttons\\.", parse_mode="MarkdownV2")
    await state.update_data(exchange_id=exchange_id)
    await state.set_state(AddExchange.entering_key)
    guide_url, steps = EXCHANGE_GUIDES.get(exchange_id, ("", "Create a read-only API key on the exchange website."))
    await message.answer(
        f"🔑 *{md(exchange_id.upper())} read\\-only API key setup*\n\n{md(steps)}\n\n"
        f"🔗 [Open API settings]({guide_url})\n\n"
        f"⚠️ *READ ONLY* — never enable withdrawals or trading\\.\n\n"
        f"Paste your *API Key*:",
        parse_mode="MarkdownV2", reply_markup=ReplyKeyboardRemove(),
    )

@router.message(AddExchange.entering_key)
async def add_exchange_key(message: Message, state: FSMContext):
    await state.update_data(api_key=message.text.strip())
    try: await message.delete()
    except Exception: pass
    await state.set_state(AddExchange.entering_secret)
    await message.answer("✅ Key received \\(deleted from chat\\)\\. Paste your *API Secret*:", parse_mode="MarkdownV2")

@router.message(AddExchange.entering_secret)
async def add_exchange_secret(message: Message, state: FSMContext):
    await state.update_data(api_secret=message.text.strip())
    try: await message.delete()
    except Exception: pass
    data = await state.get_data()
    if data["exchange_id"] in EXCHANGES_NEEDING_PASSWORD:
        await state.set_state(AddExchange.entering_password)
        await message.answer(f"✅ Secret received\\. *{md(data['exchange_id'].upper())}* also needs your *Passphrase*:", parse_mode="MarkdownV2")
    else:
        await state.set_state(AddExchange.entering_label)
        await message.answer("✅ Secret received\\. Give this a label, or /skip:", parse_mode="MarkdownV2")

@router.message(AddExchange.entering_password)
async def add_exchange_password(message: Message, state: FSMContext):
    await state.update_data(api_password=message.text.strip())
    try: await message.delete()
    except Exception: pass
    await state.set_state(AddExchange.entering_label)
    await message.answer("✅ Passphrase received\\. Give this a label, or /skip:", parse_mode="MarkdownV2")

@router.message(AddExchange.entering_label)
async def add_exchange_label(message: Message, state: FSMContext):
    label = None if message.text.strip() in ("/skip", "skip") else message.text.strip()[:64]
    data  = await state.get_data()
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, message.from_user.id)
        if not user:
            await state.clear()
            return await message.answer("Please /start first\\.", parse_mode="MarkdownV2")

        # Check for duplicate exchange connection
        existing = await db.execute(
            select(Exchange).where(
                Exchange.user_id == user.id,
                Exchange.exchange_id == data["exchange_id"],
            )
        )
        if existing.scalar_one_or_none():
            await state.clear()
            return await message.answer(
                f"⚠️ *{md(data['exchange_id'].upper())}* is already connected\\.\n"
                f"Use *🗑 Remove Exchange* first if you want to update the keys\\.",
                parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
            )

        db.add(Exchange(
            user_id=user.id, exchange_id=data["exchange_id"], label=label,
            api_key=encrypt(data["api_key"]), api_secret=encrypt(data["api_secret"]),
            api_password=encrypt(data["api_password"]) if data.get("api_password") else None,
        ))
        await db.commit()
    await state.clear()
    lbl_str = md(label) if label else "_no label_"
    await message.answer(
        f"✅ *{md(data['exchange_id'].upper())}* connected\\! {lbl_str}\n_Keys encrypted at rest\\._",
        parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
    )


# ── /removeexchange ───────────────────────────────────────────────────────────

@router.message(Command("removeexchange"))
@router.message(F.text == "🗑 Remove Exchange")
async def cmd_remove_exchange(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user      = await get_user_by_telegram_id(db, message.from_user.id)
        exchanges = list(user.exchanges) if user else []
    if not exchanges:
        return await message.answer("No exchanges connected\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    lines   = ["Choose an exchange to disconnect:\n"]
    buttons = []
    for i, e in enumerate(exchanges, 1):
        lines.append(f"{i}\\. {md(e.label or e.exchange_id.upper())}")
        buttons.append([KeyboardButton(text=str(i))])
    buttons.append([KeyboardButton(text="❌ Cancel")])
    await state.set_state(RemovingExchange.waiting_for_choice)
    await state.update_data(exchange_ids=[e.id for e in exchanges])
    await message.answer("\n".join(lines), parse_mode="MarkdownV2", reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))

@router.message(RemovingExchange.waiting_for_choice)
async def remove_exchange_choice(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)
    data = await state.get_data()
    try:
        eid = data["exchange_ids"][int(message.text) - 1]
    except (ValueError, IndexError):
        return await message.answer("Invalid choice\\.", parse_mode="MarkdownV2")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Exchange).where(Exchange.id == eid))
        exch   = result.scalar_one_or_none()
        if exch:
            await db.delete(exch)
            await db.commit()
    await state.clear()
    await message.answer("✅ Exchange disconnected\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── /settings ────────────────────────────────────────────────────────────────

@router.message(Command("settings"))
@router.message(F.text == "⚙️ Settings")
async def cmd_settings(message: Message, state: FSMContext):
    await state.clear()
    s = await _load_settings(message.from_user.id)
    if not s:
        return await message.answer("Please /start first\\.", parse_mode="MarkdownV2")
    await message.answer(_settings_summary(s) + "\nChoose a setting to change:", parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU)

@router.message(F.text == "📊 View My Settings")
async def cmd_view_settings(message: Message, state: FSMContext):
    await state.clear()
    s = await _load_settings(message.from_user.id)
    if not s:
        return await message.answer("Please /start first\\.", parse_mode="MarkdownV2")
    await message.answer(_settings_summary(s), parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU)

@router.message(F.text == "🔙 Back to Menu")
async def cmd_back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Main menu:", reply_markup=MAIN_MENU)

@router.message(F.text == "🔙 Back to Settings")
async def cmd_back_to_settings(message: Message, state: FSMContext):
    await state.clear()
    s = await _load_settings(message.from_user.id)
    if not s:
        return await message.answer("Please /start first\\.", parse_mode="MarkdownV2")
    await message.answer(_settings_summary(s) + "\nChoose a setting:", parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU)

@router.message(Command("notifications"))
@router.message(F.text == "🔕 Toggle Notifications")
async def cmd_toggle_notifications(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, message.from_user.id)
        if not user:
            return await message.answer("Please /start first\\.", parse_mode="MarkdownV2")
        s = await get_or_create_settings(db, user)
        s.notifications_enabled = not s.notifications_enabled
        await db.commit()
        status = "ON 🔔" if s.notifications_enabled else "OFF 🔕"
    await message.answer(
        f"Notifications: *{md(status)}*\n_Checks every 5 minutes, alerts on deposits\\/withdrawals\\._",
        parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU,
    )

@router.message(F.text == "💵 Min Balance Filter")
async def settings_min_balance_prompt(message: Message, state: FSMContext):
    s = await _load_settings(message.from_user.id)
    await state.set_state(Settings.min_balance)
    await message.answer(
        f"💵 *Minimum balance to display*\n\nCurrent: {fmt_usd(s.min_balance_usd if s else 1.0)}\n\n"
        "Assets worth less than this are hidden in /balances and /portfolio\\.\n\n"
        "Enter a USD amount \\(e\\.g\\. `1`, `5`, `10`, or `0` to show everything\\):",
        parse_mode="MarkdownV2", reply_markup=cancel_kb(back_to_settings=True),
    )

@router.message(Settings.min_balance)
async def settings_min_balance_save(message: Message, state: FSMContext):
    if message.text in ("🔙 Back to Settings", "❌ Cancel"):
        await state.clear()
        return await cmd_back_to_settings(message, state)
    try:
        value = float(message.text.replace("$", "").strip())
        if value < 0: raise ValueError
    except ValueError:
        return await message.answer("Enter a valid number e\\.g\\. `1`, `5`, `0`\\.", parse_mode="MarkdownV2")
    await _save_setting(message.from_user.id, min_balance_usd=value)
    await state.clear()
    desc = f"Assets below {fmt_usd(value)} will be hidden" if value > 0 else "All assets will be shown"
    await message.answer(f"✅ Min balance set to {fmt_usd(value)}\n_{md(desc)}_", parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU)

@router.message(F.text == "🔔 Alert Threshold")
async def settings_alert_pct_prompt(message: Message, state: FSMContext):
    s = await _load_settings(message.from_user.id)
    await state.set_state(Settings.alert_threshold)
    await message.answer(
        f"🔔 *Alert threshold \\(%\\)*\n\nCurrent: {md(f'{s.notify_threshold_pct:.1f}' if s else '1.0')}%\n\n"
        "Alert fires when a balance changes by at least this %\\.\n\n"
        "`1` sensitive \\| `5` moderate \\| `10` quiet\n\nEnter a %:",
        parse_mode="MarkdownV2", reply_markup=cancel_kb(back_to_settings=True),
    )

@router.message(Settings.alert_threshold)
async def settings_alert_pct_save(message: Message, state: FSMContext):
    if message.text in ("🔙 Back to Settings", "❌ Cancel"):
        await state.clear()
        return await cmd_back_to_settings(message, state)
    try:
        value = float(message.text.replace("%", "").strip())
        if not (0.1 <= value <= 100): raise ValueError
    except ValueError:
        return await message.answer("Enter a number between `0\\.1` and `100`\\.", parse_mode="MarkdownV2")
    await _save_setting(message.from_user.id, notify_threshold_pct=value)
    await state.clear()
    await message.answer(f"✅ Alert threshold: {md(f'{value:.1f}')}%", parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU)

@router.message(F.text == "💸 Min Alert Amount")
async def settings_min_alert_usd_prompt(message: Message, state: FSMContext):
    s = await _load_settings(message.from_user.id)
    await state.set_state(Settings.min_alert_usd)
    await message.answer(
        f"💸 *Min alert amount \\(USD\\)*\n\nCurrent: {fmt_usd(s.notify_min_usd if s else 1.0)}\n\n"
        "Alerts only fire when the change is worth at least this in USD\\.\n\n"
        "`0` any change \\| `1` \\$1\\+ \\| `10` \\$10\\+\n\nEnter a USD amount:",
        parse_mode="MarkdownV2", reply_markup=cancel_kb(back_to_settings=True),
    )

@router.message(Settings.min_alert_usd)
async def settings_min_alert_usd_save(message: Message, state: FSMContext):
    if message.text in ("🔙 Back to Settings", "❌ Cancel"):
        await state.clear()
        return await cmd_back_to_settings(message, state)
    try:
        value = float(message.text.replace("$", "").strip())
        if value < 0: raise ValueError
    except ValueError:
        return await message.answer("Enter a valid amount e\\.g\\. `0`, `1`, `10`\\.", parse_mode="MarkdownV2")
    await _save_setting(message.from_user.id, notify_min_usd=value)
    await state.clear()
    desc = f"Alerts for moves worth ${value:,.2f}+" if value > 0 else "Alerts for any change"
    await message.answer(f"✅ Min alert amount: {fmt_usd(value)}\n_{md(desc)}_", parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU)


# ── FIX 5: DB error recovery + Main ──────────────────────────────────────────

async def _run_health_server(port: int):
    """
    Minimal HTTP server so Render/Railway detect an open port.
    Render Web Service requires a bound port — without this it kills the process.
    GET / or GET /health → 200 {"status":"ok"}
    """
    async def health(request):
        return web.Response(
            text='{"status":"ok","bot":"running"}',
            content_type="application/json",
        )
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health server listening on port {port}")


async def main():
    # ── Database ────────────────────────────────────────────────────────────
    try:
        await init_db()
        log.info("Database tables ready.")
    except Exception as e:
        log.error(
            f"\n\n{'='*60}\n"
            f"DATABASE CONNECTION FAILED\n"
            f"Error: {e}\n\n"
            f"Check DATABASE_URL in .env / Render env vars\n"
            f"  PostgreSQL: postgresql+asyncpg://user:pass@host/db\n"
            f"  SQLite:     sqlite+aiosqlite:///./portfolio.db\n"
            f"{'='*60}\n"
        )
        raise SystemExit(1)

    # ── Scheduler ───────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_and_notify, "interval",
        seconds=config.POLL_INTERVAL_SECONDS,
        args=[bot], id="notify_poll",
        replace_existing=True,
        misfire_grace_time=60,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info(f"Scheduler started — polling every {config.POLL_INTERVAL_SECONDS}s")

    # ── Health server (required for Render Web Service) ─────────────────────
    # Render injects PORT automatically. Default 8080 for local dev.
    port = int(os.environ.get("PORT", "8080"))
    await _run_health_server(port)

    # ── Telegram bot (long-polling) ─────────────────────────────────────────
    log.info("Bot starting… (use /cancel any time to exit a flow)")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())