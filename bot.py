#!/usr/bin/env python3
"""
================================================================================
PUMP SIGNAL BOT v3 — Balanced Hunter for Sustained Pumps
================================================================================
Optimized to catch the "bulk of the move" (1hr - 24hr pumps).

Key features:
  • Phase-gated API evaluation (saves Moralis/CoinGecko by filtering junk first)
  • Configurable max tax tolerance (default 0%)
  • $10k minimum liquidity (filters micro-cap rugs)
  • Strict security via GoPlusLabs (honeypot, taxes, whitelist, pausable, etc.)
  • Holder concentration & CEX detection as bonus points
  • No permanent dedup — tokens re-evaluated every cycle
  • Parallel evaluation (scans 300 pairs in ~3 seconds)
  • Age-aware volume math (correct for tokens <1h old)
  • Alerts include contract address and market cap

Required env vars:
  TELEGRAM_TOKEN   — Telegram bot token
  CHAT_ID          — Telegram chat ID
  MORALIS_API_KEY  — optional but recommended for holder data
  COINGECKO_API_KEY— optional for CEX detection

Optional env var:
  MAX_ALLOWED_TAX  — e.g. "0.5" to allow up to 0.5% tax (default "0")
================================================================================
"""

import asyncio
import aiohttp
import time
import os
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from collections import deque
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from fastapi import FastAPI
import uvicorn

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")

MAX_ALLOWED_TAX = float(os.getenv("MAX_ALLOWED_TAX", "0"))

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")

# Networks
NETWORKS = ["bsc", "ethereum", "base"]  # Add "robinhood" if you accept risk

# Chain mappings
CHAIN_TO_MORALIS = {"bsc": "bsc", "ethereum": "eth", "base": "base"}
CHAIN_TO_COINGECKO_PLATFORM = {
    "bsc": "binance-smart-chain",
    "ethereum": "ethereum",
    "base": "base",
}
CHAIN_TO_GOPLUS_ID = {"bsc": "56", "ethereum": "1", "base": "8453"}

# --- Timing ---
SCAN_INTERVAL = 30
HEARTBEAT_INTERVAL = 3600

# --- Minimum viability ---
MIN_LIQUIDITY_USD = 10_000.0
MIN_VOL_5M_USD = 100.0
MIN_PRICE = 1e-12

# --- Scoring weights (max total = 100) ---
# Volume & momentum (25)
VOLUME_LIQUIDITY_PTS = 10
VOLUME_5M_1H_PTS = 7
VOLUME_1H_24H_PTS = 8

# Buy pressure (20)
BUY_PRESSURE_5M_PTS = 12
BUY_PRESSURE_1H_PTS = 8

# Holder concentration (15)
HOLDER_TOP10_PTS = 8
HOLDER_TOP50_PTS = 4
HOLDER_TOP100_PTS = 3

# CEX listings (10)
CEX_LISTING_PTS = 5
CEX_PERPS_PTS = 3
CEX_TIER1_PTS = 2

# Price momentum (15)
PRICE_5M_PTS = 6
PRICE_1H_PTS = 5
PRICE_6H_PTS = 4

# Security (15)
SECURITY_PTS = 15

# --- Penalties ---
PENALTY_SELL_PRESSURE_5M = 20
PENALTY_SELL_PRESSURE_1H = 15
PENALTY_LOW_TX_5M = 10
PENALTY_LOW_TX_1H = 5

# --- Alert threshold ---
ALERT_THRESHOLD = 55

# --- Cooldown ---
RE_ALERT_COOLDOWN_HOURS = 4
SCORE_IMPROVEMENT_THRESHOLD = 15

# --- API settings ---
MAX_RETRIES = 2
API_TIMEOUT = 10
CONCURRENT_API_LIMIT = 15
COINGECKO_CALLS_PER_MINUTE = 25 if COINGECKO_API_KEY else 10

PHASE1_MIN_SCORE = 25

VERBOSE_LOGGING = os.getenv("VERBOSE_LOGGING", "false").lower() == "true"

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING & GLOBALS
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("pump_bot_v3.log"), logging.StreamHandler()],
)
logger = logging.getLogger("pump_bot_v3")

bot = Bot(token=TELEGRAM_TOKEN)
api_semaphore = asyncio.Semaphore(CONCURRENT_API_LIMIT)

security_cache: Dict[str, Tuple[dict, float]] = {}
holder_cache: Dict[str, Tuple[Tuple[float, float, float], float]] = {}
coingecko_id_map: Dict[str, Dict[str, str]] = {}
coingecko_ticker_cache: Dict[str, Tuple[dict, float]] = {}

db_conn: Optional[sqlite3.Connection] = None

total_pairs_scanned = 0
tokens_evaluated = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False

DB_PATH = "pump_bot_v3.db"

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT,
            total_score INTEGER,
            alert_time REAL,
            UNIQUE(chain, token_address)
        );
    """)
    conn.commit()
    return conn

def db_get_last_alert(chain, token_address):
    cur = db_conn.execute(
        "SELECT total_score, alert_time FROM alerts WHERE chain=? AND token_address=?",
        (chain, token_address)
    )
    row = cur.fetchone()
    return {"total_score": row[0], "alert_time": row[1]} if row else None

def db_record_alert(chain, token_address, symbol, score):
    try:
        db_conn.execute(
            "INSERT INTO alerts (chain, token_address, symbol, total_score, alert_time) "
            "VALUES (?,?,?,?,?) ON CONFLICT(chain, token_address) DO UPDATE SET "
            "total_score=excluded.total_score, alert_time=excluded.alert_time, symbol=excluded.symbol",
            (chain, token_address, symbol, score, time.time())
        )
        db_conn.commit()
    except Exception as e:
        logger.warning(f"DB alert upsert failed: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# API HELPERS & PAIR DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

coingecko_calls_this_minute = 0
coingecko_minute_reset = 0.0

async def _coingecko_rate_limit():
    global coingecko_calls_this_minute, coingecko_minute_reset
    now = time.time()
    if now > coingecko_minute_reset:
        coingecko_calls_this_minute = 0
        coingecko_minute_reset = now + 60
    coingecko_calls_this_minute += 1
    if coingecko_calls_this_minute > COINGECKO_CALLS_PER_MINUTE:
        sleep_for = coingecko_minute_reset - now + 1
        await asyncio.sleep(max(sleep_for, 0))
        coingecko_calls_this_minute = 1
        coingecko_minute_reset = time.time() + 60

async def fetch_json(session, url, headers=None, use_coingecko_limiter=False):
    if use_coingecko_limiter:
        await _coingecko_rate_limit()
    async with api_semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        return None
            except Exception:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
        return None

async def get_all_pairs(session, network):
    pairs = []
    seen = set()

    async def add_pair(p):
        addr = p.get("pairAddress")
        if addr and addr not in seen:
            seen.add(addr)
            pairs.append(p)

    # Latest pairs (primary)
    url = f"https://api.dexscreener.com/latest/dex/pairs/{network}?page=0&pageSize=300"
    data = await fetch_json(session, url)
    if data and "pairs" in data:
        for p in data["pairs"]:
            if p.get("chainId") == network:
                await add_pair(p)

    # Top boosts
    boost_data = await fetch_json(session, "https://api.dexscreener.com/token-boosts/top/v1")
    if boost_data and isinstance(boost_data, list):
        tasks = []
        for b in boost_data:
            if b.get("chainId") == network and b.get("tokenAddress"):
                tasks.append(fetch_json(session, f"https://api.dexscreener.com/tokens/v1/{network}/{b['tokenAddress']}"))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, list):
                    for p in res:
                        if p.get("chainId") == network:
                            await add_pair(p)

    # Latest profiles
    profiles_data = await fetch_json(session, "https://api.dexscreener.com/token-profiles/latest/v1")
    if profiles_data and isinstance(profiles_data, list):
        tasks = []
        for p in profiles_data:
            if p.get("chainId") == network and p.get("tokenAddress"):
                tasks.append(fetch_json(session, f"https://api.dexscreener.com/tokens/v1/{network}/{p['tokenAddress']}"))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, list):
                    for p in res:
                        if p.get("chainId") == network:
                            await add_pair(p)

    pairs.sort(key=lambda x: float(x.get("volume", {}).get("m5", 0) or 0), reverse=True)
    return pairs[:300]

# ═══════════════════════════════════════════════════════════════════════════════
# EXTERNAL APIS (GOPLUS, MORALIS, COINGECKO)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_token_security(session, chain, token):
    goplus_chain = CHAIN_TO_GOPLUS_ID.get(chain)
    if not goplus_chain:
        return None

    cache_key = f"{goplus_chain}:{token.lower()}"
    now = time.time()
    if cache_key in security_cache:
        cached, ts = security_cache[cache_key]
        if now - ts < 1800:
            return cached

    url = f"https://api.gopluslabs.io/api/v1/token_security/{goplus_chain}?contract_addresses={token.lower()}"
    data = await fetch_json(session, url)

    if not data or "result" not in data:
        security_cache[cache_key] = (None, now)
        return None

    result = data["result"].get(token.lower())
    if not result:
        security_cache[cache_key] = (None, now)
        return None

    security = {
        "is_honeypot": result.get("is_honeypot") == "1",
        "buy_tax": float(result.get("buy_tax", "0") or 0),
        "sell_tax": float(result.get("sell_tax", "0") or 0),
        "is_whitelisted": result.get("is_whitelisted") == "1",
        "is_blacklisted": result.get("is_blacklisted") == "1",
        "is_open_source": result.get("is_open_source") == "1",
        "is_proxy": result.get("is_proxy") == "1",
        "can_take_back_ownership": result.get("can_take_back_ownership") == "1",
        "owner_change_balance": result.get("owner_change_balance") == "1",
        "is_mintable": result.get("is_mintable") == "1",
        "slippage_modifiable": result.get("slippage_modifiable") == "1",
        "transfer_pausable": result.get("transfer_pausable") == "1",
        "lp_locked": result.get("is_lp_locked") == "1",
    }
    security_cache[cache_key] = (security, now)
    return security

async def get_holder_concentration(session, chain, token):
    if not MORALIS_API_KEY:
        return 0.0, 0.0, 0.0
    moralis_chain = CHAIN_TO_MORALIS.get(chain)
    if not moralis_chain:
        return 0.0, 0.0, 0.0

    cache_key = f"{moralis_chain}:{token.lower()}"
    now = time.time()
    if cache_key in holder_cache:
        (top10, top50, top100), ts = holder_cache[cache_key]
        if now - ts < 600:
            return top10, top50, top100

    url = f"https://deep-index.moralis.io/api/v2.2/erc20/{token}/owners?chain={moralis_chain}&order=DESC&limit=100"
    headers = {"X-API-Key": MORALIS_API_KEY}
    data = await fetch_json(session, url, headers=headers)

    if not data or "result" not in data:
        holder_cache[cache_key] = ((0.0, 0.0, 0.0), now)
        return 0.0, 0.0, 0.0

    holders = data.get("result", [])
    total_supply = float(data.get("total_supply") or 0)
    if total_supply == 0:
        holder_cache[cache_key] = ((0.0, 0.0, 0.0), now)
        return 0.0, 0.0, 0.0

    balances = [float(h.get("balance", 0)) for h in holders]
    top10 = sum(balances[:10])
    top50 = sum(balances[:50]) if len(balances) >= 50 else sum(balances)
    top100 = sum(balances[:100]) if len(balances) >= 100 else sum(balances)

    pct10 = (top10 / total_supply) * 100
    pct50 = (top50 / total_supply) * 100
    pct100 = (top100 / total_supply) * 100

    holder_cache[cache_key] = ((pct10, pct50, pct100), now)

    stale = [k for k, (_, ts) in holder_cache.items() if now - ts > 3600]
    for k in stale:
        del holder_cache[k]

    return pct10, pct50, pct100

KNOWN_CEX_NAMES = {
    "binance", "coinbase", "okx", "bybit", "kraken", "kucoin", "bitfinex",
    "gate.io", "mexc", "huobi", "htx", "crypto.com", "bitget", "deribit",
    "gemini", "bitstamp", "bittrex", "poloniex", "upbit", "bithumb",
    "whitebit", "phemex", "bingx", "lbk", "wazirx", "coindcx", "bitmart",
    "lbank", "coinw", "digifinex", "ascendex",
}

async def build_coingecko_id_map(session):
    global coingecko_id_map
    cache_file = "coingecko_id_cache.json"
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 6 * 3600:
            with open(cache_file) as f:
                coingecko_id_map = json.load(f)
                return coingecko_id_map

    url = "https://api.coingecko.com/api/v3/coins/list?include_platform=true"
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    data = await fetch_json(session, url, headers=headers, use_coingecko_limiter=True)
    if not data:
        return {}

    mapping = {}
    for coin in data:
        platforms = coin.get("platforms", {})
        for chain_key, contract in platforms.items():
            if contract:
                mapping.setdefault(chain_key, {})[contract.lower()] = coin["id"]

    with open(cache_file, "w") as f:
        json.dump(mapping, f)
    coingecko_id_map = mapping
    return mapping

async def get_cex_listings(session, chain, token):
    global coingecko_id_map
    if not COINGECKO_API_KEY:
        return 0, False, 0

    platform = CHAIN_TO_COINGECKO_PLATFORM.get(chain)
    if not platform:
        return 0, False, 0

    if not coingecko_id_map:
        await build_coingecko_id_map(session)

    coin_id = coingecko_id_map.get(platform, {}).get(token.lower())
    if not coin_id:
        return 0, False, 0

    now = time.time()
    if coin_id in coingecko_ticker_cache:
        cached, ts = coingecko_ticker_cache[coin_id]
        if now - ts < 3600:
            return cached.get("cex_count", 0), cached.get("has_perps", False), cached.get("tier1_count", 0)

    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/tickers"
    data = await fetch_json(session, url, headers=headers, use_coingecko_limiter=True)

    if not data or "tickers" not in data:
        coingecko_ticker_cache[coin_id] = ({"cex_count": 0, "has_perps": False, "tier1_count": 0}, now)
        return 0, False, 0

    tickers = data["tickers"]
    cex_tickers = []
    for t in tickers:
        market_name = (t.get("market") or {}).get("name", "").lower()
        if any(name in market_name for name in KNOWN_CEX_NAMES):
            cex_tickers.append(t)

    cex_count = len(cex_tickers)
    has_perps = any(
        ("perpetual" in (t.get("market") or {}).get("name", "").lower()) or
        ("futures" in (t.get("market") or {}).get("name", "").lower())
        for t in tickers
    )
    tier1_count = 0
    for t in cex_tickers:
        market_name = (t.get("market") or {}).get("name", "").lower()
        if market_name.startswith(("binance", "coinbase", "okx", "bybit", "kraken")):
            tier1_count += 1

    coingecko_ticker_cache[coin_id] = ({"cex_count": cex_count, "has_perps": has_perps, "tier1_count": tier1_count}, now)
    return cex_count, has_perps, tier1_count

# ═══════════════════════════════════════════════════════════════════════════════
# SCORING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def score_volume_liquidity(vol_5m, liquidity):
    ratio = vol_5m / liquidity if liquidity > 0 else 0
    if ratio >= 1.0:
        return VOLUME_LIQUIDITY_PTS
    elif ratio >= 0.5:
        return VOLUME_LIQUIDITY_PTS * 0.8
    elif ratio >= 0.25:
        return VOLUME_LIQUIDITY_PTS * 0.6
    elif ratio >= 0.1:
        return VOLUME_LIQUIDITY_PTS * 0.4
    else:
        return VOLUME_LIQUIDITY_PTS * 0.2

def score_5m_1h(vol_5m, vol_1h, age_minutes):
    if age_minutes is not None and age_minutes > 10 and vol_1h > 0:
        ratio = vol_5m / vol_1h
        normalized = ratio / 0.0833 if ratio > 0 else 0
        if normalized >= 8:
            return VOLUME_5M_1H_PTS
        elif normalized >= 4:
            return VOLUME_5M_1H_PTS * 0.8
        elif normalized >= 2:
            return VOLUME_5M_1H_PTS * 0.6
        elif normalized >= 1:
            return VOLUME_5M_1H_PTS * 0.4
    elif age_minutes is not None and age_minutes <= 10:
        if vol_1h > 0 and vol_5m / vol_1h > 0.9:
            return VOLUME_5M_1H_PTS * 0.4
    return 0

def score_1h_24h(vol_1h, vol_24h, age_minutes):
    if age_minutes is not None and age_minutes > 120 and vol_24h > 0:
        ratio = vol_1h / vol_24h
        normalized = ratio / 0.0417 if ratio > 0 else 0
        if normalized >= 8:
            return VOLUME_1H_24H_PTS
        elif normalized >= 4:
            return VOLUME_1H_24H_PTS * 0.8
        elif normalized >= 2:
            return VOLUME_1H_24H_PTS * 0.6
        elif normalized >= 1:
            return VOLUME_1H_24H_PTS * 0.4
    elif age_minutes is not None and age_minutes <= 120:
        if vol_24h > 0 and vol_1h / vol_24h > 0.8:
            return VOLUME_1H_24H_PTS * 0.5
    return 0

def score_holder(top10, top50, top100):
    pts = 0
    if top10 >= 80:
        pts += HOLDER_TOP10_PTS
    elif top10 >= 60:
        pts += HOLDER_TOP10_PTS * 0.75
    elif top10 >= 40:
        pts += HOLDER_TOP10_PTS * 0.5
    elif top10 >= 25:
        pts += HOLDER_TOP10_PTS * 0.25

    if top50 >= 90:
        pts += HOLDER_TOP50_PTS
    elif top50 >= 75:
        pts += HOLDER_TOP50_PTS * 0.75
    elif top50 >= 60:
        pts += HOLDER_TOP50_PTS * 0.5

    if top100 >= 95:
        pts += HOLDER_TOP100_PTS
    elif top100 >= 85:
        pts += HOLDER_TOP100_PTS * 0.75
    elif top100 >= 70:
        pts += HOLDER_TOP100_PTS * 0.5
    return pts

def score_cex(cex_count, has_perps, tier1):
    pts = min(cex_count, CEX_LISTING_PTS)
    if has_perps:
        pts += CEX_PERPS_PTS
    pts += min(tier1, CEX_TIER1_PTS)
    return pts

def score_price(chg_5m, chg_1h, chg_6h, age_minutes):
    pts = 0
    if chg_5m > 0:
        if chg_5m >= 50:
            pts += PRICE_5M_PTS
        elif chg_5m >= 20:
            pts += PRICE_5M_PTS * 0.7
        elif chg_5m >= 5:
            pts += PRICE_5M_PTS * 0.4

    if chg_1h > 0 and age_minutes is not None and age_minutes > 60:
        if chg_1h >= 100:
            pts += PRICE_1H_PTS
        elif chg_1h >= 50:
            pts += PRICE_1H_PTS * 0.7
        elif chg_1h >= 10:
            pts += PRICE_1H_PTS * 0.4

    if chg_6h > 0 and age_minutes is not None and age_minutes > 360:
        if chg_6h >= 200:
            pts += PRICE_6H_PTS
        elif chg_6h >= 100:
            pts += PRICE_6H_PTS * 0.7
        elif chg_6h >= 50:
            pts += PRICE_6H_PTS * 0.4
    return pts

# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN EVALUATION (PHASE-GATED)
# ═══════════════════════════════════════════════════════════════════════════════

async def evaluate_token(session, pair):
    global tokens_evaluated

    chain = pair.get("chainId")
    base_token = pair.get("baseToken", {}) or {}
    token = base_token.get("address")
    pair_id = pair.get("pairAddress")
    symbol = base_token.get("symbol", "???")
    name = base_token.get("name", "Unknown")

    if not all([chain, token, pair_id]):
        return None

    tokens_evaluated += 1

    liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
    if liquidity < MIN_LIQUIDITY_USD:
        return None

    price = float(pair.get("priceUsd") or 0)
    if price < MIN_PRICE:
        return None

    vol_5m = float(pair.get("volume", {}).get("m5") or 0)
    if vol_5m < MIN_VOL_5M_USD:
        return None

    vol_1h = float(pair.get("volume", {}).get("h1") or 0)
    vol_24h = float(pair.get("volume", {}).get("h24") or 0)
    vol_6h = float(pair.get("volume", {}).get("h6") or 0)

    txns_5m = pair.get("txns", {}).get("m5", {}) or {}
    buys_5m = int(txns_5m.get("buys", 0))
    sells_5m = int(txns_5m.get("sells", 0))

    txns_1h = pair.get("txns", {}).get("h1", {}) or {}
    buys_1h = int(txns_1h.get("buys", 0))
    sells_1h = int(txns_1h.get("sells", 0))

    chg_5m = float(pair.get("priceChange", {}).get("m5") or 0)
    chg_1h = float(pair.get("priceChange", {}).get("h1") or 0)
    chg_6h = float(pair.get("priceChange", {}).get("h6") or 0)

    market_cap = float(pair.get("marketCap") or 0)

    pair_created = pair.get("pairCreatedAt")
    age_minutes = (time.time() - pair_created / 1000) / 60 if pair_created else None

    score = 0
    penalties = 0

    # 1. Volume components
    score += score_volume_liquidity(vol_5m, liquidity)
    score += score_5m_1h(vol_5m, vol_1h, age_minutes)
    score += score_1h_24h(vol_1h, vol_24h, age_minutes)

    # 2. Buy pressure 5m
    total_5m = buys_5m + sells_5m
    if total_5m > 0:
        buy_ratio_5m = buys_5m / total_5m
        if buy_ratio_5m >= 0.85:
            score += BUY_PRESSURE_5M_PTS
        elif buy_ratio_5m >= 0.70:
            score += BUY_PRESSURE_5M_PTS * 0.75
        elif buy_ratio_5m >= 0.55:
            score += BUY_PRESSURE_5M_PTS * 0.5
        elif buy_ratio_5m >= 0.45:
            score += BUY_PRESSURE_5M_PTS * 0.25
        if buy_ratio_5m < 0.35:
            penalties += PENALTY_SELL_PRESSURE_5M
        if total_5m < 5:
            penalties += PENALTY_LOW_TX_5M
    else:
        penalties += PENALTY_LOW_TX_5M

    # 3. Buy pressure 1h (only if age > 60 min)
    total_1h = buys_1h + sells_1h
    if age_minutes is not None and age_minutes > 60 and total_1h > 0:
        buy_ratio_1h = buys_1h / total_1h
        if buy_ratio_1h >= 0.80:
            score += BUY_PRESSURE_1H_PTS
        elif buy_ratio_1h >= 0.65:
            score += BUY_PRESSURE_1H_PTS * 0.75
        elif buy_ratio_1h >= 0.50:
            score += BUY_PRESSURE_1H_PTS * 0.5
        if buy_ratio_1h < 0.35:
            penalties += PENALTY_SELL_PRESSURE_1H
        if total_1h < 10:
            penalties += PENALTY_LOW_TX_1H

    # 4. Price momentum
    score += score_price(chg_5m, chg_1h, chg_6h, age_minutes)

    # 5. Security
    security = await get_token_security(session, chain, token)
    if security:
        if security.get("is_honeypot"):
            return None
        if security.get("buy_tax", 0) > MAX_ALLOWED_TAX or security.get("sell_tax", 0) > MAX_ALLOWED_TAX:
            return None
        if any([
            security.get("is_whitelisted"),
            security.get("is_blacklisted"),
            security.get("is_proxy"),
            security.get("can_take_back_ownership"),
            security.get("owner_change_balance"),
            security.get("is_mintable"),
            security.get("slippage_modifiable"),
            security.get("transfer_pausable"),
        ]):
            return None
        score += SECURITY_PTS
    else:
        pass

    # Phase gate
    base_score = score - penalties
    if base_score < PHASE1_MIN_SCORE:
        if VERBOSE_LOGGING:
            logger.info(f"Phase gate skip {symbol}: base_score={base_score}")
        return None

    # 6. Holder concentration
    top10, top50, top100 = await get_holder_concentration(session, chain, token)
    holder_pts = score_holder(top10, top50, top100)
    score += holder_pts

    # 7. CEX listings
    cex_count, has_perps, tier1 = await get_cex_listings(session, chain, token)
    cex_pts = score_cex(cex_count, has_perps, tier1)
    score += cex_pts

    total_score = max(0, score - penalties)

    if VERBOSE_LOGGING:
        logger.info(
            f"{symbol} score={total_score} | liq_ratio={vol_5m/liquidity if liquidity>0 else 0:.2f} "
            f"5m/1h={vol_5m/vol_1h if vol_1h>0 else 0:.2f} 1h/24h={vol_1h/vol_24h if vol_24h>0 else 0:.2f} "
            f"buy5m={buys_5m}/{sells_5m} buy1h={buys_1h}/{sells_1h} age={age_minutes:.0f}m "
            f"top10={top10:.1f}% cex={cex_count} perps={has_perps} base={base_score}"
        )

    if total_score < ALERT_THRESHOLD:
        return None

    return {
        "chain": chain,
        "token_address": token,
        "symbol": symbol,
        "name": name,
        "pair_address": pair_id,
        "total_score": total_score,
        "vol_5m": vol_5m,
        "liquidity": liquidity,
        "market_cap": market_cap,
        "buys_5m": buys_5m,
        "sells_5m": sells_5m,
        "buys_1h": buys_1h,
        "sells_1h": sells_1h,
        "chg_5m": chg_5m,
        "chg_1h": chg_1h,
        "chg_6h": chg_6h,
        "security": security,
        "holder_pct": (top10, top50, top100),
        "cex_count": cex_count,
        "has_perps": has_perps,
        "tier1": tier1,
        "age_minutes": age_minutes,
        "dex_url": f"https://dexscreener.com/{chain}/{pair_id}",
    }

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERT
# ═══════════════════════════════════════════════════════════════════════════════

async def send_alert(alert):
    global alerts_sent
    sec = alert.get("security") or {}
    pct10, pct50, pct100 = alert.get("holder_pct", (0,0,0))

    buy_pct_5m = (alert['buys_5m'] / (alert['buys_5m'] + alert['sells_5m']) * 100) if (alert['buys_5m'] + alert['sells_5m']) > 0 else 0
    buy_pct_1h = (alert['buys_1h'] / (alert['buys_1h'] + alert['sells_1h']) * 100) if (alert['buys_1h'] + alert['sells_1h']) > 0 else 0

    text = (
        f"🚨 <b>PUMP SIGNAL — Score {alert['total_score']}/100</b>\n\n"
        f"<b>{alert['name']}</b> ({alert['symbol']})\n"
        f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n"
        f"🕒 Age: <b>{alert['age_minutes']:.0f} min</b>\n\n"
        f"<b>💰 Liquidity:</b> ${alert['liquidity']:,.0f}\n"
        f"<b>💎 Market Cap:</b> ${alert['market_cap']:,.0f}\n"
        f"<b>📈 Volume (5m):</b> ${alert['vol_5m']:,.0f}\n"
        f"<b>💹 Price Δ 5m:</b> {alert['chg_5m']:+.1f}%\n"
        f"<b>💹 Price Δ 1h:</b> {alert['chg_1h']:+.1f}%\n"
        f"<b>💹 Price Δ 6h:</b> {alert['chg_6h']:+.1f}%\n\n"
        f"<b>🟢 Buy Pressure 5m:</b> {buy_pct_5m:.1f}% ({alert['buys_5m']}B / {alert['sells_5m']}S)\n"
        f"<b>🟢 Buy Pressure 1h:</b> {buy_pct_1h:.1f}% ({alert['buys_1h']}B / {alert['sells_1h']}S)\n\n"
        f"<b>🔒 Security:</b> {'✅' if security else '❓'}\n"
        f"  Honeypot: {'✅ No' if not security or not security.get('is_honeypot') else '❌ YES'}\n"
        f"  Buy Tax: {security.get('buy_tax', 0):.1f}%\n"
        f"  Sell Tax: {security.get('sell_tax', 0):.1f}%\n"
        f"  Whitelisted: {'❌ YES' if security and security.get('is_whitelisted') else '✅ No'}\n"
        f"  Transfer Pausable: {'❌ YES' if security and security.get('transfer_pausable') else '✅ No'}\n\n"
        f"<b>👥 Holder Concentration:</b>\n"
        f"  Top 10: {pct10:.1f}%\n"
        f"  Top 50: {pct50:.1f}%\n"
        f"  Top 100: {pct100:.1f}%\n\n"
        f"<b>🏛 CEX Listings:</b> {alert['cex_count']} (perps: {'✅' if alert['has_perps'] else '❌'}, tier1: {alert['tier1']})\n\n"
        f"📝 <b>Contract:</b> <code>{alert['token_address']}</code>\n\n"
        f"🔗 <a href='{alert['dex_url']}'>DexScreener</a>"
    )

    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        alerts_sent += 1
        logger.info(f"✅ ALERT #{alerts_sent} sent → {alert['symbol']} score={alert['total_score']}")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════════════════════

async def bot_task():
    global start_time, last_heartbeat_time, total_pairs_scanned, db_conn

    db_conn = init_db()
    start_time = time.time()
    last_heartbeat_time = start_time

    logger.info("🚀 Pump Bot v3 starting...")

    async with aiohttp.ClientSession() as session:
        if COINGECKO_API_KEY:
            await build_coingecko_id_map(session)

        # Startup message
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"✅ <b>Pump Bot v3</b> started\n"
                    f"Scan interval: {SCAN_INTERVAL}s\n"
                    f"Networks: {', '.join(NETWORKS).upper()}\n"
                    f"Min liquidity: ${MIN_LIQUIDITY_USD:,.0f}\n"
                    f"Alert threshold: {ALERT_THRESHOLD}\n"
                    f"Max allowed tax: {MAX_ALLOWED_TAX}%\n"
                    f"Moralis: {'✅' if MORALIS_API_KEY else '❌'} | "
                    f"CoinGecko: {'✅' if COINGECKO_API_KEY else '⚪ free tier'}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

        while not shutdown_flag:
            cycle_start = time.time()
            cycle_alerts = 0

            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                uptime = (time.time() - start_time) / 3600
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🫀 <b>Bot v3 Heartbeat</b>\nUptime: {uptime:.1f}h\nPairs scanned: {total_pairs_scanned}\nAlerts sent: {alerts_sent}",
                    parse_mode=ParseMode.HTML
                )
                last_heartbeat_time = time.time()

            for network in NETWORKS:
                if shutdown_flag:
                    break
                pairs = await get_all_pairs(session, network)
                total_pairs_scanned += len(pairs)

                tasks = [evaluate_token(session, p) for p in pairs]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if not result or isinstance(result, Exception):
                        continue

                    last_alert = db_get_last_alert(result["chain"], result["token_address"])
                    should_alert = True
                    if last_alert:
                        hours_since = (time.time() - last_alert["alert_time"]) / 3600
                        if hours_since < RE_ALERT_COOLDOWN_HOURS:
                            if (result["total_score"] - last_alert["total_score"]) < SCORE_IMPROVEMENT_THRESHOLD:
                                should_alert = False

                    if should_alert:
                        await send_alert(result)
                        db_record_alert(result["chain"], result["token_address"], result["symbol"], result["total_score"])
                        cycle_alerts += 1
                        await asyncio.sleep(1)

            cycle_duration = time.time() - cycle_start
            logger.info(f"🔄 Cycle complete in {cycle_duration:.1f}s. Alerts: {cycle_alerts}. Sleeping {SCAN_INTERVAL}s...")
            await asyncio.sleep(max(0, SCAN_INTERVAL - cycle_duration))

# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI LIFESPAN
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app):
    global shutdown_flag
    shutdown_flag = False
    task = asyncio.create_task(bot_task())
    yield
    shutdown_flag = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        logger.info("Bot task cancelled cleanly")
    if db_conn:
        db_conn.close()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health_check():
    return {
        "status": "alive",
        "alerts_sent": alerts_sent,
        "pairs_scanned": total_pairs_scanned,
        "threshold": ALERT_THRESHOLD,
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting Pump Bot v3 on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
