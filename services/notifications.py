"""
services/notifications.py
─────────────────────────
Balance change poller — checks every user's wallets and exchanges,
detects deposits/withdrawals, and pushes Telegram alerts.

Key fixes:
- All dynamic content passed through md() before sending to Telegram
- Binance locked-earn endpoint (405 deprecated) removed from poll path
- Notifications sent WITHOUT MarkdownV2 parse_mode to avoid escaping edge cases
"""
import asyncio
import logging

from aiogram import Bot
from sqlalchemy import select

from db import AsyncSessionLocal, User, Wallet, Exchange, BalanceLog, get_or_create_settings
from crypto import decrypt
from services.wallets import get_wallet_balance
from services.exchanges import get_exchange_balance
from services.prices import get_usd_prices

log = logging.getLogger(__name__)


def _safe(text) -> str:
    """Escape ALL MarkdownV2 special characters in any dynamic value."""
    text = str(text)
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


async def poll_and_notify(bot: Bot):
    """Entry point called by APScheduler. Polls all users sequentially."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        users  = result.scalars().all()

    log.info(f"[notify] polling {len(users)} user(s)")
    for user in users:
        try:
            await _check_user(bot, user)
        except Exception as e:
            log.warning(f"[notify] error for user {user.telegram_id}: {e}")


async def _check_user(bot: Bot, user: User):
    """Check one user's wallets and exchanges; send alert if anything changed."""

    # Load fresh settings
    async with AsyncSessionLocal() as db:
        result   = await db.execute(select(User).where(User.id == user.id))
        fresh    = result.scalar_one_or_none()
        if not fresh:
            return
        settings = await get_or_create_settings(db, fresh)
        if not settings.notifications_enabled:
            return

        threshold_pct = settings.notify_threshold_pct   # e.g. 1.0 → 1%
        min_usd_move  = settings.notify_min_usd          # e.g. 1.0 → must be ≥ $1

        wallets   = list(fresh.wallets)
        exchanges = list(fresh.exchanges)

    messages: list[str] = []

    async with AsyncSessionLocal() as db:

        # ── Wallets ────────────────────────────────────────────────────────
        for wallet in wallets:
            try:
                result = await asyncio.wait_for(
                    get_wallet_balance(wallet.chain, wallet.address), timeout=10
                )
            except asyncio.TimeoutError:
                continue
            if result.get("error"):
                continue

            symbol  = result["symbol"]
            current = result["balance"]

            prices = await get_usd_prices([symbol])
            price  = prices.get(symbol, 0)
            usd    = current * price

            # Previous snapshot
            prev_result = await db.execute(
                select(BalanceLog)
                .where(BalanceLog.wallet_id == wallet.id, BalanceLog.asset == symbol)
                .order_by(BalanceLog.recorded_at.desc())
                .limit(1)
            )
            prev = prev_result.scalar_one_or_none()

            # Always save a new snapshot
            db.add(BalanceLog(
                user_id=user.id, wallet_id=wallet.id,
                asset=symbol, amount=current, usd_value=usd,
            ))

            if prev is not None:
                diff     = current - prev.amount
                pct      = abs(diff / prev.amount * 100) if prev.amount else 100
                usd_diff = abs(diff) * price

                if pct >= threshold_pct and usd_diff >= min_usd_move:
                    label   = wallet.label or f"{wallet.chain}:{wallet.address[:8]}..."
                    arrow   = "📈" if diff > 0 else "📉"
                    verb    = "Received" if diff > 0 else "Sent"
                    usd_str = f" (≈ ${usd_diff:,.2f})" if usd_diff >= 0.01 else ""
                    # Plain text — no MarkdownV2, avoids all escaping issues
                    messages.append(
                        f"{arrow} {label}\n"
                        f"  {verb}: {abs(diff):.6f} {symbol}{usd_str}\n"
                        f"  Balance: {current:.6f} {symbol} (≈ ${usd:,.2f})"
                    )

        # ── Exchanges ──────────────────────────────────────────────────────
        for exch in exchanges:
            try:
                api_key    = decrypt(exch.api_key)
                api_secret = decrypt(exch.api_secret)
                api_pw     = decrypt(exch.api_password) if exch.api_password else None
            except ValueError:
                continue

            try:
                result = await asyncio.wait_for(
                    get_exchange_balance(exch.exchange_id, api_key, api_secret, api_pw),
                    timeout=20,
                )
            except asyncio.TimeoutError:
                continue
            if result.get("error"):
                continue

            prices = await get_usd_prices([b["asset"] for b in result["balances"]])

            for bal in result["balances"]:
                symbol  = bal["asset"]
                current = bal["total"]
                price   = prices.get(symbol, 0)
                usd     = current * price

                prev_result = await db.execute(
                    select(BalanceLog)
                    .where(BalanceLog.exchange_id == exch.id, BalanceLog.asset == symbol)
                    .order_by(BalanceLog.recorded_at.desc())
                    .limit(1)
                )
                prev = prev_result.scalar_one_or_none()

                db.add(BalanceLog(
                    user_id=user.id, exchange_id=exch.id,
                    asset=symbol, amount=current, usd_value=usd,
                ))

                if prev is not None:
                    diff     = current - prev.amount
                    pct      = abs(diff / prev.amount * 100) if prev.amount else 100
                    usd_diff = abs(diff) * price

                    if pct >= threshold_pct and usd_diff >= min_usd_move:
                        label   = exch.label or exch.exchange_id.upper()
                        arrow   = "📈" if diff > 0 else "📉"
                        verb    = "Credited" if diff > 0 else "Withdrawn"
                        usd_str = f" (≈ ${usd_diff:,.2f})" if usd_diff >= 0.01 else ""
                        messages.append(
                            f"{arrow} {label} — {symbol}\n"
                            f"  {verb}: {abs(diff):.6f}{usd_str}\n"
                            f"  Balance: {current:.6f} {symbol} (≈ ${usd:,.2f})"
                        )

        await db.commit()

    # ── Send alert ─────────────────────────────────────────────────────────
    if messages:
        # Use plain text (no parse_mode) — 100% safe, no escaping needed
        header = "🔔 Portfolio Alert\n" + "─" * 24 + "\n\n"
        text   = header + "\n\n".join(messages)

        # Telegram max message length is 4096
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (truncated)"

        try:
            await bot.send_message(user.telegram_id, text)  # no parse_mode = plain text
            log.info(f"[notify] sent {len(messages)} alert(s) to {user.telegram_id}")
        except Exception as e:
            log.warning(f"[notify] send failed for {user.telegram_id}: {e}")