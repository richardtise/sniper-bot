#!/usr/bin/env python3
"""
================================================================================
PUMP SIGNAL BOT v2 — Multi-Signal Scoring Engine
================================================================================
Scans BSC, Ethereum, and Base for early pump signals using a weighted scoring
system instead of hard boolean filters. Detects accumulation through:

  • Multi-timeframe volume momentum (5m, 1h, 6h, 24h)
  • Holder concentration (top 10 / 50 / 100) via Moralis
  • CEX listing count & perps/futures detection via CoinGecko
  • Buy/sell pressure ratios
  • Sustained price momentum

Alerts fire when a token's cumulative score crosses the configurable threshold.
All APIs used are free-tier compatible.

Required env vars:
  TELEGRAM_TOKEN   — Telegram bot token
  CHAT_ID          — Telegram chat ID for alerts
  MORALIS_API_KEY  — Moralis API key (free tier, for holder data)
  COINGECKO_API_KEY— CoinGecko demo API key for higher rate limits

Author: Redesign from v1 hard-filter architecture
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
from datetime import datetime, timedelta
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

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")

NETWORKS = ["bsc", "ethereum", "base"]

# Moralis chain slug mapping
CHAIN_TO_MORALIS = {"bsc": "bsc", "ethereum": "eth", "base": "base"}

# CoinGecko platform mapping (for contract address → CoinGecko ID lookup)
CHAIN_TO_COINGECKO_PLATFORM = {
    "bsc": "binance-smart-chain",
    "ethereum": "ethereum",
    "base": "base",
}

# --- Timing ---
SCAN_INTERVAL = 60          # seconds between full scan cycles
HEARTBEAT_INTERVAL = 3600   # seconds between heartbeats

# --- Minimum viability filters (hard cuts to avoid wasting API calls) ---
MIN_LIQUIDITY_USD = 3_000.0     # Absolute minimum to consider a pair
MIN_VOL_5M_USD = 200.0          # Minimum 5m volume (avoid dead pairs)
MIN_PRICE = 1e-12               # Ignore effectively zero-priced tokens

# --- Scoring: Alert threshold (0–100) ---
ALERT_THRESHOLD = 50
RE_ALERT_COOLDOWN_HOURS = 6     # Don't re-alert the same token within N hours
SCORE_IMPROVEMENT_THRESHOLD = 15  # Re-alert if score jumps by this much

# --- Volume scoring (0–35 pts) ---
# Ratios are compared against "expected" baseline (timeframe proportion)
VOL_5M_1H_WEIGHT = 15           # max pts: 5m vol vs 1h vol
VOL_1H_6H_WEIGHT = 10           # max pts: 1h vol vs 6h average
VOL_6H_24H_WEIGHT = 10          # max pts: 6h vol vs 24h average

# Ratio thresholds for volume scoring
VOL_RATIO_LOW = 1.5             # 1.5x expected = some activity
VOL_RATIO_MED = 3.0             # 3x expected = significant
VOL_RATIO_HIGH = 6.0            # 6x expected = massive spike
VOL_RATIO_EXTREME = 12.0        # 12x expected = explosive

# --- Holder concentration scoring (0–25 pts) ---
HOLDER_TOP10_WEIGHT = 15
HOLDER_TOP50_WEIGHT = 6
HOLDER_TOP100_WEIGHT = 4

# Pct thresholds for holder scoring
HOLDER_PCT_LOW = 50.0
HOLDER_PCT_MED = 70.0
HOLDER_PCT_HIGH = 85.0
HOLDER_PCT_EXTREME = 95.0

# --- CEX listing scoring (0–25 pts) ---
CEX_PER_CEX_POINTS = 3          # Points per CEX listing (capped)
CEX_MAX_LISTING_POINTS = 12     # Cap for listing count
CEX_PERPS_POINTS = 10           # Bonus if perps/futures exist
CEX_MAJOR_BONUS = 3             # Extra for tier-1 CEX (Binance, Coinbase, OKX, Bybit)

# --- Price momentum scoring (0–15 pts) ---
PRICE_5M_WEIGHT = 5
PRICE_1H_WEIGHT = 5
PRICE_6H_WEIGHT = 5

# Price change thresholds (%)
PRICE_CHG_LOW = 3.0
PRICE_CHG_MED = 10.0
PRICE_CHG_HIGH = 25.0
PRICE_CHG_EXTREME = 50.0

# --- API behaviour ---
MAX_RETRIES = 3
API_TIMEOUT = 15
CONCURRENT_API_LIMIT = 5
COINGECKO_CALLS_PER_MINUTE = 25 if COINGECKO_API_KEY else 10

# --- Debug ---
VERBOSE_SCORING = os.getenv("VERBOSE_SCORING", "false").lower() == "true"

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pump_bot_v2.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("pump_bot_v2")

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════════════════

bot = Bot(token=TELEGRAM_TOKEN)

# Rate limiters
api_semaphore = asyncio.Semaphore(CONCURRENT_API_LIMIT)

# CoinGecko rate limiter — we enforce our own RPM limit
coingecko_last_call = 0.0
coingecko_calls_this_minute = 0
coingecko_minute_reset = 0.0

# Seen pairs (LRU dedup)
_seen_pairs_order: deque = deque(maxlen=50_000)
_seen_pairs_set: set = set()

# Caches
holder_cache: Dict[str, Tuple[float, float, float, float]] = {}  # key→(top10,top50,top100,ts)
coingecko_id_map: Dict[str, Dict[str, str]] = {}  # chain → {contract: coin_id}
coingecko_ticker_cache: Dict[str, Tuple[dict, float]] = {}  # coin_id → (tickers, ts)

# SQLite DB connection
db_conn: Optional[sqlite3.Connection] = None

# Stats
total_pairs_scanned = 0
tokens_evaluated = 0
tokens_scored = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False
hourly_scan_records: deque = deque(maxlen=200)

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

DB_PATH = "pump_bot_v2.db"


def init_db() -> sqlite3.Connection:
    """Initialise SQLite database for score tracking and alert history."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT,
            name TEXT,
            total_score INTEGER,
            vol_score INTEGER,
            holder_score INTEGER,
            cex_score INTEGER,
            price_score INTEGER,
            top10_pct REAL,
            top50_pct REAL,
            top100_pct REAL,
            cex_count INTEGER,
            has_perps INTEGER,
            major_cex_count INTEGER,
            price_chg_5m REAL,
            price_chg_1h REAL,
            price_chg_6h REAL,
            vol_5m REAL,
            vol_1h REAL,
            vol_6h REAL,
            buy_pressure_5m REAL,
            liquidity REAL,
            created_at REAL
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT,
            name TEXT,
            total_score INTEGER,
            alert_time REAL,
            UNIQUE(chain, token_address)
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
            ON token_snapshots(chain, token_address, created_at);

        CREATE INDEX IF NOT EXISTS idx_alerts_lookup
            ON alerts(chain, token_address);
        """
    )
    conn.commit()
    return conn


def db_insert_snapshot(data: dict):
    """Persist a token's score snapshot for trend analysis."""
    if db_conn is None:
        return
    try:
        db_conn.execute(
            """
            INSERT INTO token_snapshots
            (chain, token_address, symbol, name, total_score,
             vol_score, holder_score, cex_score, price_score,
             top10_pct, top50_pct, top100_pct,
             cex_count, has_perps, major_cex_count,
             price_chg_5m, price_chg_1h, price_chg_6h,
             vol_5m, vol_1h, vol_6h, buy_pressure_5m, liquidity, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["chain"], data["token_address"], data.get("symbol"),
                data.get("name"), data["total_score"],
                data["vol_score"], data["holder_score"],
                data["cex_score"], data["price_score"],
                data.get("top10_pct"), data.get("top50_pct"),
                data.get("top100_pct"), data.get("cex_count", 0),
                1 if data.get("has_perps") else 0,
                data.get("major_cex_count", 0),
                data.get("price_chg_5m", 0), data.get("price_chg_1h", 0),
                data.get("price_chg_6h", 0), data.get("vol_5m", 0),
                data.get("vol_1h", 0), data.get("vol_6h", 0),
                data.get("buy_pressure_5m", 0), data.get("liquidity", 0),
                time.time(),
            ),
        )
        db_conn.commit()
    except Exception as e:
        logger.warning(f"DB snapshot insert failed: {e}")


def db_get_last_alert(chain: str, token_address: str) -> Optional[dict]:
    """Get the most recent alert for a token (if any)."""
    if db_conn is None:
        return None
    cur = db_conn.execute(
        "SELECT total_score, alert_time FROM alerts WHERE chain=? AND token_address=?",
        (chain, token_address),
    )
    row = cur.fetchone()
    if row:
        return {"total_score": row[0], "alert_time": row[1]}
    return None


def db_record_alert(chain: str, token_address: str, symbol: str, name: str, score: int):
    """Upsert an alert record."""
    if db_conn is None:
        return
    try:
        db_conn.execute(
            """
            INSERT INTO alerts (chain, token_address, symbol, name, total_score, alert_time)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(chain, token_address) DO UPDATE SET
                total_score=excluded.total_score,
                alert_time=excluded.alert_time,
                symbol=excluded.symbol,
                name=excluded.name
            """,
            (chain, token_address, symbol, name, score, time.time()),
        )
        db_conn.commit()
    except Exception as e:
        logger.warning(f"DB alert upsert failed: {e}")


def db_get_high_scorers(hours: int = 24, min_score: int = 40, limit: int = 20) -> list:
    """Return tokens with highest recent scores for heartbeat summary."""
    if db_conn is None:
        return []
    since = time.time() - hours * 3600
    cur = db_conn.execute(
        """
        SELECT chain, token_address, symbol, name, MAX(total_score) as max_score
        FROM token_snapshots
        WHERE created_at > ? AND total_score >= ?
        GROUP BY chain, token_address
        ORDER BY max_score DESC
        LIMIT ?
        """,
        (since, min_score, limit),
    )
    return [
        {
            "chain": r[0],
            "address": r[1],
            "symbol": r[2],
            "name": r[3],
            "max_score": r[4],
        }
        for r in cur.fetchall()
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# SEEN-PAIRS LRU
# ═══════════════════════════════════════════════════════════════════════════════


def _pair_seen(pair_id: str) -> bool:
    return pair_id in _seen_pairs_set


def _mark_pair_seen(pair_id: str):
    """Add pair_id to the LRU dedup set."""
    if pair_id in _seen_pairs_set:
        return
    if len(_seen_pairs_order) == _seen_pairs_order.maxlen:
        # deque will evict leftmost on append; mirror in set
        evicted = _seen_pairs_order.popleft()
        _seen_pairs_set.discard(evicted)
        _seen_pairs_order.append(pair_id)
    else:
        _seen_pairs_order.append(pair_id)
    _seen_pairs_set.add(pair_id)


# ═══════════════════════════════════════════════════════════════════════════════
# COINGECKO ID MAPPING
# ═══════════════════════════════════════════════════════════════════════════════


async def build_coingecko_id_map(session: aiohttp.ClientSession) -> Dict[str, Dict[str, str]]:
    """
    Fetch CoinGecko's full coin list and build a mapping of
    chain → {contract_address_lower: coin_gecko_id}.
    Called once at startup. Cached to disk for persistence across restarts.
    """
    cache_file = "coingecko_id_cache.json"
    # Use on-disk cache if fresh (< 6 hours old)
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 6 * 3600:
            try:
                with open(cache_file) as f:
                    data = json.load(f)
                logger.info(f"Loaded CoinGecko ID cache from disk ({len(data)} chains)")
                return {k: v for k, v in data.items()}
            except Exception:
                pass

    logger.info("Fetching CoinGecko coin list to build contract→ID mapping...")
    headers = {}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    await _coingecko_rate_limit()
    try:
        async with session.get(
            "https://api.coingecko.com/api/v3/coins/list?include_platform=true",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"CoinGecko list returned {resp.status}")
                return {}
            coins = await resp.json()
    except Exception as e:
        logger.warning(f"CoinGecko list fetch failed: {e}")
        return {}

    mapping: Dict[str, Dict[str, str]] = {}
    for coin in coins:
        platforms = coin.get("platforms", {})
        for chain_key, contract in platforms.items():
            if not contract:
                continue
            if chain_key not in mapping:
                mapping[chain_key] = {}
            mapping[chain_key][contract.lower()] = coin["id"]

    # Persist to disk
    try:
        with open(cache_file, "w") as f:
            json.dump(mapping, f)
    except Exception as e:
        logger.warning(f"Failed to persist CoinGecko cache: {e}")

    total = sum(len(v) for v in mapping.values())
    logger.info(f"CoinGecko ID map built: {total} contract entries across {len(mapping)} chains")
    return mapping


# ═══════════════════════════════════════════════════════════════════════════════
# API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


async def _coingecko_rate_limit():
    """Enforce CoinGecko rate limit (calls per minute)."""
    global coingecko_calls_this_minute, coingecko_minute_reset

    now = time.time()
    if now > coingecko_minute_reset:
        coingecko_calls_this_minute = 0
        coingecko_minute_reset = now + 60

    coingecko_calls_this_minute += 1
    if coingecko_calls_this_minute > COINGECKO_CALLS_PER_MINUTE:
        sleep_for = coingecko_minute_reset - now + 1
        logger.debug(f"CoinGecko rate limit reached; sleeping {sleep_for:.1f}s")
        await asyncio.sleep(max(sleep_for, 0))
        coingecko_calls_this_minute = 1
        coingecko_minute_reset = time.time() + 60


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    headers: Optional[Dict] = None,
    timeout: int = API_TIMEOUT,
    retries: int = MAX_RETRIES,
    use_coingecko_limiter: bool = False,
) -> Optional[Any]:
    """
    Fetch JSON with retry and optional CoinGecko rate-limit gating.
    """
    if use_coingecko_limiter:
        await _coingecko_rate_limit()

    async with api_semaphore:
        for attempt in range(1, retries + 1):
            try:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning(f"429 on attempt {attempt}/{retries}: {url} — backoff {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    elif resp.status in (401, 403):
                        logger.error(f"Auth error {resp.status} from {url}")
                        return None
                    else:
                        body = await resp.text()
                        logger.warning(f"HTTP {resp.status} from {url}: {body[:200]}")
                        return None
            except asyncio.TimeoutError:
                logger.debug(f"Timeout attempt {attempt}/{retries}: {url}")
            except aiohttp.ClientError as e:
                logger.debug(f"ClientError attempt {attempt}/{retries} {url}: {e}")
            except Exception as e:
                logger.debug(f"Error attempt {attempt}/{retries} {url}: {e}")

            if attempt < retries:
                await asyncio.sleep(1 * attempt)

        return None


# ═══════════════════════════════════════════════════════════════════════════════
# PAIR DISCOVERY (DexScreener)
# ═══════════════════════════════════════════════════════════════════════════════


async def get_all_pairs(session: aiohttp.ClientSession, network: str) -> List[Dict]:
    """
    Collect pairs for *network* from multiple DexScreener endpoints:
      1. Top boosted tokens (high attention / paid promo)
      2. Latest token profiles (newest listings)
      3. Search by chain (broad coverage)

    Each discovered token is hydrated to full pair data via /tokens/v1.
    Results are deduped and sorted by 5m volume descending.
    """
    pairs: List[Dict] = []
    seen: set = set()

    async def add_pair(p: Dict):
        addr = p.get("pairAddress")
        if addr and addr not in seen:
            seen.add(addr)
            pairs.append(p)

    async def hydrate_token(token_address: str) -> List[Dict]:
        """Fetch full pair data for a token address on this network."""
        url = f"https://api.dexscreener.com/tokens/v1/{network}/{token_address}"
        data = await fetch_json(session, url)
        if not data:
            return []
        if isinstance(data, list):
            return [p for p in data if p.get("chainId") == network]
        if isinstance(data, dict):
            inner = data.get("pairs", [])
            return [p for p in inner if p.get("chainId") == network]
        return []

    # ── Source 1: Top boosted tokens ──
    boost_url = "https://api.dexscreener.com/token-boosts/top/v1"
    boost_data = await fetch_json(session, boost_url)
    if boost_data and isinstance(boost_data, list):
        network_boosts = [
            b for b in boost_data if b.get("chainId") == network
        ][:60]
        tasks = [
            hydrate_token(b["tokenAddress"])
            for b in network_boosts if b.get("tokenAddress")
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, list):
                    for p in result:
                        await add_pair(p)
        logger.info(f"[{network}] Boosts → {len(pairs)} pairs so far")

    # ── Source 2: Latest token profiles ──
    profiles_url = "https://api.dexscreener.com/token-profiles/latest/v1"
    profiles_data = await fetch_json(session, profiles_url)
    if profiles_data and isinstance(profiles_data, list):
        network_profiles = [
            p for p in profiles_data if p.get("chainId") == network
        ][:60]
        tasks = [
            hydrate_token(p["tokenAddress"])
            for p in network_profiles if p.get("tokenAddress")
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, list):
                    for p in result:
                        await add_pair(p)
        logger.info(f"[{network}] Profiles → {len(pairs)} pairs so far")

    # ── Source 3: Search by chain name ──
    if len(pairs) < 200:
        search_url = f"https://api.dexscreener.com/latest/dex/search?q={network}"
        search_data = await fetch_json(session, search_url)
        if search_data and isinstance(search_data, dict) and "pairs" in search_data:
            chain_pairs = [
                p for p in search_data["pairs"] if p.get("chainId") == network
            ][:200]
            for p in chain_pairs:
                await add_pair(p)
        logger.info(f"[{network}] Search → {len(pairs)} pairs total")

    # Sort by 5m volume descending (hottest first)
    pairs.sort(
        key=lambda x: float(x.get("volume", {}).get("m5", 0) or 0),
        reverse=True,
    )
    logger.info(f"[{network}] Collected {len(pairs)} unique pairs")
    return pairs[:300]


# ═══════════════════════════════════════════════════════════════════════════════
# HOLDER CONCENTRATION (Moralis)
# ═══════════════════════════════════════════════════════════════════════════════


async def get_holder_concentration(
    session: aiohttp.ClientSession, chain: str, token: str
) -> Tuple[float, float, float]:
    """
    Return (top10_pct, top50_pct, top100_pct) of total supply held.
    Results cached for 10 minutes. If Moralis key is missing, returns (0,0,0).
    """
    if not MORALIS_API_KEY:
        return 0.0, 0.0, 0.0

    moralis_chain = CHAIN_TO_MORALIS.get(chain)
    if not moralis_chain:
        return 0.0, 0.0, 0.0

    cache_key = f"{moralis_chain}:{token.lower()}"
    now = time.time()

    if cache_key in holder_cache:
        top10, top50, top100, ts = holder_cache[cache_key]
        if now - ts < 600:  # 10-min TTL
            logger.debug(f"Holder cache hit {token[:8]}: top10={top10:.1f}%")
            return top10, top50, top100

    url = (
        f"https://deep-index.moralis.io/api/v2.2/erc20/{token}/owners"
        f"?chain={moralis_chain}&order=DESC&limit=100"
    )
    headers = {"X-API-Key": MORALIS_API_KEY}
    data = await fetch_json(session, url, headers=headers)

    if not data or "result" not in data:
        holder_cache[cache_key] = (0.0, 0.0, 0.0, now)
        return 0.0, 0.0, 0.0

    holders = data.get("result", [])
    total_supply = float(data.get("total_supply") or 0)
    if total_supply == 0:
        holder_cache[cache_key] = (0.0, 0.0, 0.0, now)
        return 0.0, 0.0, 0.0

    balances = [float(h.get("balance", 0)) for h in holders]
    top10 = sum(balances[:10])
    top50 = sum(balances[:50]) if len(balances) >= 50 else sum(balances)
    top100 = sum(balances[:100]) if len(balances) >= 100 else sum(balances)

    top10_pct = (top10 / total_supply) * 100
    top50_pct = (top50 / total_supply) * 100
    top100_pct = (top100 / total_supply) * 100

    holder_cache[cache_key] = (top10_pct, top50_pct, top100_pct, now)

    # Lazy eviction of stale entries (> 1 hour)
    stale = [k for k, (_, _, _, ts) in holder_cache.items() if now - ts > 3600]
    for k in stale:
        del holder_cache[k]

    return top10_pct, top50_pct, top100_pct


# ═══════════════════════════════════════════════════════════════════════════════
# CEX LISTING DETECTION (CoinGecko)
# ═══════════════════════════════════════════════════════════════════════════════


# Tier-1 CEX names (lowercase for matching)
TIER1_CEX_NAMES = {
    "binance", "coinbase", "okx", "bybit", "kraken", "kucoin", "bitfinex",
    "gate.io", "mexc", "huobi", "htx", "crypto.com", "bitget", "deribit",
}

# Keywords indicating perps/futures markets
PERPS_KEYWORDS = {"perpetual", "perp", "futures", "future", "swap", "derivatives", "linear", "inverse"}


def _is_perps_market(ticker: dict) -> bool:
    """Check if a CoinGecko ticker represents a perps/futures market."""
    market_name = (ticker.get("market", {}) or {}).get("name", "").lower()
    trade_url = (ticker.get("trade_url") or "").lower()
    ticker_symbol = (ticker.get("base") or "").lower()
    combined = f"{market_name} {trade_url} {ticker_symbol}"
    return any(kw in combined for kw in PERPS_KEYWORDS)


def _is_cex(ticker: dict) -> bool:
    """Check if a ticker is from a CEX (not DEX)."""
    market = ticker.get("market", {}) or {}
    # CoinGecko marks DEXes explicitly
    if market.get("has_trading_incentive") is not None:
        # has_trading_incentive is usually present for CEXes
        pass
    # If trust_score is present, it's likely a CEX
    if ticker.get("trust_score") is not None:
        return True
    # Check market identifier
    identifier = (market.get("identifier") or "").lower()
    if identifier in {"uniswap", "pancakeswap", "sushiswap", "curve", "balancer",
                       "1inch", "0x", "dodo", "quickswap", "trader_joe",
                       "camelot", "raydium", "orca"}:
        return False
    return True


def _is_tier1_cex(ticker: dict) -> bool:
    """Check if the ticker is from a tier-1 CEX."""
    market_name = (ticker.get("market", {}) or {}).get("name", "").lower()
    return any(t1 in market_name for t1 in TIER1_CEX_NAMES)


async def get_cex_listings(
    session: aiohttp.ClientSession, chain: str, token_address: str
) -> Tuple[int, bool, int]:
    """
    Check CoinGecko for CEX listings of a token.
    Returns (cex_count, has_perps, tier1_cex_count).
    Uses caching to avoid repeated API calls.
    """
    global coingecko_id_map

    platform = CHAIN_TO_COINGECKO_PLATFORM.get(chain)
    if not platform:
        return 0, False, 0

    # Build id map if empty
    if not coingecko_id_map:
        coingecko_id_map = await build_coingecko_id_map(session)

    platform_map = coingecko_id_map.get(platform, {})
    coin_id = platform_map.get(token_address.lower())
    if not coin_id:
        return 0, False, 0

    # Check ticker cache (1-hour TTL)
    now = time.time()
    if coin_id in coingecko_ticker_cache:
        cached, ts = coingecko_ticker_cache[coin_id]
        if now - ts < 3600:
            return cached.get("cex_count", 0), cached.get("has_perps", False), cached.get("tier1_count", 0)

    headers = {}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/tickers"
    data = await fetch_json(session, url, headers=headers, use_coingecko_limiter=True)

    if not data or "tickers" not in data:
        coingecko_ticker_cache[coin_id] = ({"cex_count": 0, "has_perps": False, "tier1_count": 0}, now)
        return 0, False, 0

    tickers = data["tickers"]
    cex_tickers = [t for t in tickers if _is_cex(t)]
    cex_count = len(cex_tickers)
    has_perps = any(_is_perps_market(t) for t in tickers)
    tier1_count = sum(1 for t in cex_tickers if _is_tier1_cex(t))

    result = {"cex_count": cex_count, "has_perps": has_perps, "tier1_count": tier1_count}
    coingecko_ticker_cache[coin_id] = (result, now)

    # Lazy eviction
    stale = [k for k, (_, ts) in coingecko_ticker_cache.items() if now - ts > 7200]
    for k in stale:
        del coingecko_ticker_cache[k]

    return cex_count, has_perps, tier1_count


# ═══════════════════════════════════════════════════════════════════════════════
# SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════


def _score_volume(vol_5m: float, vol_1h: float, vol_6h: float, vol_24h: float) -> Tuple[int, dict]:
    """
    Score volume momentum across timeframes (0–35 pts).
    Returns (score, metadata_dict).
    """
    score = 0
    meta = {}

    # 5m vs 1h: 5m is 8.33% of 1h normally
    if vol_1h > 0:
        ratio_5m_1h = vol_5m / vol_1h
    else:
        ratio_5m_1h = 0.0
    expected_5m_1h = 0.0833  # 5m / 60m
    if expected_5m_1h > 0 and ratio_5m_1h > 0:
        normalized_5m_1h = ratio_5m_1h / expected_5m_1h
    else:
        normalized_5m_1h = 0.0

    pts = _ratio_to_points(normalized_5m_1h)
    score += min(pts, VOL_5M_1H_WEIGHT)
    meta["ratio_5m_1h"] = round(ratio_5m_1h, 4)
    meta["norm_5m_1h"] = round(normalized_5m_1h, 2)
    meta["pts_5m_1h"] = min(pts, VOL_5M_1H_WEIGHT)

    # 1h vs 6h: 1h is 16.67% of 6h normally
    if vol_6h > 0:
        ratio_1h_6h = vol_1h / (vol_6h / 6.0)
    else:
        ratio_1h_6h = 0.0
    pts = _ratio_to_points(ratio_1h_6h)
    score += min(pts, VOL_1H_6H_WEIGHT)
    meta["ratio_1h_6h"] = round(ratio_1h_6h, 2)
    meta["pts_1h_6h"] = min(pts, VOL_1H_6H_WEIGHT)

    # 6h vs 24h: 6h is 25% of 24h normally
    if vol_24h > 0:
        ratio_6h_24h = vol_6h / (vol_24h / 4.0)
    else:
        ratio_6h_24h = 0.0
    pts = _ratio_to_points(ratio_6h_24h)
    score += min(pts, VOL_6H_24H_WEIGHT)
    meta["ratio_6h_24h"] = round(ratio_6h_24h, 2)
    meta["pts_6h_24h"] = min(pts, VOL_6H_24H_WEIGHT)

    return score, meta


def _ratio_to_points(ratio: float) -> int:
    """Convert a volume ratio to score points."""
    if ratio >= VOL_RATIO_EXTREME:
        return 15
    if ratio >= VOL_RATIO_HIGH:
        return 10
    if ratio >= VOL_RATIO_MED:
        return 7
    if ratio >= VOL_RATIO_LOW:
        return 4
    if ratio >= 1.0:
        return 1
    return 0


def _score_holders(top10: float, top50: float, top100: float) -> Tuple[int, dict]:
    """
    Score holder concentration (0–25 pts).
    High concentration = whales/CEXs are accumulating = pump signal.
    """
    score = 0
    meta = {"top10": round(top10, 2), "top50": round(top50, 2), "top100": round(top100, 2)}

    # Top 10 (0–15 pts)
    pts = _pct_to_points(top10)
    score += min(pts, HOLDER_TOP10_WEIGHT)
    meta["pts_top10"] = min(pts, HOLDER_TOP10_WEIGHT)

    # Top 50 (0–6 pts)
    pts = _pct_to_points(top50)
    score += min(pts, HOLDER_TOP50_WEIGHT)
    meta["pts_top50"] = min(pts, HOLDER_TOP50_WEIGHT)

    # Top 100 (0–4 pts)
    pts = _pct_to_points(top100)
    score += min(pts, HOLDER_TOP100_WEIGHT)
    meta["pts_top100"] = min(pts, HOLDER_TOP100_WEIGHT)

    return score, meta


def _pct_to_points(pct: float) -> int:
    """Convert a percentage to score points."""
    if pct >= HOLDER_PCT_EXTREME:
        return 15
    if pct >= HOLDER_PCT_HIGH:
        return 10
    if pct >= HOLDER_PCT_MED:
        return 7
    if pct >= HOLDER_PCT_LOW:
        return 4
    if pct >= 30.0:
        return 1
    return 0


def _score_cex(cex_count: int, has_perps: bool, tier1_count: int) -> Tuple[int, dict]:
    """
    Score CEX listing status (0–25 pts).
    More CEXs + perps = institutional interest = likely pump setup.
    """
    score = 0
    meta = {"cex_count": cex_count, "has_perps": has_perps, "tier1_count": tier1_count}

    listing_pts = min(cex_count * CEX_PER_CEX_POINTS, CEX_MAX_LISTING_POINTS)
    score += listing_pts
    meta["pts_listings"] = listing_pts

    if has_perps:
        score += CEX_PERPS_POINTS
    meta["pts_perps"] = CEX_PERPS_POINTS if has_perps else 0

    tier_pts = min(tier1_count * CEX_MAJOR_BONUS, 6)
    score += tier_pts
    meta["pts_tier1"] = tier_pts

    return score, meta


def _score_price(chg_5m: float, chg_1h: float, chg_6h: float) -> Tuple[int, dict]:
    """
    Score price momentum (0–15 pts).
    Consistent upward movement across timeframes = bullish.
    """
    score = 0
    meta = {"chg_5m": chg_5m, "chg_1h": chg_1h, "chg_6h": chg_6h}

    pts = _chg_to_points(chg_5m)
    score += min(pts, PRICE_5M_WEIGHT)
    meta["pts_5m"] = min(pts, PRICE_5M_WEIGHT)

    pts = _chg_to_points(chg_1h)
    score += min(pts, PRICE_1H_WEIGHT)
    meta["pts_1h"] = min(pts, PRICE_1H_WEIGHT)

    pts = _chg_to_points(chg_6h)
    score += min(pts, PRICE_6H_WEIGHT)
    meta["pts_6h"] = min(pts, PRICE_6H_WEIGHT)

    return score, meta


def _chg_to_points(chg: float) -> int:
    """Convert price change % to score points."""
    abs_chg = abs(chg)
    if abs_chg >= PRICE_CHG_EXTREME:
        return 15
    if abs_chg >= PRICE_CHG_HIGH:
        return 10
    if abs_chg >= PRICE_CHG_MED:
        return 7
    if abs_chg >= PRICE_CHG_LOW:
        return 4
    if abs_chg >= 1.0:
        return 1
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════


async def evaluate_token(
    session: aiohttp.ClientSession, pair: Dict
) -> Optional[Dict]:
    """
    Evaluate a single pair and return a score dict if it passes minimum viability.
    Scoring is cumulative — tokens earn points across multiple signals.
    Only the most promising candidates trigger expensive API calls (Moralis, CoinGecko).
    """
    global tokens_evaluated, tokens_scored

    chain = pair.get("chainId")
    base_token = pair.get("baseToken", {}) or {}
    token = base_token.get("address")
    pair_id = pair.get("pairAddress")
    symbol = base_token.get("symbol", "???")
    name = base_token.get("name", "Unknown")

    if not all([chain, token, pair_id]):
        return None

    tokens_evaluated += 1

    # ── Basic viability checks (cheap) ──
    liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
    if liquidity < MIN_LIQUIDITY_USD:
        return None

    price = float(pair.get("priceUsd") or 0)
    if price < MIN_PRICE:
        return None

    vol_5m = float(pair.get("volume", {}).get("m5") or 0)
    if vol_5m < MIN_VOL_5M_USD:
        return None

    # Dedup
    dedup_key = f"{chain}:{token.lower()}"
    if _pair_seen(dedup_key):
        return None
    _mark_pair_seen(dedup_key)

    # Extract all timeframe data
    vol_1h = float(pair.get("volume", {}).get("h1") or 0)
    vol_6h = float(pair.get("volume", {}).get("h6") or 0)
    vol_24h = float(pair.get("volume", {}).get("h24") or 0)

    txns_5m = pair.get("txns", {}).get("m5", {}) or {}
    buys_5m = int(txns_5m.get("buys", 0))
    sells_5m = int(txns_5m.get("sells", 0))
    total_txns_5m = buys_5m + sells_5m
    buy_pressure = buys_5m / total_txns_5m if total_txns_5m > 0 else 0.0

    chg_5m = float(pair.get("priceChange", {}).get("m5") or 0)
    chg_1h = float(pair.get("priceChange", {}).get("h1") or 0)
    chg_6h = float(pair.get("priceChange", {}).get("h6") or 0)

    # ── Phase 1: Volume + Price scoring (free, no external API) ──
    vol_score, vol_meta = _score_volume(vol_5m, vol_1h, vol_6h, vol_24h)
    price_score, price_meta = _score_price(chg_5m, chg_1h, chg_6h)

    phase1_score = vol_score + price_score

    # If phase 1 is too low, skip expensive API calls
    if phase1_score < 10:
        if VERBOSE_SCORING:
            logger.debug(
                f"SKIP {symbol}: phase1={phase1_score} (vol={vol_score}, price={price_score})"
            )
        return None

    # ── Phase 2: Holder concentration (Moralis — expensive) ──
    top10, top50, top100 = await get_holder_concentration(session, chain, token)
    holder_score, holder_meta = _score_holders(top10, top50, top100)

    phase2_score = phase1_score + holder_score

    # If still not promising, optionally skip CoinGecko
    if phase2_score < 20:
        if VERBOSE_SCORING:
            logger.debug(
                f"SKIP {symbol}: phase2={phase2_score} (holders={holder_score})"
            )
        return None

    # ── Phase 3: CEX listings (CoinGecko — rate-limited) ──
    cex_count, has_perps, tier1_count = await get_cex_listings(session, chain, token)
    cex_score, cex_meta = _score_cex(cex_count, has_perps, tier1_count)

    # ── Final score ──
    total_score = phase2_score + cex_score
    tokens_scored += 1

    result = {
        "chain": chain,
        "token_address": token,
        "symbol": symbol,
        "name": name,
        "pair_address": pair_id,
        "total_score": total_score,
        "vol_score": vol_score,
        "holder_score": holder_score,
        "cex_score": cex_score,
        "price_score": price_score,
        "top10_pct": top10,
        "top50_pct": top50,
        "top100_pct": top100,
        "cex_count": cex_count,
        "has_perps": has_perps,
        "major_cex_count": tier1_count,
        "price_chg_5m": chg_5m,
        "price_chg_1h": chg_1h,
        "price_chg_6h": chg_6h,
        "vol_5m": vol_5m,
        "vol_1h": vol_1h,
        "vol_6h": vol_6h,
        "buy_pressure_5m": buy_pressure,
        "liquidity": liquidity,
        "dex_url": f"https://dexscreener.com/{chain}/{pair_id}",
        # Metadata for debugging
        "_vol_meta": vol_meta,
        "_holder_meta": holder_meta,
        "_cex_meta": cex_meta,
        "_price_meta": price_meta,
    }

    if VERBOSE_SCORING:
        logger.info(
            f"SCORED {symbol}: total={total_score} "
            f"(vol={vol_score}, holders={holder_score}, cex={cex_score}, price={price_score}) "
            f"top10={top10:.1f}% cex={cex_count} perps={has_perps}"
        )

    # Persist snapshot for trend tracking
    db_insert_snapshot(result)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERTS
# ═══════════════════════════════════════════════════════════════════════════════


async def send_alert(alert: Dict, retries: int = 2):
    """Send a rich Telegram alert with full score breakdown."""
    global alerts_sent

    buy_pct = (
        round(alert["buy_pressure_5m"] * 100, 1)
        if alert.get("buy_pressure_5m")
        else "N/A"
    )

    # Build score bar visualization
    score = alert["total_score"]
    filled = min(score // 5, 20)
    bar = "█" * filled + "░" * (20 - filled)

    # Emoji indicators
    perps_emoji = "🔥" if alert.get("has_perps") else "❌"
    holder_emoji = "🐳" if alert.get("top10_pct", 0) > 80 else "👥"

    text = (
        f"🚨 <b>PUMP SIGNAL — Score {score}/100</b>\n"
        f"<code>[{bar}]</code>\n\n"
        f"<b>{alert['name']}</b> ({alert['symbol']})\n"
        f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n\n"
        f"<b>📊 Score Breakdown:</b>\n"
        f"  Volume:     <b>{alert['vol_score']}</b>/35 pts\n"
        f"  Holders:    <b>{alert['holder_score']}</b>/25 pts {holder_emoji}\n"
        f"  CEX:        <b>{alert['cex_score']}</b>/25 pts\n"
        f"  Price:      <b>{alert['price_score']}</b>/15 pts\n\n"
        f"<b>💰 Liquidity:</b> ${alert['liquidity']:,.0f}\n"
        f"<b>📈 Volume:</b> 5m=${alert['vol_5m']:,.0f} | 1h=${alert['vol_1h']:,.0f} | 6h=${alert['vol_6h']:,.0f}\n"
        f"<b>💹 Price Δ:</b> 5m={alert['price_chg_5m']:+.1f}% | 1h={alert['price_chg_1h']:+.1f}% | 6h={alert['price_chg_6h']:+.1f}%\n"
        f"<b>🟢 Buy Pressure 5m:</b> {buy_pct}% ({alert.get('buys_5m', '?')}B / {alert.get('sells_5m', '?')}S)\n\n"
        f"<b>👥 Holder Concentration:</b>\n"
        f"  Top 10:  <b>{alert['top10_pct']:.1f}%</b>\n"
        f"  Top 50:  <b>{alert['top50_pct']:.1f}%</b>\n"
        f"  Top 100: <b>{alert['top100_pct']:.1f}%</b>\n\n"
        f"<b>🏛 CEX Listings:</b> {alert['cex_count']} ({alert['major_cex_count']} major)\n"
        f"<b>📋 Perps/Futures:</b> {perps_emoji} {'YES' if alert.get('has_perps') else 'No'}\n\n"
        f"🔗 <a href='{alert['dex_url']}'>DexScreener</a>"
    )

    # Append CoinGecko link if we have a mapping
    cg_id = None
    platform = CHAIN_TO_COINGECKO_PLATFORM.get(alert['chain'], '')
    if platform and coingecko_id_map:
        cg_id = coingecko_id_map.get(platform, {}).get(alert['token_address'].lower())
    if cg_id:
        text += f" | <a href='https://www.coingecko.com/en/coins/{cg_id}'>CoinGecko</a>"

    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            alerts_sent += 1
            logger.info(
                f"✅ ALERT #{alerts_sent} sent → {alert['symbol']} score={score}"
            )
            return
        except Exception as e:
            logger.error(f"Telegram send attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                await asyncio.sleep(2)

    logger.error(f"Alert dropped after {retries} retries: {alert['symbol']}")


async def send_heartbeat():
    """Send periodic status heartbeat to Telegram."""
    uptime_hours = (time.time() - start_time) / 3600 if start_time > 0 else 0
    now = time.time()
    pairs_last_hour = sum(
        cnt for ts, cnt in hourly_scan_records if now - ts <= 3600
    )

    # Get top scorers from DB
    top_scorers = db_get_high_scorers(hours=24, min_score=30, limit=10)
    scorers_text = ""
    if top_scorers:
        scorers_text = "\n<b>🏆 Top Scorers (24h):</b>\n"
        for s in top_scorers:
            scorers_text += f"  {s['symbol']} — max score <b>{s['max_score']}</b>\n"

    text = (
        f"🫀 <b>Pump Bot v2 Heartbeat</b>\n"
        f"Uptime: <b>{uptime_hours:.1f}h</b>\n"
        f"Pairs scanned: <b>{total_pairs_scanned:,}</b> (last hour: <b>{pairs_last_hour:,}</b>)\n"
        f"Tokens evaluated: <b>{tokens_evaluated:,}</b>\n"
        f"Tokens scored: <b>{tokens_scored:,}</b>\n"
        f"Alerts sent: <b>{alerts_sent}</b>\n"
        f"Holder cache: <b>{len(holder_cache)}</b> | "
        f"CG ticker cache: <b>{len(coingecko_ticker_cache)}</b>\n"
        f"Seen pairs: <b>{len(_seen_pairs_set):,}</b>\n"
        f"Moralis: {'✅' if MORALIS_API_KEY else '❌'} | "
        f"CoinGecko key: {'✅' if COINGECKO_API_KEY else '⚪ (free tier)'}"
        f"{scorers_text}"
    )

    for attempt in range(1, 3):
        try:
            await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
            logger.info("Heartbeat sent")
            return
        except Exception as e:
            logger.error(f"Heartbeat attempt {attempt}/2 failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════════════════════


async def bot_task():
    """Core bot scanning loop."""
    global start_time, last_heartbeat_time, total_pairs_scanned, coingecko_id_map, db_conn

    # Init database
    db_conn = init_db()

    # Validate config
    if not MORALIS_API_KEY:
        logger.warning(
            "⚠️ MORALIS_API_KEY not set — holder concentration will score 0. "
            "Get a free key at https://admin.moralis.io"
        )
    if not COINGECKO_API_KEY:
        logger.info(
            "ℹ️ COINGECKO_API_KEY not set — using free tier (10 calls/min). "
            "Get a free demo key at https://www.coingecko.com/en/api/pricing for 30 calls/min."
        )

    start_time = time.time()
    last_heartbeat_time = start_time

    logger.info("🚀 Pump Bot v2 starting...")

    async with aiohttp.ClientSession() as session:
        # Pre-build CoinGecko ID map (needed for CEX detection)
        logger.info("Building CoinGecko contract→ID mapping (one-time)...")
        coingecko_id_map = await build_coingecko_id_map(session)

        # Send startup message
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"✅ <b>Pump Bot v2</b> started\n"
                    f"Scoring engine: multi-signal (0-100)\n"
                    f"Alert threshold: <b>{ALERT_THRESHOLD}</b> pts\n"
                    f"Networks: {', '.join(NETWORKS).upper()}\n"
                    f"Min liquidity: ${MIN_LIQUIDITY_USD:,.0f}\n"
                    f"Re-alert cooldown: {RE_ALERT_COOLDOWN_HOURS}h\n"
                    f"Moralis: {'✅' if MORALIS_API_KEY else '❌'} | "
                    f"CoinGecko: {'✅ key' if COINGECKO_API_KEY else '⚪ free tier'}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

        # Main loop
        while not shutdown_flag:
            cycle_start = time.time()
            cycle_pairs = 0
            cycle_alerts = 0

            # Heartbeat
            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                await send_heartbeat()
                last_heartbeat_time = time.time()

            # Scan each network
            for network in NETWORKS:
                if shutdown_flag:
                    break

                logger.info(f"🔍 Scanning {network.upper()}...")
                try:
                    pairs = await get_all_pairs(session, network)
                except Exception as e:
                    logger.error(f"get_all_pairs({network}) crashed: {e}")
                    continue

                logger.info(f"[{network}] Evaluating {len(pairs)} pairs...")

                for pair in pairs:
                    if shutdown_flag:
                        break

                    total_pairs_scanned += 1
                    cycle_pairs += 1

                    try:
                        result = await evaluate_token(session, pair)
                    except Exception as e:
                        logger.error(f"evaluate_token crashed: {e}")
                        continue

                    if not result:
                        continue

                    score = result["total_score"]

                    # Check alert threshold
                    if score >= ALERT_THRESHOLD:
                        # Check cooldown
                        last_alert = db_get_last_alert(
                            result["chain"], result["token_address"]
                        )
                        should_alert = True

                        if last_alert:
                            hours_since = (time.time() - last_alert["alert_time"]) / 3600
                            if hours_since < RE_ALERT_COOLDOWN_HOURS:
                                # Check if score improved significantly
                                score_jump = score - last_alert["total_score"]
                                if score_jump < SCORE_IMPROVEMENT_THRESHOLD:
                                    should_alert = False
                                    logger.debug(
                                        f"Cooldown: {result['symbol']} — "
                                        f"last alert {hours_since:.1f}h ago, "
                                        f"score jump {score_jump}"
                                    )

                        if should_alert:
                            # Add txns for the alert
                            txns_5m = pair.get("txns", {}).get("m5", {}) or {}
                            result["buys_5m"] = int(txns_5m.get("buys", 0))
                            result["sells_5m"] = int(txns_5m.get("sells", 0))

                            await send_alert(result)
                            db_record_alert(
                                result["chain"],
                                result["token_address"],
                                result["symbol"],
                                result["name"],
                                score,
                            )
                            cycle_alerts += 1
                            await asyncio.sleep(1)  # throttle between alerts

            cycle_duration = time.time() - cycle_start
            hourly_scan_records.append((cycle_start, cycle_pairs))

            logger.info(
                f"🔄 Cycle complete: {cycle_pairs} pairs, "
                f"{cycle_alerts} alerts in {cycle_duration:.1f}s"
            )

            # Sleep remainder of scan interval
            sleep_time = max(0, SCAN_INTERVAL - cycle_duration)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                logger.warning(
                    f"Cycle overran SCAN_INTERVAL by {cycle_duration - SCAN_INTERVAL:.1f}s"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI LIFESPAN & HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    global shutdown_flag
    shutdown_flag = False
    task = asyncio.create_task(bot_task())
    logger.info("✅ Bot background task started via lifespan")
    yield
    shutdown_flag = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        logger.info("Bot task cancelled cleanly")
    if db_conn:
        db_conn.close()
    logger.info("Shutdown complete")


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health_check():
    uptime = (time.time() - start_time) / 3600 if start_time > 0 else 0
    now = time.time()
    pairs_last_hour = sum(
        cnt for ts, cnt in hourly_scan_records if now - ts <= 3600
    )
    return {
        "status": "alive",
        "version": "2.0.0",
        "uptime_hours": round(uptime, 2),
        "pairs_scanned_total": total_pairs_scanned,
        "pairs_scanned_last_hour": pairs_last_hour,
        "tokens_evaluated": tokens_evaluated,
        "tokens_scored": tokens_scored,
        "alerts_sent": alerts_sent,
        "holder_cache_size": len(holder_cache),
        "cg_ticker_cache_size": len(coingecko_ticker_cache),
        "seen_pairs_size": len(_seen_pairs_set),
        "moralis_configured": bool(MORALIS_API_KEY),
        "coingecko_key_configured": bool(COINGECKO_API_KEY),
        "alert_threshold": ALERT_THRESHOLD,
    }


@app.get("/top-scorers")
async def top_scorers(hours: int = 24, min_score: int = 30, limit: int = 20):
    """API endpoint to query top-scoring tokens from the database."""
    return {
        "tokens": db_get_high_scorers(hours=hours, min_score=min_score, limit=limit)
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting Pump Bot v2 on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
