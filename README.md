# 📊 Crypto Portfolio Tracker Bot

A self-hosted Telegram bot that tracks your crypto portfolio across **9 blockchains** and **10 exchanges** in real time — all from a single chat interface.

Your API keys never leave your server. Everything is encrypted at rest.

🤖 **Live demo:** [@portfoliomanage_track_bot](https://t.me/portfoliomanage_track_bot)
---

## ✨ Features

- **Multi-chain wallet tracking** — Ethereum, BNB Chain, Polygon, Arbitrum, Optimism, Base, Avalanche, Solana, Bitcoin
- **ERC-20 token balances** — USDT, LINK, UNI, and any other tokens in your EVM wallets (via Alchemy)
- **10 exchange integrations** — Binance, Bybit, OKX, KuCoin, Coinbase, Kraken, Bitget, Gate.io, MEXC, HTX
- **Live USD values** — prices via CoinGecko + Binance ticker fallback
- **Balance change alerts** — notified when deposits or withdrawals hit any tracked wallet or exchange
- **Price alerts** — set a target price and get notified when an asset crosses it
- **Portfolio history** — 7-day sparkline chart, peak/low, and % change
- **P&L tracking** — profit and loss vs your first recorded snapshot
- **Portfolio dominance** — see each asset's % share of your total
- **Daily/weekly digest** — scheduled portfolio summary sent to your Telegram
- **Balance filter** — hide dust assets below a USD threshold
- **IP whitelisting guide** — built-in setup guide shows your server IP before you create API keys
- **Balance cache** — serves cached data on concurrent requests, never shows $0
- **Rate limiting** — prevents API hammering
- **BalanceLog pruning** — automatic cleanup keeps your database lean

---

## 🖥️ Commands

| Command | Description |
|---|---|
| `/balances` | Live balances across all wallets and exchanges |
| `/portfolio` | Total USD value grouped by asset |
| `/refresh` | Force-clear cache and fetch fresh data |
| `/history` | 7-day balance sparkline |
| `/pnl` | Profit & loss vs first snapshot |
| `/dominance` | Asset allocation % breakdown |
| `/pricealert` | Set a price alert for any asset |
| `/serverip` | Show this server's outbound IP (for exchange whitelisting) |
| `/addwallet` | Track a wallet address |
| `/addchain` | Add more chains to an existing wallet |
| `/addexchange` | Connect an exchange with read-only API keys |
| `/updateexchange` | Update API keys after changing IP whitelist |
| `/mywallets` | List tracked wallets |
| `/myexchanges` | List connected exchanges |
| `/removewallet` | Remove a wallet or specific chain |
| `/removeexchange` | Disconnect an exchange |
| `/editwallet` | Rename a wallet |
| `/editexchange` | Rename an exchange |
| `/settings` | Configure filters, alert thresholds, digest |
| `/cancel` | Cancel any ongoing action |

---

## 🔧 Tech Stack

- **Python 3.12** — async throughout
- **aiogram 3.x** — Telegram bot framework
- **SQLAlchemy 2.0 + asyncpg** — async Postgres ORM
- **CCXT** — unified exchange API (10 exchanges)
- **Web3.py** — EVM chain balance fetching
- **Alchemy** — ERC-20 token balance detection
- **APScheduler** — background polling and digest scheduler
- **Fernet encryption** — API keys encrypted at rest
- **aiohttp** — health server for cloud deployment

---

## 🚀 Self-Hosting

### Prerequisites

- Python 3.12+
- PostgreSQL database (Supabase free tier works well)
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Alchemy API key (free tier) for ERC-20 token support

### 1. Clone and install

```bash
git clone https://github.com/sosten6/portfolio-track.git
cd portfolio-track
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create `.env`

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
DATABASE_URL=postgresql+asyncpg://user:pass@host/db
ENCRYPTION_KEY=your_fernet_key_here
ALCHEMY_API_KEY=your_alchemy_key_here

# Optional — restrict bot to specific Telegram user IDs
ALLOWED_USER_IDS=123456789,987654321

# Optional — your server's outbound IP(s) for exchange whitelisting guides
# Find it in Railway/Render dashboard or run: curl ipify.org
SERVER_IP=1.2.3.4

# Polling interval in seconds (300 = 5 minutes recommended)
POLL_INTERVAL_SECONDS=300
```

Generate an encryption key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Run

```bash
python bot.py
```

### 4. Deploy to Railway / Render

The bot includes a built-in health server that binds to `$PORT` automatically — no extra configuration needed for Railway or Render web services.

Set all the `.env` variables in your platform's environment variables panel, push to GitHub, and connect the repo.

**Recommended settings for free-tier hosting:**
- `POLL_INTERVAL_SECONDS=300` — prevents database quota exhaustion
- Use Supabase free tier for Postgres (no egress quota lock-outs)

---

## 🔐 Security

- Exchange API keys are encrypted using **Fernet symmetric encryption** before being stored in the database
- The bot only requests **read-only** API key permissions — no trading or withdrawal access
- Your keys never leave your own server
- Telegram user ID allowlist via `ALLOWED_USER_IDS` for private deployments

---

## ⚙️ Supported Exchanges

| Exchange | Notes |
|---|---|
| Binance | Spot + Earn + Funding |
| Bybit | Unified Trading Account |
| OKX | Trading + Funding |
| KuCoin | Spot (requires passphrase) |
| Coinbase | Spot |
| Kraken | Spot |
| Bitget | Spot (requires passphrase) |
| Gate.io | Spot |
| MEXC | Spot |
| HTX (Huobi) | Spot |

> **Note:** Binance returns HTTP 451 (geo-blocked) from US-based hosting regions (Render Oregon, some AWS regions). Use a non-US server or a different exchange for Binance tracking on hosted deployments.

---

## 🌐 Supported Chains

| Chain | Native Token | ERC-20 Tokens |
|---|---|---|
| Ethereum | ETH | ✅ via Alchemy |
| BNB Chain | BNB | — |
| Polygon | POL | ✅ via Alchemy |
| Arbitrum | ETH | ✅ via Alchemy |
| Optimism | ETH | ✅ via Alchemy |
| Base | ETH | ✅ via Alchemy |
| Avalanche | AVAX | — |
| Solana | SOL | — |
| Bitcoin | BTC | — |

---

## 📁 Project Structure

```
crypto-portfolio-bot/
├── bot.py                  # Main bot — handlers, FSM flows, startup
├── config.py               # Environment variable loading
├── db.py                   # SQLAlchemy models and helpers
├── crypto.py               # Fernet encrypt/decrypt for API keys
├── requirements.txt
└── services/
    ├── wallets.py          # Multi-chain balance fetching + ERC-20 tokens
    ├── exchanges.py        # CCXT exchange balance fetching with caching
    ├── prices.py           # CoinGecko + Binance price lookup
    ├── notifications.py    # Balance poller, price alerts, digest sender
    └── cache.py            # TTL balance cache + rate limiter
```

---

## 📄 License

MIT — do whatever you want with it.

---

## 🤝 Contributing

<<<<<<< HEAD
PRs welcome. If you add support for a new chain or exchange, open a pull request.
=======
PRs welcome. If you add support for a new chain or exchange, open a pull request.
>>>>>>> 6f50e133ff65b2c65c783115381891b072738b03
