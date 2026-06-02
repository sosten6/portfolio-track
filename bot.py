"""
bot.py — Crypto Portfolio Tracker Bot (fully async, asyncpg/Neon)
"""
import asyncio
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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot    = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

SUPPORTED_CHAINS = list(CHAIN_CONFIG.keys())

EXCHANGE_GUIDES: dict[str, tuple[str, str]] = {
    "binance":  ("https://www.binance.com/en/my/settings/api-management",
                 "1. Profile → API Management → Create API\n2. Choose System generated\n3. ✅ Enable Read Info ONLY\n4. Complete 2FA → copy API Key and Secret Key"),
    "coinbase": ("https://www.coinbase.com/settings/api",
                 "1. Settings → API → New API Key\n2. Select portfolio\n3. ✅ Permission: view ONLY\n4. Complete 2FA → copy Key and Secret"),
    "kraken":   ("https://www.kraken.com/u/security/api",
                 "1. Settings → API → Add key\n2. ✅ Permission: Query Funds ONLY\n3. Generate Key → copy API Key and Private Key"),
    "kucoin":   ("https://www.kucoin.com/account/api",
                 "1. Account → API Management → Create API\n2. Set name and passphrase\n3. ✅ Permission: General ONLY\n4. Complete 2FA → copy Key, Secret AND Passphrase"),
    "bybit":    ("https://www.bybit.com/app/user/api-management",
                 "1. Account → API Management → Create New Key\n2. ✅ Permissions: Read-Only\n3. Complete 2FA → copy the keys"),
    "okx":      ("https://www.okx.com/account/my-api",
                 "1. Account → API → Create APIs\n2. Set name and passphrase\n3. ✅ Permission: Read ONLY\n4. Complete 2FA → copy Key, Secret AND Passphrase"),
    "bitget":   ("https://www.bitget.com/account/newapi",
                 "1. Profile → API Management → Create API\n2. Set label and passphrase\n3. ✅ Permission: Read-Only\n4. Complete 2FA → copy Key, Secret AND Passphrase"),
    "gate":     ("https://www.gate.io/myaccount/apiv4keys",
                 "1. Account → API Management → Create API Key\n2. ✅ Permission: Read account info ONLY\n3. Complete 2FA → copy Key and Secret"),
    "mexc":     ("https://www.mexc.com/user/openapi",
                 "1. Profile → API → Create\n2. ✅ Permission: Account Read ONLY\n3. Complete 2FA → copy Access Key and Secret Key"),
    "huobi":    ("https://www.htx.com/en-us/user/api_management",
                 "1. Account → API Management → Create\n2. ✅ Permission: Read Only\n3. Complete 2FA → copy Access Key and Secret Key"),
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
    entering_address  = State()
    choosing_chains   = State()
    entering_label    = State()

class AddChainToWallet(StatesGroup):
    choosing_wallet   = State()   # user picks which grouped wallet to expand
    choosing_chains   = State()   # user picks which new chains to add

class AddExchange(StatesGroup):
    choosing_exchange = State()
    entering_key      = State()
    entering_secret   = State()
    entering_password = State()
    entering_label    = State()

class RemovingWallet(StatesGroup):
    choosing_wallet   = State()   # pick address group
    choosing_action   = State()   # remove one chain or all chains

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


# ── Settings helpers ──────────────────────────────────────────────────────────

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
        f"💵 *Min balance to show:* {fmt_usd(s.min_balance_usd)}\n"
        f"    _Hides assets below this in /balances \\& /portfolio_\n\n"
        f"🔔 *Alert threshold:* {md(f'{s.notify_threshold_pct:.1f}')}%\n"
        f"    _Notifies when a balance changes by this %_\n\n"
        f"💸 *Min alert amount:* {fmt_usd(s.notify_min_usd)}\n"
        f"    _Alerts only if the move is worth at least this_\n\n"
        f"📳 *Notifications:* {md(notif)}\n"
    )


# ── Wallet grouping helpers ───────────────────────────────────────────────────

def _chain_tags(chains: list[str]) -> str:
    """Return emoji chain tags: ⟠🟡🟣 etc."""
    return "".join(CHAIN_CONFIG.get(c, {}).get("emoji", "") for c in sorted(chains))


def _chain_names(chains: list[str]) -> str:
    """Return comma-separated chain labels."""
    return ", ".join(CHAIN_CONFIG.get(c, {}).get("label", c) for c in sorted(chains))


def _build_wallet_groups(wallets: list) -> list[dict]:
    """
    Collapse wallet rows that share an address into groups.
    Returns list of dicts sorted by first-added.
    """
    raw = group_wallets_by_address(wallets)   # {address: {label, chains, ids, added_at}}
    return [
        {"address": addr, **info}
        for addr, info in raw.items()
    ]


def _evm_chains_not_tracked(tracked_chains: list[str]) -> list[str]:
    """Return EVM chains that are not yet in tracked_chains."""
    return [c for c in EVM_CHAINS if c not in tracked_chains]


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
        "I track your crypto balances across wallets and exchanges and alert you "
        "when deposits or withdrawals happen\\.\n\n"
        "  • *➕ Add Wallet* — paste any wallet address\n"
        "  • *➕ Add Exchange* — connect Binance, Bybit, etc\\.\n"
        "  • *💰 Balances* — live balances with USD values\n"
        "  • *⚙️ Settings* — filter dust, set alert thresholds\n\n"
        "_All API keys are encrypted and never leave this server\\._",
        parse_mode="MarkdownV2",
        reply_markup=MAIN_MENU,
    )


# ── /help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
@router.message(F.text == "❓ Help")
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "*Commands*\n\n"
        "💰 /balances — Live wallet & exchange balances\n"
        "📊 /portfolio — Total USD value by asset\n"
        "📋 /mywallets — View wallets grouped by address\n"
        "📋 /myexchanges — List connected exchanges\n"
        "➕ /addwallet — Track a wallet address\n"
        "➕ /addchain — Add chains to an existing wallet\n"
        "🗑 /removewallet — Remove a wallet or specific chain\n"
        "➕ /addexchange — Connect an exchange\n"
        "🗑 /removeexchange — Disconnect an exchange\n"
        "⚙️ /settings — Filters & alert thresholds\n\n"
        "*Chains* — ETH, BNB, Polygon, Arbitrum, Optimism, Base, Avalanche, Solana, Bitcoin\n"
        "*Exchanges* — Binance, Coinbase, Kraken, KuCoin, Bybit, OKX, Bitget, Gate, MEXC, HTX",
        parse_mode="MarkdownV2",
        reply_markup=MAIN_MENU,
    )


# ── /mywallets — grouped view ─────────────────────────────────────────────────

@router.message(Command("mywallets"))
@router.message(F.text == "📋 My Wallets")
async def cmd_my_wallets(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user    = await get_user_by_telegram_id(db, message.from_user.id)
        wallets = list(user.wallets) if user else []

    if not wallets:
        return await message.answer(
            "You have no wallets tracked yet\\.\nUse *➕ Add Wallet* to add one\\.",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )

    groups = _build_wallet_groups(wallets)
    lines  = [f"📋 *Your wallets* \\({len(groups)} address{'es' if len(groups)!=1 else ''}\\)\n"]

    for i, g in enumerate(groups, 1):
        addr      = g["address"]
        label     = md(g["label"]) if g["label"] else "_no label_"
        chains    = g["chains"]
        tags      = _chain_tags(chains)
        names     = md(_chain_names(chains))
        short     = md(addr[:6] + "…" + addr[-4:])   # 0xABCD…1234

        # Flag if more EVM chains could be added
        missing = _evm_chains_not_tracked(chains) if any(
            CHAIN_CONFIG.get(c, {}).get("type") == "evm" for c in chains
        ) else []
        add_hint = f"\n   _\\+ {len(missing)} more EVM chain{'s' if len(missing)!=1 else ''} available_" if missing else ""

        lines.append(
            f"{i}\\. {label}\n"
            f"   `{short}`\n"
            f"   {tags} {names}"
            f"{add_hint}"
        )

    lines.append(
        "\n_Use *➕ Add Chain* to track more chains on an existing wallet\\._\n"
        "_Use *🗑 Remove Wallet* to remove a wallet or specific chain\\._"
    )
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
            "No exchanges connected yet\\. Use *➕ Add Exchange*\\.",
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
        "Paste your wallet address:\n\n"
        "  • `0x…` — ETH, BNB, Polygon, Arbitrum, Optimism, Base, Avalanche\n"
        "  • Solana address \\(32–44 chars\\)\n"
        "  • Bitcoin address \\(starts with 1, 3, or bc1\\)\n\n"
        "_One 0x address covers all EVM chains — you choose which to track\\._",
        parse_mode="MarkdownV2",
        reply_markup=cancel_kb(),
    )


@router.message(AddWallet.entering_address)
async def add_wallet_address(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)

    address   = message.text.strip()
    addr_type = detect_address_type(address)

    if len(address) < 26 or addr_type == "unknown":
        return await message.answer(
            "⚠️ Could not recognise this address\\.\n"
            "  • EVM: `0x…` 42 chars\n"
            "  • Solana: 32–44 chars\n"
            "  • Bitcoin: starts with `1`, `3`, or `bc1`",
            parse_mode="MarkdownV2",
        )

    if addr_type == "evm":
        # Check if address already tracked — prefill already-tracked chains so user sees what's new
        async with AsyncSessionLocal() as db:
            user = await get_user_by_telegram_id(db, message.from_user.id)
            if user:
                already = [w.chain for w in user.wallets if w.address.lower() == address.lower()]
            else:
                already = []

        await state.update_data(address=address, addr_type="evm",
                                selected_chains=[], already_tracked=already)
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


async def _show_evm_chain_picker(
    message: Message,
    address: str,
    already_tracked: list[str],
    selected: list[str],
):
    """Build and send the EVM chain picker keyboard."""
    buttons = []
    for c in EVM_CHAINS:
        cfg   = CHAIN_CONFIG[c]
        label = f"{cfg['emoji']} {cfg['label']}"
        if c in already_tracked:
            label += " ✅"    # already tracked — shown as info
        elif c in selected:
            label += " 🔘"   # pending selection
        buttons.append([KeyboardButton(text=label)])

    buttons += [
        [KeyboardButton(text="🌐 All new EVM chains")],
        [KeyboardButton(text="✅ Done — save selected")],
        [KeyboardButton(text="❌ Cancel")],
    ]

    already_names = md(_chain_names(already_tracked)) if already_tracked else None
    selected_names = md(_chain_names(selected)) if selected else "none"
    already_note  = f"\n_Already tracking: {already_names}_" if already_names else ""

    await message.answer(
        f"✅ EVM address: `{md(address[:6])}…{md(address[-4:])}`{already_note}\n\n"
        f"Select chains to *add* \\(tap to toggle\\):\n"
        f"Currently selected: {selected_names}\n\n"
        "_Chains marked ✅ are already tracked\\._",
        parse_mode="MarkdownV2",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(AddWallet.choosing_chains)
async def add_wallet_choose_chains(message: Message, state: FSMContext):
    text = message.text
    data = await state.get_data()
    selected: list        = data.get("selected_chains", [])
    already: list         = data.get("already_tracked", [])
    address: str          = data["address"]

    if text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)

    if text == "🌐 All new EVM chains":
        # Select all chains NOT already tracked
        selected = [c for c in EVM_CHAINS if c not in already]
        if not selected:
            return await message.answer(
                "All EVM chains are already tracked for this address\\.",
                parse_mode="MarkdownV2",
            )
        await state.update_data(selected_chains=selected)
        await state.set_state(AddWallet.entering_label)
        return await message.answer(
            f"✅ Selected all {len(selected)} new EVM chains\\.\n\nGive this wallet a label, or /skip:",
            parse_mode="MarkdownV2", reply_markup=cancel_kb(),
        )

    if text == "✅ Done — save selected":
        if not selected:
            return await message.answer(
                "Select at least one chain first\\.", parse_mode="MarkdownV2",
            )
        await state.update_data(selected_chains=selected)
        await state.set_state(AddWallet.entering_label)
        return await message.answer(
            f"✅ Selected: {md(_chain_names(selected))}\n\nGive this wallet a label, or /skip:",
            parse_mode="MarkdownV2", reply_markup=cancel_kb(),
        )

    # Strip trailing status badges to find the chain key
    clean_text = text.replace(" ✅", "").replace(" 🔘", "").strip()
    chain = _EVM_LABEL_TO_CHAIN.get(clean_text)

    if chain:
        if chain in already:
            await message.answer(
                f"{CHAIN_CONFIG[chain]['emoji']} *{md(CHAIN_CONFIG[chain]['label'])}* is already tracked\\.",
                parse_mode="MarkdownV2",
            )
            return
        if chain in selected:
            selected.remove(chain)
        else:
            selected.append(chain)
        await state.update_data(selected_chains=selected)
        await _show_evm_chain_picker(message, address, already_tracked=already, selected=selected)
    else:
        await message.answer("Please use the buttons to select chains.")


@router.message(AddWallet.entering_label)
async def add_wallet_label(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)

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
            return await message.answer("Please /start first.")
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
    short   = md(address[:6] + "…" + address[-4:])
    await message.answer(
        f"`{short}`\n" + "\n".join(parts) + f"\nLabel: {lbl_str}\n\n"
        "_Use *➕ Add Chain* to track more chains on this address later\\._",
        parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
    )


# ── /addchain — add chains to existing wallet ─────────────────────────────────

@router.message(Command("addchain"))
@router.message(F.text == "➕ Add Chain")
async def cmd_add_chain_start(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user    = await get_user_by_telegram_id(db, message.from_user.id)
        wallets = list(user.wallets) if user else []

    if not wallets:
        return await message.answer(
            "You have no wallets yet\\. Use *➕ Add Wallet* first\\.",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )

    groups = _build_wallet_groups(wallets)
    # Only show groups that have expandable EVM chains
    expandable = [
        g for g in groups
        if any(CHAIN_CONFIG.get(c, {}).get("type") == "evm" for c in g["chains"])
        and _evm_chains_not_tracked(g["chains"])
    ]

    if not expandable:
        return await message.answer(
            "All your EVM wallets are already tracking all available chains\\!\n\n"
            "Use *➕ Add Wallet* to add a new address\\.",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )

    buttons = []
    lines   = ["Choose a wallet to add chains to:\n"]
    for i, g in enumerate(expandable, 1):
        lbl   = g["label"] or (g["address"][:6] + "…" + g["address"][-4:])
        tags  = _chain_tags(g["chains"])
        missing_count = len(_evm_chains_not_tracked(g["chains"]))
        lines.append(f"{i}\\. {tags} {md(lbl)} _\\({missing_count} chain{'s' if missing_count!=1 else ''} available\\)_")
        buttons.append([KeyboardButton(text=str(i))])

    buttons.append([KeyboardButton(text="❌ Cancel")])
    await state.set_state(AddChainToWallet.choosing_wallet)
    await state.update_data(expandable_groups=[(g["address"], g["chains"], g["label"]) for g in expandable])
    await message.answer(
        "\n".join(lines),
        parse_mode="MarkdownV2",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(AddChainToWallet.choosing_wallet)
async def add_chain_pick_wallet(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)

    data   = await state.get_data()
    groups = data["expandable_groups"]
    try:
        idx     = int(message.text) - 1
        address, tracked_chains, label = groups[idx]
    except (ValueError, IndexError):
        return await message.answer("Invalid choice. Use the buttons.")

    await state.update_data(
        target_address=address,
        already_tracked=tracked_chains,
        wallet_label=label,
        selected_chains=[],
    )
    await state.set_state(AddChainToWallet.choosing_chains)
    await _show_evm_chain_picker(message, address, already_tracked=tracked_chains, selected=[])


@router.message(AddChainToWallet.choosing_chains)
async def add_chain_pick_chains(message: Message, state: FSMContext):
    text   = message.text
    data   = await state.get_data()
    selected: list  = data.get("selected_chains", [])
    already: list   = data["already_tracked"]
    address: str    = data["target_address"]
    label: str|None = data.get("wallet_label")

    if text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)

    if text == "🌐 All new EVM chains":
        selected = [c for c in EVM_CHAINS if c not in already]
        if not selected:
            return await message.answer("All EVM chains already tracked\\.", parse_mode="MarkdownV2")
        # Save immediately
        await _save_new_chains(message, state, address, selected, label)
        return

    if text == "✅ Done — save selected":
        if not selected:
            return await message.answer("Select at least one chain\\.", parse_mode="MarkdownV2")
        await _save_new_chains(message, state, address, selected, label)
        return

    clean_text = text.replace(" ✅", "").replace(" 🔘", "").strip()
    chain = _EVM_LABEL_TO_CHAIN.get(clean_text)
    if chain:
        if chain in already:
            await message.answer(
                f"{CHAIN_CONFIG[chain]['emoji']} *{md(CHAIN_CONFIG[chain]['label'])}* is already tracked\\.",
                parse_mode="MarkdownV2",
            )
            return
        if chain in selected:
            selected.remove(chain)
        else:
            selected.append(chain)
        await state.update_data(selected_chains=selected)
        await _show_evm_chain_picker(message, address, already_tracked=already, selected=selected)
    else:
        await message.answer("Please use the buttons.")


async def _save_new_chains(message: Message, state: FSMContext, address: str, chains: list, label: str | None):
    """Save new chain rows to DB and send confirmation."""
    added = []
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, message.from_user.id)
        if not user:
            await state.clear()
            return await message.answer("Please /start first.")
        for chain in chains:
            if not await get_wallet_exists(db, user.id, chain, address):
                db.add(Wallet(user_id=user.id, chain=chain, address=address, label=label))
                added.append(CHAIN_CONFIG[chain]["label"])
        await db.commit()

    await state.clear()
    short = md(address[:6] + "…" + address[-4:])
    if added:
        tags = _chain_tags([c for c in EVM_CHAINS if CHAIN_CONFIG[c]["label"] in added])
        await message.answer(
            f"✅ Added {md(', '.join(added))} to `{short}`\n{tags}",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )
    else:
        await message.answer("No new chains were added \\(all already tracked\\)\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── /removewallet — grouped with per-chain option ─────────────────────────────

@router.message(Command("removewallet"))
@router.message(F.text == "🗑 Remove Wallet")
async def cmd_remove_wallet(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user    = await get_user_by_telegram_id(db, message.from_user.id)
        wallets = list(user.wallets) if user else []

    if not wallets:
        return await message.answer("You have no wallets to remove.", reply_markup=MAIN_MENU)

    groups = _build_wallet_groups(wallets)
    lines  = ["Choose a wallet to remove:\n"]
    buttons = []
    for i, g in enumerate(groups, 1):
        lbl   = g["label"] or (g["address"][:6] + "…" + g["address"][-4:])
        tags  = _chain_tags(g["chains"])
        names = md(_chain_names(g["chains"]))
        lines.append(f"{i}\\. {tags} {md(lbl)}\n   {names}")
        buttons.append([KeyboardButton(text=str(i))])

    buttons.append([KeyboardButton(text="❌ Cancel")])
    await state.set_state(RemovingWallet.choosing_wallet)
    await state.update_data(groups=[(g["address"], g["chains"], g["ids"], g["label"]) for g in groups])
    await message.answer(
        "\n".join(lines),
        parse_mode="MarkdownV2",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(RemovingWallet.choosing_wallet)
async def remove_wallet_pick(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)

    data   = await state.get_data()
    groups = data["groups"]  # list of (address, chains, ids, label)
    try:
        idx = int(message.text) - 1
        address, chains, ids, label = groups[idx]
    except (ValueError, IndexError):
        return await message.answer("Invalid choice. Use the buttons.")

    await state.update_data(target_address=address, target_chains=chains, target_ids=ids)
    await state.set_state(RemovingWallet.choosing_action)

    lbl_str = md(label) if label else md(address[:6] + "…" + address[-4:])

    if len(chains) == 1:
        # Only one chain — just confirm removal
        buttons = [
            [KeyboardButton(text="🗑 Yes, remove it")],
            [KeyboardButton(text="❌ Cancel")],
        ]
        await message.answer(
            f"Remove {bold(lbl_str)} on {md(CHAIN_CONFIG.get(chains[0], {}).get('label', chains[0]))}?\n\n"
            f"_This will delete all balance history for this wallet\\._",
            parse_mode="MarkdownV2",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
    else:
        # Multiple chains — let user pick: one chain or all
        chain_buttons = [
            [KeyboardButton(text=f"Remove {CHAIN_CONFIG[c]['emoji']} {CHAIN_CONFIG[c]['label']} only")]
            for c in chains
        ]
        chain_buttons += [
            [KeyboardButton(text="🗑 Remove ALL chains for this address")],
            [KeyboardButton(text="❌ Cancel")],
        ]
        tags  = _chain_tags(chains)
        names = md(_chain_names(chains))
        await message.answer(
            f"{tags} {bold(lbl_str)}\n"
            f"Tracked on: {names}\n\n"
            f"Remove which chain\\(s\\)?",
            parse_mode="MarkdownV2",
            reply_markup=ReplyKeyboardMarkup(keyboard=chain_buttons, resize_keyboard=True),
        )


@router.message(RemovingWallet.choosing_action)
async def remove_wallet_action(message: Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.clear()
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)

    data    = await state.get_data()
    chains  = data["target_chains"]
    ids     = data["target_ids"]

    # Map "Remove ⟠ Ethereum only" → chain key
    chains_to_remove = []
    if message.text == "🗑 Yes, remove it" or message.text == "🗑 Remove ALL chains for this address":
        chains_to_remove = list(chains)
    else:
        for c in chains:
            cfg = CHAIN_CONFIG.get(c, {})
            if message.text == f"Remove {cfg.get('emoji','')} {cfg.get('label', c)} only":
                chains_to_remove = [c]
                break
        if not chains_to_remove:
            return await message.answer("Invalid choice. Please use the buttons.")

    # Build wallet id → chain map
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
    if removed:
        await message.answer(
            f"✅ Removed: {md(', '.join(removed))}",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )
    else:
        await message.answer("Nothing was removed.", reply_markup=MAIN_MENU)


# ── /balances ─────────────────────────────────────────────────────────────────

@router.message(Command("balances"))
@router.message(F.text == "💰 Balances")
async def cmd_balances(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, message.from_user.id)
        if not user:
            return await message.answer("Please /start first.")
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
        min_usd = s.min_balance_usd

    if not wallets and not exchanges:
        return await message.answer(
            "No wallets or exchanges added yet\\.\nUse *➕ Add Wallet* or *➕ Add Exchange*\\.",
            parse_mode="MarkdownV2", reply_markup=MAIN_MENU,
        )

    filter_note = f" _\\(hiding below {fmt_usd(min_usd)}\\)_" if min_usd > 0 else ""
    status_msg  = await message.answer(f"⏳ Fetching balances{filter_note}\\.\\.\\.", parse_mode="MarkdownV2")

    try:
        lines            = []
        hidden_count     = 0

        # ── Wallets — group by address ─────────────────────────────────────
        if wallets:
            try:
                wallet_results = await asyncio.wait_for(get_all_wallet_balances(wallets), timeout=20.0)
            except asyncio.TimeoutError:
                wallet_results = [
                    {**w, "balance": 0.0,
                     "symbol": CHAIN_CONFIG.get(w["chain"], {}).get("symbol", "?"),
                     "error": "Timed out — try again"}
                    for w in wallets
                ]

            # Attach input labels
            for r, w in zip(wallet_results, wallets):
                r["label"] = w.get("label")

            symbols = list({r["symbol"] for r in wallet_results if not r.get("error")})
            prices  = await get_usd_prices(symbols) if symbols else {}

            # Group results by address for display
            addr_groups: dict[str, list] = defaultdict(list)
            for r in wallet_results:
                addr_groups[r["address"]].append(r)

            lines.append("🏦 *Wallets*")
            for addr, results in addr_groups.items():
                # Header: label + short address + chain tags
                sample_label = next((r["label"] for r in results if r.get("label")), None)
                all_chains   = [r["chain"] for r in results]
                tags         = _chain_tags(all_chains)
                lbl          = md(sample_label) if sample_label else md(addr[:6] + "…" + addr[-4:])
                lines.append(f"\n  {tags} {bold(lbl)}")

                for r in results:
                    cfg   = CHAIN_CONFIG.get(r["chain"], {})
                    emoji = cfg.get("emoji", "")
                    cname = md(cfg.get("label", r["chain"]))
                    if r.get("error"):
                        lines.append(f"    {emoji} {cname}: ❌ {md(r['error'][:60])}")
                    else:
                        bal = r["balance"]
                        sym = r["symbol"]
                        usd = bal * prices.get(sym, 0)
                        if usd < min_usd and bal > 0 and min_usd > 0:
                            hidden_count += 1
                            continue
                        usd_str = f" \\({fmt_usd(usd)}\\)" if usd >= 0.01 else ""
                        lines.append(f"    {emoji} {cname}: `{fmt_num(bal)} {md(sym)}`{usd_str}")

        # ── Exchanges ──────────────────────────────────────────────────────
        if exchanges:
            lines.append("")
            lines.append("💱 *Exchanges*")
            try:
                exchange_results = await asyncio.wait_for(get_all_exchange_balances(exchanges), timeout=25.0)
            except asyncio.TimeoutError:
                exchange_results = [
                    {"exchange": e["exchange_id"], "balances": [], "error": "Timed out — try again"}
                    for e in exchanges
                ]

            for i, r in enumerate(exchange_results):
                lbl = md(exchanges[i].get("label") or r["exchange"].upper())
                if r.get("error"):
                    lines.append(f"  {bold(lbl)}\n    ❌ {md(r['error'][:80])}")
                    continue
                if not r["balances"]:
                    lines.append(f"  {bold(lbl)}\n    _No balances found_")
                    continue

                lines.append(f"\n  {bold(lbl)}")
                syms   = [b["asset"] for b in r["balances"]]
                prices = await get_usd_prices(syms) if syms else {}
                ex_hidden = 0
                for b in r["balances"]:
                    sym  = b["asset"]
                    tot  = b["total"]
                    usd  = tot * prices.get(sym, 0)
                    if usd < min_usd and tot > 0 and min_usd > 0:
                        ex_hidden += 1; hidden_count += 1; continue
                    usd_str    = f" \\({fmt_usd(usd)}\\)" if usd >= 0.01 else ""
                    locked_str = f" \\[locked: {fmt_num(b.get('locked',0),4)}\\]" if b.get("locked",0)>0 else ""
                    lines.append(f"    • `{md(sym)}` {fmt_num(tot)}{locked_str}{usd_str}")
                if ex_hidden:
                    lines.append(f"    _\\+ {ex_hidden} small asset\\(s\\) hidden_")

        if hidden_count > 0:
            lines.append(f"\n_⚙️ {hidden_count} asset\\(s\\) below {fmt_usd(min_usd)} hidden — change in /settings_")

        text = "\n".join(lines) if lines else "No balances to show\\."
        if len(text) > 4000:
            text = text[:4000] + "\n\n_\\.\\.\\. truncated — use /portfolio_"

        await status_msg.delete()
        await message.answer(text, parse_mode="MarkdownV2", reply_markup=MAIN_MENU)

    except Exception as e:
        log.exception("Balances error")
        await status_msg.delete()
        await message.answer(f"❌ Error: {md(str(e)[:120])}", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── /portfolio ────────────────────────────────────────────────────────────────

@router.message(Command("portfolio"))
@router.message(F.text == "📊 Portfolio")
async def cmd_portfolio(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, message.from_user.id)
        if not user:
            return await message.answer("Please /start first.")
        wallets = [{"chain": w.chain, "address": w.address, "label": w.label} for w in user.wallets]
        exchanges = []
        for e in user.exchanges:
            try:
                exchanges.append({
                    "exchange_id":  e.exchange_id,
                    "api_key":      decrypt(e.api_key),
                    "api_secret":   decrypt(e.api_secret),
                    "api_password": decrypt(e.api_password) if e.api_password else None,
                })
            except Exception:
                pass
        s = await get_or_create_settings(db, user)
        min_usd = s.min_balance_usd

    if not wallets and not exchanges:
        return await message.answer("No wallets or exchanges added yet\\.", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)

    filter_note = f" _\\(hiding below {fmt_usd(min_usd)}\\)_" if min_usd > 0 else ""
    status_msg  = await message.answer(f"⏳ Calculating portfolio{filter_note}\\.\\.\\.", parse_mode="MarkdownV2")

    try:
        totals: dict[str, float] = {}

        if wallets:
            try:
                results = await asyncio.wait_for(get_all_wallet_balances(wallets), timeout=20.0)
            except asyncio.TimeoutError:
                results = []
            for r in results:
                if not r.get("error") and r["balance"] > 0:
                    totals[r["symbol"]] = totals.get(r["symbol"], 0) + r["balance"]

        if exchanges:
            try:
                results = await asyncio.wait_for(get_all_exchange_balances(exchanges), timeout=25.0)
            except asyncio.TimeoutError:
                results = []
            for r in results:
                for b in r.get("balances", []):
                    totals[b["asset"]] = totals.get(b["asset"], 0) + b["total"]

        if not totals:
            await status_msg.delete()
            return await message.answer(
                "No balances found\\. Check wallets/exchanges or try again shortly\\.",
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
        if min_usd > 0:
            lines.append(f"_⚙️ Adjust filter in /settings_")

        await status_msg.delete()
        await message.answer("\n".join(lines), parse_mode="MarkdownV2", reply_markup=MAIN_MENU)

    except Exception as e:
        log.exception("Portfolio error")
        await status_msg.delete()
        await message.answer(f"❌ Error: {md(str(e)[:120])}", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── ⚙️ Settings ───────────────────────────────────────────────────────────────

@router.message(Command("settings"))
@router.message(F.text == "⚙️ Settings")
async def cmd_settings(message: Message, state: FSMContext):
    await state.clear()
    s = await _load_settings(message.from_user.id)
    if not s:
        return await message.answer("Please /start first.")
    await message.answer(
        _settings_summary(s) + "\nChoose a setting to change:",
        parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU,
    )

@router.message(F.text == "📊 View My Settings")
async def cmd_view_settings(message: Message, state: FSMContext):
    await state.clear()
    s = await _load_settings(message.from_user.id)
    if not s:
        return await message.answer("Please /start first.")
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
        return await message.answer("Please /start first.")
    await message.answer(_settings_summary(s) + "\nChoose a setting to change:", parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU)

@router.message(Command("notifications"))
@router.message(F.text == "🔕 Toggle Notifications")
async def cmd_toggle_notifications(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, message.from_user.id)
        if not user:
            return await message.answer("Please /start first.")
        s = await get_or_create_settings(db, user)
        s.notifications_enabled = not s.notifications_enabled
        await db.commit()
        status = "ON 🔔" if s.notifications_enabled else "OFF 🔕"
    await message.answer(
        f"Notifications: *{md(status)}*\n_Checks every 5 min, alerts on changes above your threshold\\._",
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
        return await message.answer("Enter a valid number, e\\.g\\. `1`, `5`, `0`\\.", parse_mode="MarkdownV2")
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
        "`1` \\= sensitive \\| `5` \\= moderate \\| `10` \\= big moves only\n\nEnter a %:",
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
        "Alerts only fire when the move is worth at least this in USD\\.\n\n"
        "`0` \\= any change \\| `1` \\= \\$1\\+ \\| `10` \\= \\$10\\+\n\nEnter a USD amount:",
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
        return await message.answer("Enter a valid amount, e\\.g\\. `0`, `1`, `10`\\.", parse_mode="MarkdownV2")
    await _save_setting(message.from_user.id, notify_min_usd=value)
    await state.clear()
    await message.answer(f"✅ Min alert amount: {fmt_usd(value)}", parse_mode="MarkdownV2", reply_markup=SETTINGS_MENU)


# ── Add Exchange ──────────────────────────────────────────────────────────────

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
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)
    exchange_id = message.text.lower().strip()
    if exchange_id not in SUPPORTED_EXCHANGES:
        return await message.answer("Please choose one of the buttons.")
    await state.update_data(exchange_id=exchange_id)
    await state.set_state(AddExchange.entering_key)
    guide_url, steps = EXCHANGE_GUIDES.get(exchange_id, ("", "Create a read-only API key on the exchange website."))
    await message.answer(
        f"🔑 *{md(exchange_id.upper())} read\\-only API key setup*\n\n{md(steps)}\n\n"
        f"🔗 [Open API settings]({guide_url})\n\n"
        f"⚠️ *READ ONLY* — never enable withdrawals or trading\\.\n\nPaste your *API Key*:",
        parse_mode="MarkdownV2", reply_markup=ReplyKeyboardRemove(),
    )

@router.message(AddExchange.entering_key)
async def add_exchange_key(message: Message, state: FSMContext):
    await state.update_data(api_key=message.text.strip())
    try: await message.delete()
    except Exception: pass
    await state.set_state(AddExchange.entering_secret)
    await message.answer("✅ Key received \\(deleted from chat\\)\\. Now paste your *API Secret*:", parse_mode="MarkdownV2")

@router.message(AddExchange.entering_secret)
async def add_exchange_secret(message: Message, state: FSMContext):
    await state.update_data(api_secret=message.text.strip())
    try: await message.delete()
    except Exception: pass
    data = await state.get_data()
    if data["exchange_id"] in EXCHANGES_NEEDING_PASSWORD:
        await state.set_state(AddExchange.entering_password)
        await message.answer(f"✅ Secret received\\.\n*{md(data['exchange_id'].upper())}* needs a *Passphrase* too\\. Enter it:", parse_mode="MarkdownV2")
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
            return await message.answer("Please /start first.")
        db.add(Exchange(
            user_id=user.id, exchange_id=data["exchange_id"], label=label,
            api_key=encrypt(data["api_key"]), api_secret=encrypt(data["api_secret"]),
            api_password=encrypt(data["api_password"]) if data.get("api_password") else None,
        ))
        await db.commit()
    await state.clear()
    lbl_str = md(label) if label else "_no label_"
    await message.answer(f"✅ *{md(data['exchange_id'].upper())}* connected\\! {lbl_str}\n_Keys encrypted at rest\\._", parse_mode="MarkdownV2", reply_markup=MAIN_MENU)


# ── Remove Exchange ───────────────────────────────────────────────────────────

@router.message(Command("removeexchange"))
@router.message(F.text == "🗑 Remove Exchange")
async def cmd_remove_exchange(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as db:
        user      = await get_user_by_telegram_id(db, message.from_user.id)
        exchanges = list(user.exchanges) if user else []
    if not exchanges:
        return await message.answer("No exchanges connected.", reply_markup=MAIN_MENU)
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
        return await message.answer("Cancelled.", reply_markup=MAIN_MENU)
    data = await state.get_data()
    try:
        eid = data["exchange_ids"][int(message.text) - 1]
    except (ValueError, IndexError):
        return await message.answer("Invalid choice.")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Exchange).where(Exchange.id == eid))
        exch   = result.scalar_one_or_none()
        if exch:
            await db.delete(exch)
            await db.commit()
    await state.clear()
    await message.answer("✅ Exchange disconnected.", reply_markup=MAIN_MENU)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    await init_db()
    log.info("Database tables ready.")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_and_notify, "interval",
        seconds=config.POLL_INTERVAL_SECONDS,
        args=[bot], id="notify_poll",
        replace_existing=True,
        misfire_grace_time=60,
        max_instances=1,   # never run two polls simultaneously
        coalesce=True,     # if missed, run once not multiple times
    )
    scheduler.start()
    log.info(f"Scheduler started — polling every {config.POLL_INTERVAL_SECONDS}s")
    log.info("Bot starting…")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())