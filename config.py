"""
config.py — all settings loaded from .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

# Strip inline comments that users might add after the value
def _clean(val: str) -> str:
    return val.split("#")[0].strip()

_raw_ids = _clean(os.getenv("ALLOWED_USER_IDS", ""))
ALLOWED_USER_IDS: set[int] = (
    {int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip().isdigit()}
    if _raw_ids else set()
)

# ── Blockchain RPCs ───────────────────────────────────────────────────────────
ALCHEMY_API_KEY: str = os.getenv("ALCHEMY_API_KEY", "")

ETH_RPC_URL: str = os.getenv(
    "ETH_RPC_URL",
    f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}" if ALCHEMY_API_KEY
    else "https://cloudflare-eth.com"
)
BNB_RPC_URL: str      = os.getenv("BNB_RPC_URL",      "https://bsc-dataseed1.binance.org/")
POLYGON_RPC_URL: str  = os.getenv("POLYGON_RPC_URL",  "https://polygon-rpc.com/")
ARBITRUM_RPC_URL: str = os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
OPTIMISM_RPC_URL: str = os.getenv("OPTIMISM_RPC_URL", "https://mainnet.optimism.io")
BASE_RPC_URL: str     = os.getenv("BASE_RPC_URL",     "https://mainnet.base.org")
AVAX_RPC_URL: str     = os.getenv("AVAX_RPC_URL",     "https://api.avax.network/ext/bc/C/rpc")
SOLANA_RPC_URL: str   = os.getenv("SOLANA_RPC_URL",   "https://api.mainnet-beta.solana.com")
BITCOIN_API_URL: str  = "https://blockstream.info/api"

# ── Database — always asyncpg for PostgreSQL ──────────────────────────────────
_raw_db = _clean(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./portfolio.db"))

# Auto-fix common driver mistakes so the user doesn't have to worry about it
def _fix_db_url(url: str) -> str:
    # Strip any ssl/sslmode args — we handle SSL via connect_args
    import re
    url = re.sub(r'\?.*', '', url)  # remove query string entirely; we add ssl ourselves
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    return url

DATABASE_URL: str = _fix_db_url(_raw_db)
IS_POSTGRES: bool = DATABASE_URL.startswith("postgresql")

# ── Encryption ────────────────────────────────────────────────────────────────
ENCRYPTION_KEY: str | None = os.getenv("ENCRYPTION_KEY")

# ── Polling ───────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS: int = int(_clean(os.getenv("POLL_INTERVAL_SECONDS", "300")))