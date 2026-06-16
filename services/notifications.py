"""
services/notifications.py — balance poller, price alerts, digest
"""
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import select

from db import AsyncSessionLocal, User, Wallet, Exchange, BalanceLog, PriceAlert, get_or_create_settings, prune_balance_logs
from config import BALANCELOG_RETENTION_DAYS
from crypto import decrypt
from services.wallets import get_wallet_balance
from services.exchanges import get_exchange_balance
from services.prices import get_usd_prices

log = logging.getLogger(__name__)

_last_prune: datetime = datetime.min
_last_digest: dict[int, datetime] = {}


async def poll_and_notify(bot: Bot):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        users  = result.scalars().all()

    log.info(f"[notify] polling {len(users)} user(s)")
    for user in users:
        try:
            await _check_user(bot, user)
        except Exception as e:
            log.warning(f"[notify] error for user {user.telegram_id}: {e}")

    global _last_prune
    if datetime.utcnow() - _last_prune > timedelta(hours=24):
        try:
            async with AsyncSessionLocal() as db:
                deleted = await prune_balance_logs(db, BALANCELOG_RETENTION_DAYS)
            if deleted:
                log.info(f"[prune] deleted {deleted} old BalanceLog rows")
            _last_prune = datetime.utcnow()
        except Exception as e:
            log.warning(f"[prune] failed: {e}")

    try:
        await _check_price_alerts(bot)
    except Exception as e:
        log.warning(f"[alerts] error: {e}")


async def _check_user(bot: Bot, user: User):
    async with AsyncSessionLocal() as db:
        result   = await db.execute(select(User).where(User.id == user.id))
        fresh    = result.scalar_one_or_none()
        if not fresh:
            return
        settings = await get_or_create_settings(db, fresh)
        if not settings.notifications_enabled:
            return
        threshold_pct = settings.notify_threshold_pct
        min_usd_move  = settings.notify_min_usd
        wallets   = list(fresh.wallets)
        exchanges = list(fresh.exchanges)

    messages: list[str] = []

    async with AsyncSessionLocal() as db:
        for wallet in wallets:
            try:
                result = await asyncio.wait_for(get_wallet_balance(wallet.chain, wallet.address), timeout=10)
            except asyncio.TimeoutError:
                continue
            if result.get("error"):
                continue

            symbol  = result["symbol"]
            current = result["balance"]
            prices  = await get_usd_prices([symbol])
            price   = prices.get(symbol, 0)
            usd     = current * price

            prev_r = await db.execute(
                select(BalanceLog)
                .where(BalanceLog.wallet_id == wallet.id, BalanceLog.asset == symbol)
                .order_by(BalanceLog.recorded_at.desc()).limit(1)
            )
            prev = prev_r.scalar_one_or_none()
            db.add(BalanceLog(user_id=user.id, wallet_id=wallet.id, asset=symbol, amount=current, usd_value=usd))

            if prev is not None:
                diff     = current - prev.amount
                pct      = abs(diff / prev.amount * 100) if prev.amount else 100
                usd_diff = abs(diff) * price
                if pct >= threshold_pct and usd_diff >= min_usd_move:
                    label   = wallet.label or f"{wallet.chain}:{wallet.address[:8]}..."
                    arrow   = "📈" if diff > 0 else "📉"
                    verb    = "Received" if diff > 0 else "Sent"
                    usd_str = f" (≈ ${usd_diff:,.2f})" if usd_diff >= 0.01 else ""
                    messages.append(
                        f"{arrow} {label}\n"
                        f"  {verb}: {abs(diff):.6f} {symbol}{usd_str}\n"
                        f"  Balance: {current:.6f} {symbol} (≈ ${usd:,.2f})"
                    )

        for exch in exchanges:
            try:
                api_key    = decrypt(exch.api_key)
                api_secret = decrypt(exch.api_secret)
                api_pw     = decrypt(exch.api_password) if exch.api_password else None
            except ValueError:
                continue

            try:
                result = await asyncio.wait_for(
                    get_exchange_balance(exch.exchange_id, api_key, api_secret, api_pw), timeout=20
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

                prev_r = await db.execute(
                    select(BalanceLog)
                    .where(BalanceLog.exchange_id == exch.id, BalanceLog.asset == symbol)
                    .order_by(BalanceLog.recorded_at.desc()).limit(1)
                )
                prev = prev_r.scalar_one_or_none()
                db.add(BalanceLog(user_id=user.id, exchange_id=exch.id, asset=symbol, amount=current, usd_value=usd))

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

    if messages:
        header = "🔔 Portfolio Alert\n" + "─" * 24 + "\n\n"
        text   = header + "\n\n".join(messages)
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (truncated)"
        try:
            await bot.send_message(user.telegram_id, text)
            log.info(f"[notify] sent {len(messages)} alert(s) to {user.telegram_id}")
        except Exception as e:
            log.warning(f"[notify] send failed for {user.telegram_id}: {e}")


async def _check_price_alerts(bot: Bot):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PriceAlert).where(PriceAlert.triggered == False))
        alerts = result.scalars().all()
        if not alerts:
            return

        symbols = list({a.asset for a in alerts})
        prices  = await get_usd_prices(symbols)
        fired: list[tuple[int, str]] = []

        for alert in alerts:
            price = prices.get(alert.asset, 0)
            if price == 0:
                continue
            hit = (
                (alert.direction == "above" and price >= alert.target_usd) or
                (alert.direction == "below" and price <= alert.target_usd)
            )
            if hit:
                alert.triggered = True
                direction_emoji = "📈" if alert.direction == "above" else "📉"
                user_r = await db.execute(select(User).where(User.id == alert.user_id))
                user   = user_r.scalar_one_or_none()
                if user:
                    msg = (
                        f"🎯 Price Alert Triggered!\n"
                        f"{direction_emoji} {alert.asset} is now ${price:,.2f}\n"
                        f"   (your target: {alert.direction} ${alert.target_usd:,.2f})"
                    )
                    fired.append((user.telegram_id, msg))

        await db.commit()

    for telegram_id, msg in fired:
        try:
            await bot.send_message(telegram_id, msg)
            log.info(f"[alerts] price alert fired for {telegram_id}")
        except Exception as e:
            log.warning(f"[alerts] send failed: {e}")


async def send_digest(bot: Bot):
    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        users  = result.scalars().all()

    for user in users:
        try:
            async with AsyncSessionLocal() as db:
                fresh = (await db.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
                if not fresh:
                    continue
                settings = await get_or_create_settings(db, fresh)
                if not settings.digest_enabled:
                    continue
                last = _last_digest.get(user.id)
                if last:
                    delta = (now - last).days
                    if settings.digest_frequency == "daily"  and delta < 1: continue
                    if settings.digest_frequency == "weekly" and delta < 7: continue

            async with AsyncSessionLocal() as db:
                log_result = await db.execute(
                    select(BalanceLog).where(BalanceLog.user_id == user.id).order_by(BalanceLog.recorded_at.desc())
                )
                logs = log_result.scalars().all()

            seen: set[str] = set()
            totals: dict[str, float] = {}
            for entry in logs:
                if entry.asset not in seen:
                    totals[entry.asset] = totals.get(entry.asset, 0) + entry.amount
                    seen.add(entry.asset)

            prices      = await get_usd_prices(list(totals.keys()))
            grand_total = sum(amt * prices.get(sym, 0) for sym, amt in totals.items())
            rows = sorted(
                [(sym, amt, amt * prices.get(sym, 0)) for sym, amt in totals.items()],
                key=lambda x: x[2], reverse=True
            )

            freq  = settings.digest_frequency.capitalize()
            lines = [f"📊 {freq} Portfolio Digest — {now.strftime('%Y-%m-%d')}\n{'─'*30}\n"]
            for sym, amt, usd in rows[:10]:
                pct = (usd / grand_total * 100) if grand_total else 0
                lines.append(f"  {sym:<8} ${usd:>10,.2f}   ({pct:.1f}%)")
            if len(rows) > 10:
                lines.append(f"  ... and {len(rows)-10} more assets")
            lines.append(f"\n💵 Total: ${grand_total:,.2f} USD")

            await bot.send_message(user.telegram_id, "\n".join(lines))
            _last_digest[user.id] = now
            log.info(f"[digest] sent to {user.telegram_id}")
        except Exception as e:
            log.warning(f"[digest] error for {user.telegram_id}: {e}")