"""
config.py — all settings loaded from .env
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _clean(val: str) -> str:
    """Strip inline comments from env values."""
    return val.split("#")[0].strip()


# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

_raw_ids = _clean(os.getenv("ALLOWED_USER_IDS", ""))
ALLOWED_USER_IDS: set[int] = (
    {int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip().isdigit()}
    if _raw_ids else set()
)

_raw_admins = _clean(os.getenv("ADMIN_USER_IDS", ""))
ADMIN_USER_IDS: set[int] = (
    {int(uid.strip()) for uid in _raw_admins.split(",") if uid.strip().isdigit()}
    if _raw_admins else set()
)

# ── Server IP (for exchange API key IP whitelisting) ─────────────────────────
# Set this to your hosting provider's static outbound IP(s).
# Render:  Dashboard → your service → Outbound IPs
# Railway: Project Settings → Networking → Static IP (paid plan)
# Fly.io:  fly ips list
# VPS:     curl ipify.org
#
# Multiple IPs: comma-separated   e.g. SERVER_IP=1.2.3.4,5.6.7.8
# Leave blank to auto-detect at startup (works on most providers).
SERVER_IP: str = _clean(os.getenv("SERVER_IP", ""))

# ── Blockchain RPCs ───────────────────────────────────────────────────────────
ALCHEMY_API_KEY: str = os.getenv("ALCHEMY_API_KEY", "")

ETH_RPC_URL: str = os.getenv(
    "ETH_RPC_URL",
    f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    if ALCHEMY_API_KEY else "https://cloudflare-eth.com",
)
BNB_RPC_URL: str      = os.getenv("BNB_RPC_URL",      "https://bsc-dataseed1.binance.org/")
POLYGON_RPC_URL: str  = os.getenv("POLYGON_RPC_URL",  "https://polygon-rpc.com/")
ARBITRUM_RPC_URL: str = os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
OPTIMISM_RPC_URL: str = os.getenv("OPTIMISM_RPC_URL", "https://mainnet.optimism.io")
BASE_RPC_URL: str     = os.getenv("BASE_RPC_URL",     "https://mainnet.base.org")
AVAX_RPC_URL: str     = os.getenv("AVAX_RPC_URL",     "https://api.avax.network/ext/bc/C/rpc")
SOLANA_RPC_URL: str   = os.getenv("SOLANA_RPC_URL",   "https://api.mainnet-beta.solana.com")
BITCOIN_API_URL: str  = "https://blockstream.info/api"

# ── Database ──────────────────────────────────────────────────────────────────
def _fix_db_url(url: str) -> str:
    import re
    url = _clean(url)
    url = re.sub(r'\?.*', '', url)
    for old, new in [
        ("postgresql://",         "postgresql+asyncpg://"),
        ("postgres://",           "postgresql+asyncpg://"),
        ("postgresql+psycopg2://","postgresql+asyncpg://"),
    ]:
        if url.startswith(old):
            url = url.replace(old, new, 1)
            break
    return url

DATABASE_URL: str = _fix_db_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./portfolio.db"))
IS_POSTGRES: bool = DATABASE_URL.startswith("postgresql")

# ── Encryption ────────────────────────────────────────────────────────────────
ENCRYPTION_KEY: str | None = os.getenv("ENCRYPTION_KEY")

# ── Polling & limits ──────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS: int     = int(_clean(os.getenv("POLL_INTERVAL_SECONDS",     "300")))
BALANCELOG_RETENTION_DAYS: int = int(_clean(os.getenv("BALANCELOG_RETENTION_DAYS", "30")))
RATE_LIMIT_PER_MINUTE: int     = int(_clean(os.getenv("RATE_LIMIT_PER_MINUTE",     "4")))
DIGEST_HOUR_UTC: int           = int(_clean(os.getenv("DIGEST_HOUR_UTC",           "8")))