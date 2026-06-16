"""
db.py — async SQLAlchemy 2.0 + asyncpg models
"""
import ssl
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float,
    ForeignKey, Integer, String, UniqueConstraint, select, text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from config import DATABASE_URL, IS_POSTGRES

_engine_kwargs: dict = {"echo": False}
if IS_POSTGRES:
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE
    _engine_kwargs["connect_args"] = {"ssl": _ssl_ctx}

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class UserSettings(Base):
    __tablename__ = "user_settings"

    id                    = Column(Integer,  primary_key=True, index=True)
    user_id               = Column(Integer,  ForeignKey("users.id"), unique=True, nullable=False)
    min_balance_usd       = Column(Float,    default=1.0,   nullable=False)
    notify_threshold_pct  = Column(Float,    default=1.0,   nullable=False)
    notify_min_usd        = Column(Float,    default=1.0,   nullable=False)
    notifications_enabled = Column(Boolean,  default=True,  nullable=False)
    digest_enabled        = Column(Boolean,  default=False, nullable=False)
    digest_frequency      = Column(String(8),default="daily", nullable=False)
    updated_at            = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="settings")


class User(Base):
    __tablename__ = "users"

    id            = Column(Integer,    primary_key=True, index=True)
    telegram_id   = Column(BigInteger, unique=True, nullable=False, index=True)
    username      = Column(String(64),  nullable=True)
    first_name    = Column(String(128), nullable=True)
    joined_at     = Column(DateTime, default=datetime.utcnow)
    notifications = Column(Boolean,  default=True)

    wallets   = relationship("Wallet",       back_populates="user", cascade="all, delete", lazy="selectin")
    exchanges = relationship("Exchange",     back_populates="user", cascade="all, delete", lazy="selectin")
    logs      = relationship("BalanceLog",   back_populates="user", cascade="all, delete", lazy="selectin")
    settings  = relationship("UserSettings", back_populates="user", cascade="all, delete", lazy="selectin", uselist=False)
    alerts    = relationship("PriceAlert",   back_populates="user", cascade="all, delete", lazy="selectin")


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("user_id", "chain", "address", name="uq_wallet"),)

    id       = Column(Integer,    primary_key=True, index=True)
    user_id  = Column(Integer,    ForeignKey("users.id"), nullable=False)
    chain    = Column(String(32),  nullable=False)
    address  = Column(String(128), nullable=False)
    label    = Column(String(64),  nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="wallets")
    logs = relationship("BalanceLog", back_populates="wallet", cascade="all, delete", lazy="selectin")


class Exchange(Base):
    __tablename__ = "exchanges"
    __table_args__ = (UniqueConstraint("user_id", "exchange_id", name="uq_exchange"),)

    id           = Column(Integer,    primary_key=True, index=True)
    user_id      = Column(Integer,    ForeignKey("users.id"), nullable=False)
    exchange_id  = Column(String(32),  nullable=False)
    label        = Column(String(64),  nullable=True)
    api_key      = Column(String(512), nullable=False)
    api_secret   = Column(String(512), nullable=False)
    api_password = Column(String(512), nullable=True)
    added_at     = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="exchanges")
    logs = relationship("BalanceLog", back_populates="exchange", cascade="all, delete", lazy="selectin")


class BalanceLog(Base):
    __tablename__ = "balance_logs"

    id          = Column(Integer,    primary_key=True, index=True)
    user_id     = Column(Integer,    ForeignKey("users.id"),     nullable=False)
    wallet_id   = Column(Integer,    ForeignKey("wallets.id"),   nullable=True)
    exchange_id = Column(Integer,    ForeignKey("exchanges.id"), nullable=True)
    asset       = Column(String(16), nullable=False)
    amount      = Column(Float,      nullable=False)
    usd_value   = Column(Float,      nullable=True)
    recorded_at = Column(DateTime,   default=datetime.utcnow, index=True)

    user     = relationship("User",     back_populates="logs")
    wallet   = relationship("Wallet",   back_populates="logs")
    exchange = relationship("Exchange", back_populates="logs")


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id         = Column(Integer,    primary_key=True, index=True)
    user_id    = Column(Integer,    ForeignKey("users.id"), nullable=False)
    asset      = Column(String(16), nullable=False)
    direction  = Column(String(5),  nullable=False)
    target_usd = Column(Float,      nullable=False)
    triggered  = Column(Boolean,    default=False)
    created_at = Column(DateTime,   default=datetime.utcnow)

    user = relationship("User", back_populates="alerts")


async def get_user_by_telegram_id(db: AsyncSession, telegram_id: int) -> User | None:
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def create_or_update_user(
    db: AsyncSession, telegram_id: int,
    username: str | None, first_name: str | None,
) -> User:
    user = await get_user_by_telegram_id(db, telegram_id)
    if user:
        user.username   = username
        user.first_name = first_name
        await db.commit()
        await db.refresh(user)
        return user
    user = User(telegram_id=telegram_id, username=username, first_name=first_name)
    db.add(user)
    await db.flush()
    db.add(UserSettings(user_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def get_or_create_settings(db: AsyncSession, user: User) -> UserSettings:
    if user.settings:
        return user.settings
    s = UserSettings(user_id=user.id)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def get_wallet_exists(db: AsyncSession, user_id: int, chain: str, address: str) -> bool:
    result = await db.execute(
        select(Wallet).where(Wallet.user_id == user_id, Wallet.chain == chain, Wallet.address == address)
    )
    return result.scalar_one_or_none() is not None


def group_wallets_by_address(wallets: list) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for w in wallets:
        addr = w.address
        if addr not in groups:
            groups[addr] = {"label": w.label, "chains": [], "ids": [], "added_at": w.added_at}
        groups[addr]["chains"].append(w.chain)
        groups[addr]["ids"].append(w.id)
        if w.label:
            groups[addr]["label"] = w.label
    return groups


async def prune_balance_logs(db: AsyncSession, retention_days: int = 30) -> int:
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    if IS_POSTGRES:
        result = await db.execute(
            text("DELETE FROM balance_logs WHERE recorded_at < :cutoff RETURNING id"),
            {"cutoff": cutoff}
        )
        deleted = len(result.fetchall())
    else:
        result = await db.execute(
            text("DELETE FROM balance_logs WHERE recorded_at < :cutoff"),
            {"cutoff": cutoff}
        )
        deleted = result.rowcount
    await db.commit()
    return deleted


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if IS_POSTGRES:
            for stmt in [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications BOOLEAN DEFAULT TRUE",
                "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS digest_enabled BOOLEAN DEFAULT FALSE",
                "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS digest_frequency VARCHAR(8) DEFAULT 'daily'",
            ]:
                try:
                    await conn.execute(text(stmt))
                except Exception:
                    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session