#!/usr/bin/env python3
"""
================================================================================
PUMP SIGNAL BOT v5.2 — Manual Trader + CA Paste (BonkBot Style)
================================================================================
NO auto-buy. You scan, you decide, you click.

Features:
  • Paste any CA in chat → bot fetches token + shows buy buttons
  • Custom buy amounts (preset small + custom input)
  • /risk <usd> — set $ risk per trade, bot converts to ETH/BNB
  • /positions — click any position → sell % / buy more / set SL
  • All swaps via Uniswap V3 with QuoterV2 slippage protection
  • Paper trading by default

.env:
  TELEGRAM_TOKEN, CHAT_ID, WALLET_PRIVATE_KEY
  MORALIS_API_KEY, COINGECKO_API_KEY (optional)
  PAPER_TRADING=true (keep true until you trust it)
================================================================================
"""

import asyncio
import aiohttp
import time
import os
import json
import logging
import sqlite3
import re
from contextlib import asynccontextmanager
from collections import deque
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from fastapi import FastAPI
import uvicorn

try:
    from web3 import Web3
    from eth_account import Account
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False
    print("WARNING: web3 not installed. Run: pip install web3")

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")

PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
MAX_ALLOWED_TAX = float(os.getenv("MAX_ALLOWED_TAX", "0"))
VERBOSE_LOGGING = os.getenv("VERBOSE_LOGGING", "false").lower() == "true"

# Alert threshold — reads from env, defaults to 65
ALERT_THRESHOLD = int(os.getenv("MIN_SCORE", os.getenv("ALERT_THRESHOLD", "65")))

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")

NETWORKS = ["bsc", "ethereum", "base", "robinhood"]

CHAIN_TO_MORALIS = {"bsc": "bsc", "ethereum": "eth", "base": "base"}
CHAIN_TO_COINGECKO_PLATFORM = {
    "bsc": "binance-smart-chain",
    "ethereum": "ethereum",
    "base": "base",
    "robinhood": "robinhood",
}
CHAIN_TO_GOPLUS_ID = {"bsc": "56", "ethereum": "1", "base": "8453"}
BLOCKSCOUT_URLS = {"robinhood": "https://robinhoodchain.blockscout.com/api/v2"}

RPCS = {
    "ethereum": os.getenv("ETH_RPC", "https://eth.llamarpc.com"),
    "bsc": os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org/"),
    "base": os.getenv("BASE_RPC", "https://mainnet.base.org"),
    "robinhood": os.getenv("ROBINHOOD_RPC", "https://robinhoodchain.blockscout.com/api/eth-rpc"),
}

ROUTERS_V3 = {
    "ethereum": os.getenv("ETH_ROUTER_V3", "0xE592427A0AEce92De3Edee1F18E0157C05861564"),
    "base": os.getenv("BASE_ROUTER_V3", "0x2626664c2603336E57B271c5C0b26F421741e481"),
    "bsc": os.getenv("BSC_ROUTER_V3", "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4"),
    "robinhood": os.getenv("ROBINHOOD_ROUTER_V3", ""),
}

QUOTERS_V2 = {
    "ethereum": os.getenv("ETH_QUOTER_V2", "0x61fFE014bA17989E743c5F6cB21bF969dc0b0e10"),
    "base": os.getenv("BASE_QUOTER_V2", "0x3d4e44Eb1374240CE5F1B871ab261CD16335CB61"),
    "bsc": os.getenv("BSC_QUOTER_V2", ""),
    "robinhood": os.getenv("ROBINHOOD_QUOTER_V2", ""),
}

WETH = {
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "base": "0x4200000000000000000000000000000000000006",
    "robinhood": os.getenv("ROBINHOOD_WNATIVE", ""),
}

NATIVE_SYMBOL = {"ethereum": "ETH", "bsc": "BNB", "base": "ETH", "robinhood": "ETH"}
NATIVE_DECIMALS = {"ethereum": 18, "bsc": 18, "base": 18, "robinhood": 18}

V3_FEE_TIERS = [3000, 10000, 500]

SCAN_INTERVAL = 30
HEARTBEAT_INTERVAL = 3600
POSITION_CHECK_INTERVAL = 30

MIN_LIQUIDITY_USD = 10_000.0
MIN_VOL_5M_USD = 100.0
MIN_PRICE = 1e-12
MIN_MARKET_CAP_USD = 10_000.0
ROBINHOOD_MIN_LIQUIDITY_USD = 5_000.0
ROBINHOOD_MIN_PAIR_AGE_MIN = 10.0

# Scoring weights
VOL_LIQUIDITY_PTS = 10; VOL_5M_1H_PTS = 8; VOL_1H_6H_PTS = 7; VOL_6H_24H_PTS = 5
BUY_PRESSURE_5M_PTS = 12; BUY_PRESSURE_1H_PTS = 8
HOLDER_TOP10_PTS = 10; HOLDER_TOP50_PTS = 6; HOLDER_TOP100_PTS = 4
PRICE_5M_PTS = 6; PRICE_1H_PTS = 5; PRICE_6H_PTS = 4
SECURITY_PTS = 10; CEX_LISTING_PTS = 3; CEX_PERPS_PTS = 2
PENALTY_SELL_PRESSURE_5M = 15; PENALTY_SELL_PRESSURE_1H = 10
PENALTY_LOW_TX_5M = 8; PENALTY_LOW_TX_1H = 4; PENALTY_UNVERIFIED_CONTRACT = 8

RE_ALERT_COOLDOWN_HOURS = 4
SCORE_IMPROVEMENT_THRESHOLD = 12
MAX_RETRIES = 2; API_TIMEOUT = 10; CONCURRENT_API_LIMIT = 15
COINGECKO_CALLS_PER_MINUTE = 25 if COINGECKO_API_KEY else 10
PHASE1_MIN_SCORE = 20

# Default small buy amounts (can be overridden per user)
DEFAULT_BUY_AMOUNTS = {
    "ethereum": [0.001, 0.003, 0.005, 0.01],
    "bsc": [0.01, 0.03, 0.05, 0.1],
    "base": [0.001, 0.003, 0.005, 0.01],
    "robinhood": [0.001, 0.003, 0.005, 0.01],
}

DEFAULT_TP_LEVELS = [
    {"pct": 50, "sell_pct": 25},
    {"pct": 100, "sell_pct": 25},
    {"pct": 200, "sell_pct": 50},
]

DEFAULT_TRAILING_STOP = 15.0
DEFAULT_SLIPPAGE = 5.0
DEFAULT_RISK_USD = 10.0  # $10 default risk per trade

# Telegram state for awaiting user input
user_state: Dict[int, dict] = {}  # user_id -> {action, data}

# ═══════════════════════════════════════════════════════════════════════════════
# ABIs
# ═══════════════════════════════════════════════════════════════════════════════

V3_ROUTER_ABI = [
    {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct ISwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
]

QUOTER_V2_ABI = [
    {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}
]

ERC20_ABI = [
    {"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
    {"constant":False,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
]

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING & GLOBALS
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("pump_bot_v5.log"), logging.StreamHandler()],
)
logger = logging.getLogger("pump_bot_v5")

bot = Bot(token=TELEGRAM_TOKEN)
api_semaphore = asyncio.Semaphore(CONCURRENT_API_LIMIT)

security_cache: Dict[str, Tuple[dict, float]] = {}
holder_cache: Dict[str, Tuple[Tuple[float, float, float], float]] = {}
coingecko_id_map: Dict[str, Dict[str, str]] = {}
coingecko_ticker_cache: Dict[str, Tuple[dict, float]] = {}

db_conn: Optional[sqlite3.Connection] = None
w3_instances: Dict[str, Any] = {}
WALLET_ADDRESS: Optional[str] = None

if WEB3_AVAILABLE and PRIVATE_KEY:
    logger.info(f"Private key detected ({len(PRIVATE_KEY)} chars), attempting wallet load...")
    for chain, rpc in RPCS.items():
        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            if w3.is_connected():
                w3_instances[chain] = w3
                logger.info(f"Web3 connected: {chain}")
            else:
                logger.warning(f"Web3 failed: {chain}")
        except Exception as e:
            logger.warning(f"Web3 init failed for {chain}: {e}")
    if w3_instances:
        try:
            account = Account.from_key(PRIVATE_KEY)
            WALLET_ADDRESS = account.address
            logger.info(f"Wallet loaded: {WALLET_ADDRESS}")
        except Exception as e:
            logger.error(f"Failed to derive wallet from private key: {e}")
            WALLET_ADDRESS = None
    else:
        logger.error("No Web3 connections established")
else:
    reason = []
    if not WEB3_AVAILABLE:
        reason.append("web3 package not installed")
    if not PRIVATE_KEY:
        reason.append("WALLET_PRIVATE_KEY not set")
    logger.warning(f"Wallet disabled: {', '.join(reason)}")
    if not PAPER_TRADING:
        logger.warning("PAPER_TRADING=false but wallet unavailable. Forcing paper mode.")
        PAPER_TRADING = True

total_pairs_scanned = 0
tokens_evaluated = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False

DB_PATH = "pump_bot_v5.db"

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL, token_address TEXT NOT NULL, symbol TEXT,
            total_score INTEGER, alert_time REAL,
            UNIQUE(chain, token_address)
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL, token_address TEXT NOT NULL, symbol TEXT,
            entry_price REAL, highest_price REAL,
            amount_tokens REAL, amount_native REAL,
            remaining_pct REAL DEFAULT 100.0,
            trailing_stop_pct REAL, take_profit_levels TEXT,
            status TEXT DEFAULT 'open', entry_time REAL, close_time REAL,
            pnl_pct REAL, tx_hash_buy TEXT, tx_hash_sell TEXT, paper_trade INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT, token_address TEXT, symbol TEXT, action TEXT,
            amount_native REAL, amount_tokens REAL, price_usd REAL, timestamp REAL
        );
    """)
    defaults = {
        "slippage": str(DEFAULT_SLIPPAGE),
        "trailing_stop": str(DEFAULT_TRAILING_STOP),
        "take_profit_levels": json.dumps(DEFAULT_TP_LEVELS),
        "buy_amounts_eth": json.dumps(DEFAULT_BUY_AMOUNTS.get("ethereum", [0.001, 0.003, 0.005, 0.01])),
        "buy_amounts_bnb": json.dumps(DEFAULT_BUY_AMOUNTS.get("bsc", [0.01, 0.03, 0.05, 0.1])),
        "buy_amounts_base": json.dumps(DEFAULT_BUY_AMOUNTS.get("base", [0.001, 0.003, 0.005, 0.01])),
        "risk_usd": str(DEFAULT_RISK_USD),
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    return conn

def db_get_setting(key: str, default=None):
    cur = db_conn.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default

def db_set_setting(key: str, value: str):
    db_conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db_conn.commit()

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

def db_add_position(chain, token_address, symbol, entry_price, amount_tokens, amount_native,
                    trailing_stop, tp_levels, tx_hash, paper=0):
    try:
        db_conn.execute(
            "INSERT INTO positions (chain, token_address, symbol, entry_price, highest_price, "
            "amount_tokens, amount_native, remaining_pct, trailing_stop_pct, take_profit_levels, "
            "status, entry_time, tx_hash_buy, paper_trade) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chain, token_address, symbol, entry_price, entry_price, amount_tokens, amount_native,
             100.0, trailing_stop, json.dumps(tp_levels), "open", time.time(), tx_hash, paper)
        )
        db_conn.commit()
        return db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception as e:
        logger.error(f"DB add position failed: {e}")
        return None

def db_get_open_positions():
    cur = db_conn.execute(
        "SELECT id, chain, token_address, symbol, entry_price, highest_price, amount_tokens, "
        "remaining_pct, trailing_stop_pct, take_profit_levels, paper_trade "
        "FROM positions WHERE status='open' ORDER BY entry_time DESC"
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

def db_get_position(pos_id):
    cur = db_conn.execute(
        "SELECT id, chain, token_address, symbol, entry_price, highest_price, amount_tokens, "
        "remaining_pct, trailing_stop_pct, take_profit_levels, paper_trade "
        "FROM positions WHERE id=?", (pos_id,)
    )
    cols = [c[0] for c in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None

def db_update_position_price(pos_id, current_price, highest_price):
    db_conn.execute(
        "UPDATE positions SET highest_price=? WHERE id=?",
        (max(highest_price, current_price), pos_id)
    )
    db_conn.commit()

def db_reduce_position(pos_id, sell_pct, pnl_pct, tx_hash):
    cur = db_conn.execute("SELECT remaining_pct FROM positions WHERE id=?", (pos_id,))
    row = cur.fetchone()
    if not row:
        return
    new_remaining = max(0, row[0] - sell_pct)
    status = "closed" if new_remaining <= 0 else "open"
    db_conn.execute(
        "UPDATE positions SET remaining_pct=?, status=?, pnl_pct=?, tx_hash_sell=? WHERE id=?",
        (new_remaining, status, pnl_pct, tx_hash, pos_id)
    )
    db_conn.commit()

def db_close_position(pos_id, pnl_pct, tx_hash):
    db_conn.execute(
        "UPDATE positions SET status='closed', remaining_pct=0, close_time=?, pnl_pct=?, tx_hash_sell=? WHERE id=?",
        (time.time(), pnl_pct, tx_hash, pos_id)
    )
    db_conn.commit()

def db_update_trailing_stop(pos_id, new_sl):
    db_conn.execute("UPDATE positions SET trailing_stop_pct=? WHERE id=?", (new_sl, pos_id))
    db_conn.commit()

def db_log_paper_trade(chain, token_address, symbol, action, amount_native, amount_tokens, price_usd):
    db_conn.execute(
        "INSERT INTO paper_trades (chain, token_address, symbol, action, amount_native, amount_tokens, price_usd, timestamp) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (chain, token_address, symbol, action, amount_native, amount_tokens, price_usd, time.time())
    )
    db_conn.commit()

def prune_caches():
    now = time.time()
    for cache, ttl in [(security_cache, 1800), (coingecko_ticker_cache, 7200)]:
        stale = [k for k, (_, ts) in cache.items() if now - ts > ttl]
        for k in stale:
            del cache[k]
    stale = [k for k, (_, ts) in holder_cache.items() if now - ts > 3600]
    for k in stale:
        del holder_cache[k]

# ═══════════════════════════════════════════════════════════════════════════════
# API HELPERS
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
                        if VERBOSE_LOGGING:
                            logger.debug(f"HTTP {resp.status} for {url[:80]}")
                        return None
            except Exception as e:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
                elif VERBOSE_LOGGING:
                    logger.debug(f"Fetch failed for {url[:80]}: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# PAIR DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

async def get_all_pairs(session, network):
    pairs = []
    seen = set()
    async def add_pair(p):
        addr = p.get("pairAddress")
        if addr and addr not in seen:
            seen.add(addr)
            pairs.append(p)

    url = f"https://api.dexscreener.com/latest/dex/pairs/{network}?page=0&pageSize=300"
    data = await fetch_json(session, url)
    if data and "pairs" in data:
        for p in data["pairs"]:
            if p.get("chainId") == network:
                await add_pair(p)

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
# SECURITY CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_token_security(session, chain, token):
    if chain == "robinhood":
        return await get_robinhood_security(session, token)
    goplus_chain = CHAIN_TO_GOPLUS_ID.get(chain)
    if not goplus_chain:
        return None
    cache_key = f"goplus:{goplus_chain}:{token.lower()}"
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
        "source": "goplus",
    }
    security_cache[cache_key] = (security, now)
    return security

async def get_robinhood_security(session, token):
    cache_key = f"robinhood_sec:{token.lower()}"
    now = time.time()
    if cache_key in security_cache:
        cached, ts = security_cache[cache_key]
        if now - ts < 900:
            return cached
    base = BLOCKSCOUT_URLS["robinhood"]
    security = {
        "is_honeypot": False, "buy_tax": 0, "sell_tax": 0,
        "is_whitelisted": False, "is_blacklisted": False,
        "is_open_source": False, "is_proxy": False,
        "can_take_back_ownership": False, "owner_change_balance": False,
        "is_mintable": False, "slippage_modifiable": False,
        "transfer_pausable": False, "lp_locked": False,
        "is_verified": False, "source": "blockscout",
    }
    url = f"{base}/smart-contracts/{token}"
    data = await fetch_json(session, url)
    if data:
        security["is_verified"] = data.get("is_verified", False)
        security["is_open_source"] = data.get("is_verified", False)
        security["is_proxy"] = data.get("proxy_type") is not None
    url2 = f"{base}/tokens/{token}"
    token_data = await fetch_json(session, url2)
    if token_data:
        supply = token_data.get("total_supply")
        if supply is None or str(supply) in ("0", "null", ""):
            security["is_honeypot"] = True
    security_cache[cache_key] = (security, now)
    return security

# ═══════════════════════════════════════════════════════════════════════════════
# HOLDER CONCENTRATION
# ═══════════════════════════════════════════════════════════════════════════════

async def get_holder_concentration(session, chain, token):
    if chain == "robinhood":
        return await get_robinhood_holders(session, token)
    return await get_moralis_holders(session, chain, token)

async def get_moralis_holders(session, chain, token):
    if not MORALIS_API_KEY:
        return 0.0, 0.0, 0.0
    moralis_chain = CHAIN_TO_MORALIS.get(chain)
    if not moralis_chain:
        return 0.0, 0.0, 0.0
    cache_key = f"moralis:{moralis_chain}:{token.lower()}"
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
    return pct10, pct50, pct100

async def get_robinhood_holders(session, token):
    cache_key = f"robinhood_holders:{token.lower()}"
    now = time.time()
    if cache_key in holder_cache:
        (top10, top50, top100), ts = holder_cache[cache_key]
        if now - ts < 600:
            return top10, top50, top100
    base = BLOCKSCOUT_URLS["robinhood"]
    url = f"{base}/tokens/{token}/holders"
    data = await fetch_json(session, url)
    if not data or "items" not in data:
        holder_cache[cache_key] = ((0.0, 0.0, 0.0), now)
        return 0.0, 0.0, 0.0
    items = data["items"]
    if not items:
        holder_cache[cache_key] = ((0.0, 0.0, 0.0), now)
        return 0.0, 0.0, 0.0
    token_info = await fetch_json(session, f"{base}/tokens/{token}")
    total_supply = 0.0
    if token_info:
        try:
            total_supply = float(token_info.get("total_supply") or 0)
        except (ValueError, TypeError):
            total_supply = 0.0
    if total_supply == 0:
        holder_cache[cache_key] = ((0.0, 0.0, 0.0), now)
        return 0.0, 0.0, 0.0
    balances = [float(h.get("value", 0)) for h in items]
    top10 = sum(balances[:10])
    top50 = sum(balances[:50]) if len(balances) >= 50 else sum(balances)
    top100 = sum(balances[:100]) if len(balances) >= 100 else sum(balances)
    pct10 = (top10 / total_supply) * 100
    pct50 = (top50 / total_supply) * 100
    pct100 = (top100 / total_supply) * 100
    holder_cache[cache_key] = ((pct10, pct50, pct100), now)
    return pct10, pct50, pct100

# ═══════════════════════════════════════════════════════════════════════════════
# CEX LISTINGS
# ═══════════════════════════════════════════════════════════════════════════════

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
# SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def score_volume_liquidity(vol_5m, liquidity):
    ratio = vol_5m / liquidity if liquidity > 0 else 0
    if ratio >= 1.0: return VOL_LIQUIDITY_PTS
    elif ratio >= 0.5: return VOL_LIQUIDITY_PTS * 0.8
    elif ratio >= 0.25: return VOL_LIQUIDITY_PTS * 0.6
    elif ratio >= 0.1: return VOL_LIQUIDITY_PTS * 0.4
    else: return VOL_LIQUIDITY_PTS * 0.2

def score_5m_1h(vol_5m, vol_1h, age_minutes):
    if age_minutes is not None and age_minutes > 10 and vol_1h > 0:
        ratio = vol_5m / vol_1h
        normalized = ratio / 0.0833 if ratio > 0 else 0
        if normalized >= 8: return VOL_5M_1H_PTS
        elif normalized >= 4: return VOL_5M_1H_PTS * 0.75
        elif normalized >= 2: return VOL_5M_1H_PTS * 0.5
        elif normalized >= 1: return VOL_5M_1H_PTS * 0.25
    elif age_minutes is not None and age_minutes <= 10:
        if vol_1h > 0 and vol_5m / vol_1h > 0.9: return VOL_5M_1H_PTS * 0.3
    return 0

def score_1h_6h(vol_1h, vol_6h, age_minutes):
    if age_minutes is not None and age_minutes > 60 and vol_6h > 0:
        ratio = vol_1h / vol_6h
        normalized = ratio / 0.1667 if ratio > 0 else 0
        if normalized >= 6: return VOL_1H_6H_PTS
        elif normalized >= 3: return VOL_1H_6H_PTS * 0.75
        elif normalized >= 1.5: return VOL_1H_6H_PTS * 0.5
        elif normalized >= 1.0: return VOL_1H_6H_PTS * 0.25
    elif age_minutes is not None and age_minutes <= 60:
        if vol_6h > 0 and vol_1h / vol_6h > 0.8: return VOL_1H_6H_PTS * 0.3
    return 0

def score_6h_24h(vol_6h, vol_24h, age_minutes):
    if age_minutes is not None and age_minutes > 360 and vol_24h > 0:
        ratio = vol_6h / vol_24h
        normalized = ratio / 0.25 if ratio > 0 else 0
        if normalized >= 4: return VOL_6H_24H_PTS
        elif normalized >= 2: return VOL_6H_24H_PTS * 0.6
        elif normalized >= 1.2: return VOL_6H_24H_PTS * 0.3
    elif age_minutes is not None and age_minutes <= 360:
        if vol_24h > 0 and vol_6h / vol_24h > 0.8: return VOL_6H_24H_PTS * 0.3
    return 0

def score_holder(top10, top50, top100, age_minutes):
    pts = 0
    age_discount = 0.3 if age_minutes and age_minutes < 120 else 1.0
    if top10 >= 80: pts += HOLDER_TOP10_PTS * age_discount
    elif top10 >= 60: pts += HOLDER_TOP10_PTS * 0.75 * age_discount
    elif top10 >= 40: pts += HOLDER_TOP10_PTS * 0.5 * age_discount
    elif top10 >= 25: pts += HOLDER_TOP10_PTS * 0.25 * age_discount
    if top50 >= 90: pts += HOLDER_TOP50_PTS
    elif top50 >= 75: pts += HOLDER_TOP50_PTS * 0.75
    elif top50 >= 60: pts += HOLDER_TOP50_PTS * 0.5
    if top100 >= 95: pts += HOLDER_TOP100_PTS
    elif top100 >= 85: pts += HOLDER_TOP100_PTS * 0.75
    elif top100 >= 70: pts += HOLDER_TOP100_PTS * 0.5
    return pts

def score_cex(cex_count, has_perps, tier1):
    pts = min(cex_count, CEX_LISTING_PTS)
    if has_perps: pts += CEX_PERPS_PTS
    return min(pts, 5)

def score_price(chg_5m, chg_1h, chg_6h, age_minutes):
    pts = 0
    if chg_5m > 0:
        if chg_5m >= 50: pts += PRICE_5M_PTS
        elif chg_5m >= 20: pts += PRICE_5M_PTS * 0.7
        elif chg_5m >= 5: pts += PRICE_5M_PTS * 0.4
    if chg_1h > 0 and age_minutes is not None and age_minutes > 60:
        if chg_1h >= 100: pts += PRICE_1H_PTS
        elif chg_1h >= 50: pts += PRICE_1H_PTS * 0.7
        elif chg_1h >= 10: pts += PRICE_1H_PTS * 0.4
    if chg_6h > 0 and age_minutes is not None and age_minutes > 360:
        if chg_6h >= 200: pts += PRICE_6H_PTS
        elif chg_6h >= 100: pts += PRICE_6H_PTS * 0.7
        elif chg_6h >= 50: pts += PRICE_6H_PTS * 0.4
    return pts

# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN EVALUATION
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

    min_liq = ROBINHOOD_MIN_LIQUIDITY_USD if chain == "robinhood" else MIN_LIQUIDITY_USD
    liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
    if liquidity < min_liq: return None
    price = float(pair.get("priceUsd") or 0)
    if price < MIN_PRICE: return None
    market_cap = float(pair.get("marketCap") or 0)
    if market_cap < MIN_MARKET_CAP_USD: return None
    vol_5m = float(pair.get("volume", {}).get("m5") or 0)
    if vol_5m < MIN_VOL_5M_USD: return None

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
    pair_created = pair.get("pairCreatedAt")
    age_minutes = (time.time() - pair_created / 1000) / 60 if pair_created else None
    if chain == "robinhood" and age_minutes is not None and age_minutes < ROBINHOOD_MIN_PAIR_AGE_MIN:
        return None

    score = 0; penalties = 0
    score += score_volume_liquidity(vol_5m, liquidity)
    score += score_5m_1h(vol_5m, vol_1h, age_minutes)
    score += score_1h_6h(vol_1h, vol_6h, age_minutes)
    score += score_6h_24h(vol_6h, vol_24h, age_minutes)

    total_5m = buys_5m + sells_5m
    if total_5m > 0:
        buy_ratio_5m = buys_5m / total_5m
        if buy_ratio_5m >= 0.85: score += BUY_PRESSURE_5M_PTS
        elif buy_ratio_5m >= 0.70: score += BUY_PRESSURE_5M_PTS * 0.75
        elif buy_ratio_5m >= 0.55: score += BUY_PRESSURE_5M_PTS * 0.5
        elif buy_ratio_5m >= 0.45: score += BUY_PRESSURE_5M_PTS * 0.25
        if buy_ratio_5m < 0.35: penalties += PENALTY_SELL_PRESSURE_5M
        if total_5m < 5: penalties += PENALTY_LOW_TX_5M
    else:
        penalties += PENALTY_LOW_TX_5M

    total_1h = buys_1h + sells_1h
    if age_minutes is not None and age_minutes > 60 and total_1h > 0:
        buy_ratio_1h = buys_1h / total_1h
        if buy_ratio_1h >= 0.80: score += BUY_PRESSURE_1H_PTS
        elif buy_ratio_1h >= 0.65: score += BUY_PRESSURE_1H_PTS * 0.75
        elif buy_ratio_1h >= 0.50: score += BUY_PRESSURE_1H_PTS * 0.5
        if buy_ratio_1h < 0.35: penalties += PENALTY_SELL_PRESSURE_1H
        if total_1h < 10: penalties += PENALTY_LOW_TX_1H

    score += score_price(chg_5m, chg_1h, chg_6h, age_minutes)

    security = await get_token_security(session, chain, token)
    if security:
        if security.get("is_honeypot"): return None
        if security.get("buy_tax", 0) > MAX_ALLOWED_TAX or security.get("sell_tax", 0) > MAX_ALLOWED_TAX: return None
        if chain != "robinhood":
            if any([
                security.get("is_whitelisted"), security.get("is_blacklisted"),
                security.get("is_proxy"), security.get("can_take_back_ownership"),
                security.get("owner_change_balance"), security.get("is_mintable"),
                security.get("slippage_modifiable"), security.get("transfer_pausable"),
            ]): return None
        else:
            if not security.get("is_verified", False): penalties += PENALTY_UNVERIFIED_CONTRACT
        score += SECURITY_PTS
    else:
        return None

    base_score = score - penalties
    if base_score < PHASE1_MIN_SCORE:
        if VERBOSE_LOGGING:
            logger.info(f"Phase gate skip {symbol}@{chain}: base_score={base_score:.1f}")
        return None

    top10, top50, top100 = await get_holder_concentration(session, chain, token)
    holder_pts = score_holder(top10, top50, top100, age_minutes)
    score += holder_pts

    cex_count, has_perps, tier1 = await get_cex_listings(session, chain, token)
    cex_pts = score_cex(cex_count, has_perps, tier1)
    score += cex_pts

    total_score = max(0, score - penalties)

    if VERBOSE_LOGGING:
        logger.info(
            f"{symbol}@{chain} score={total_score:.0f} | "
            f"vol={vol_5m/liquidity:.2f}xliq 5m/1h={vol_5m/vol_1h if vol_1h>0 else 0:.2f} "
            f"1h/6h={vol_1h/vol_6h if vol_6h>0 else 0:.2f} 6h/24h={vol_6h/vol_24h if vol_24h>0 else 0:.2f} | "
            f"buy5m={buys_5m}/{sells_5m} buy1h={buys_1h}/{sells_1h} | "
            f"age={age_minutes:.0f}m | holders top10={top10:.1f}% top50={top50:.1f}% top100={top100:.1f}% | "
            f"cex={cex_count} perps={has_perps} | base={base_score:.1f} penalties={penalties}"
        )

    if total_score < ALERT_THRESHOLD:
        return None

    return {
        "chain": chain, "token_address": token, "symbol": symbol, "name": name,
        "pair_address": pair_id, "total_score": total_score,
        "vol_5m": vol_5m, "liquidity": liquidity, "market_cap": market_cap,
        "buys_5m": buys_5m, "sells_5m": sells_5m,
        "buys_1h": buys_1h, "sells_1h": sells_1h,
        "chg_5m": chg_5m, "chg_1h": chg_1h, "chg_6h": chg_6h,
        "security": security, "holder_pct": (top10, top50, top100),
        "cex_count": cex_count, "has_perps": has_perps, "tier1": tier1,
        "age_minutes": age_minutes,
        "dex_url": f"https://dexscreener.com/{chain}/{pair_id}",
        "price_usd": price,
        "price_native": float(pair.get("priceNative") or 0),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# V3 TRADING — exactInputSingle + QuoterV2 slippage protection
# ═══════════════════════════════════════════════════════════════════════════════

async def get_wallet_balance(chain: str) -> float:
    w3 = w3_instances.get(chain)
    if not w3 or not WALLET_ADDRESS: return 0.0
    try:
        bal = await asyncio.to_thread(w3.eth.get_balance, WALLET_ADDRESS)
        return w3.from_wei(bal, 'ether')
    except Exception as e:
        logger.warning(f"Balance check failed for {chain}: {e}")
        return 0.0

async def get_token_balance(chain: str, token_address: str) -> Tuple[float, int]:
    w3 = w3_instances.get(chain)
    if not w3 or not WALLET_ADDRESS: return 0.0, 18
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        bal = await asyncio.to_thread(token.functions.balanceOf(WALLET_ADDRESS).call)
        decimals = await asyncio.to_thread(token.functions.decimals().call)
        return bal / (10 ** decimals), decimals
    except Exception as e:
        logger.warning(f"Token balance check failed: {e}")
        return 0.0, 18

async def ensure_token_approval(chain: str, token_address: str, spender: str, amount: int):
    w3 = w3_instances.get(chain)
    if not w3 or not PRIVATE_KEY: return None
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        allowance = await asyncio.to_thread(
            token.functions.allowance(WALLET_ADDRESS, Web3.to_checksum_address(spender)).call
        )
        if allowance >= amount: return None
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, WALLET_ADDRESS)
        gas_price = await asyncio.to_thread(w3.eth.gas_price)
        approve_tx = token.functions.approve(
            Web3.to_checksum_address(spender), 2**256 - 1
        ).build_transaction({
            'from': WALLET_ADDRESS, 'gas': 100000,
            'gasPrice': int(gas_price * 1.1), 'nonce': nonce,
        })
        signed = w3.eth.account.sign_transaction(approve_tx, PRIVATE_KEY)
        tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed.rawTransaction)
        receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
        if receipt.status == 1:
            logger.info(f"Approval confirmed: {tx_hash.hex()}")
            return tx_hash.hex()
        else:
            logger.error(f"Approval failed: {tx_hash.hex()}")
            return None
    except Exception as e:
        logger.error(f"Approval failed: {e}")
        return None

async def quote_exact_output_v3(w3, chain, token_in, token_out, amount_in, fee_tier):
    quoter_addr = QUOTERS_V2.get(chain)
    if not quoter_addr or not Web3.is_address(quoter_addr): return None
    try:
        quoter = w3.eth.contract(address=Web3.to_checksum_address(quoter_addr), abi=QUOTER_V2_ABI)
        params = (Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out), fee_tier, amount_in, 0)
        result = await asyncio.to_thread(quoter.functions.quoteExactInputSingle(params).call)
        return int(result[0])
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["invalid fee", "no pool", "revert", "uniswapv3"]):
            return None
        logger.debug(f"QuoterV2 error for fee={fee_tier}: {e}")
        return None

async def estimate_output_fallback(w3, chain, token_in, token_out, amount_in, price_native, token_decimals):
    if not price_native or price_native <= 0: return None
    try:
        amount_in_eth = w3.from_wei(amount_in, 'ether')
        expected_tokens = float(amount_in_eth) / price_native
        expected_raw = int(expected_tokens * (10 ** token_decimals))
        return expected_raw
    except Exception as e:
        logger.debug(f"Fallback estimation failed: {e}")
        return None

async def try_v3_swap(w3, chain, token_in, token_out, amount_in, is_eth_input, slippage, price_native=None, token_decimals=None):
    router_addr = ROUTERS_V3.get(chain)
    if not router_addr or not Web3.is_address(router_addr):
        return False, f"No V3 router for {chain}", 0
    router = w3.eth.contract(address=Web3.to_checksum_address(router_addr), abi=V3_ROUTER_ABI)
    for fee in V3_FEE_TIERS:
        amount_out = await quote_exact_output_v3(w3, chain, token_in, token_out, amount_in, fee)
        if amount_out is None and price_native and token_decimals:
            amount_out = await estimate_output_fallback(w3, chain, token_in, token_out, amount_in, price_native, token_decimals)
        if amount_out is None: continue
        amount_out_min = int(amount_out * (1 - slippage / 100))
        if amount_out_min <= 0: amount_out_min = 1
        params = (
            Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out),
            fee, Web3.to_checksum_address(WALLET_ADDRESS),
            amount_in, amount_out_min, 0
        )
        try:
            nonce = await asyncio.to_thread(w3.eth.get_transaction_count, WALLET_ADDRESS)
            gas_price = await asyncio.to_thread(w3.eth.gas_price)
            tx_dict = {'from': WALLET_ADDRESS, 'gas': 350000, 'gasPrice': int(gas_price * 1.2), 'nonce': nonce}
            if is_eth_input: tx_dict['value'] = amount_in
            tx = router.functions.exactInputSingle(params).build_transaction(tx_dict)
            signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed.rawTransaction)
            logger.info(f"V3 swap tx sent: {tx_hash.hex()} | fee={fee} | minOut={amount_out_min}")
            receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
            if receipt.status != 1:
                return False, f"Tx failed: {tx_hash.hex()}", 0
            return True, tx_hash.hex(), amount_out
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["invalid fee", "no pool", "revert"]):
                continue
            logger.error(f"V3 swap failed for fee={fee}: {e}")
            return False, str(e), 0
    return False, "No valid V3 pool found for any fee tier", 0

async def execute_buy(chain: str, token_address: str, amount_native: float, slippage: float = None, price_native: float = None):
    if slippage is None:
        slippage = float(db_get_setting("slippage", DEFAULT_SLIPPAGE))
    w3 = w3_instances.get(chain)
    weth_address = WETH.get(chain)
    if not w3 or not weth_address:
        return False, f"No Web3/WETH for {chain}", 0.0
    if PAPER_TRADING:
        simulated_tokens = amount_native * 1000
        db_log_paper_trade(chain, token_address, "???", "BUY", amount_native, simulated_tokens, 0.0)
        logger.info(f"PAPER BUY: {amount_native} {NATIVE_SYMBOL[chain]} → {simulated_tokens} tokens on {chain}")
        return True, "PAPER_TRADE", simulated_tokens
    amount_in_wei = w3.to_wei(amount_native, 'ether')
    balance = await asyncio.to_thread(w3.eth.get_balance, WALLET_ADDRESS)
    if balance < amount_in_wei:
        return False, f"Insufficient {NATIVE_SYMBOL[chain]}: {w3.from_wei(balance, 'ether')}", 0.0
    _, decimals = await get_token_balance(chain, token_address)
    success, tx_hash, tokens_received = await try_v3_swap(
        w3, chain, weth_address, token_address, amount_in_wei,
        is_eth_input=True, slippage=slippage, price_native=price_native, token_decimals=decimals
    )
    if success:
        human_tokens = tokens_received / (10 ** decimals) if tokens_received > 0 else 0
        logger.info(f"Buy confirmed: {human_tokens} tokens for {amount_native} {NATIVE_SYMBOL[chain]}")
        return True, tx_hash, human_tokens
    else:
        return False, tx_hash, 0.0

async def execute_sell(chain: str, token_address: str, percentage: float = 100.0, slippage: float = None):
    if slippage is None:
        slippage = float(db_get_setting("slippage", DEFAULT_SLIPPAGE))
    w3 = w3_instances.get(chain)
    router_addr = ROUTERS_V3.get(chain)
    weth_address = WETH.get(chain)
    if not w3 or not router_addr or not weth_address:
        return False, f"No Web3/router for {chain}", 0.0
    if PAPER_TRADING:
        db_log_paper_trade(chain, token_address, "???", "SELL", 0.0, 0.0, 0.0)
        logger.info(f"PAPER SELL: {percentage}% on {chain}")
        return True, "PAPER_TRADE", 0.0
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
    try:
        bal = await asyncio.to_thread(token.functions.balanceOf(WALLET_ADDRESS).call)
        decimals = await asyncio.to_thread(token.functions.decimals().call)
    except Exception as e:
        return False, f"Balance check failed: {e}", 0.0
    if bal == 0: return False, "Zero balance", 0.0
    sell_amount = int(bal * (percentage / 100))
    if sell_amount == 0: return False, "Sell amount too small", 0.0
    await ensure_token_approval(chain, token_address, router_addr, sell_amount)
    success, tx_hash, weth_received = await try_v3_swap(
        w3, chain, token_address, weth_address, sell_amount,
        is_eth_input=False, slippage=slippage
    )
    if success:
        native_received = w3.from_wei(weth_received, 'ether') if weth_received > 0 else 0
        logger.info(f"Sell confirmed: {percentage}% sold, received {native_received} W{NATIVE_SYMBOL[chain]}")
        return True, tx_hash, float(native_received)
    else:
        return False, tx_hash, 0.0

async def open_position(chain, token_address, symbol, amount_native, price_usd, price_native=None):
    trailing_stop = float(db_get_setting("trailing_stop", DEFAULT_TRAILING_STOP))
    tp_levels_raw = db_get_setting("take_profit_levels", json.dumps(DEFAULT_TP_LEVELS))
    tp_levels = json.loads(tp_levels_raw)
    success, tx_hash, tokens_received = await execute_buy(chain, token_address, amount_native, price_native=price_native)
    if not success:
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ <b>Buy failed for {symbol}</b>\n{tx_hash}", parse_mode=ParseMode.HTML)
        return None
    pos_id = db_add_position(
        chain, token_address, symbol, price_usd, tokens_received,
        amount_native, trailing_stop, tp_levels, tx_hash,
        paper=1 if PAPER_TRADING else 0
    )
    mode = "📄 PAPER" if PAPER_TRADING else "💰 LIVE"
    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"{mode} <b>Position Opened</b>\n"
            f"{symbol} on {chain.upper()}\n"
            f"Spent: {amount_native} {NATIVE_SYMBOL[chain]}\n"
            f"Received: {tokens_received:.4f} {symbol}\n"
            f"Entry: ${price_usd:.6f}\n"
            f"Trailing stop: {trailing_stop}%\n"
            f"Tx: <code>{tx_hash}</code>"
        ),
        parse_mode=ParseMode.HTML
    )
    return pos_id

async def close_position_manual(pos_id: int):
    cur = db_conn.execute(
        "SELECT chain, token_address, symbol, remaining_pct FROM positions WHERE id=?",
        (pos_id,)
    )
    row = cur.fetchone()
    if not row: return False, "Position not found"
    chain, token_address, symbol, remaining_pct = row
    if remaining_pct <= 0: return False, "Position already closed"
    success, tx_hash, native_received = await execute_sell(chain, token_address, 100.0)
    if not success: return False, f"Sell failed: {tx_hash}"
    db_close_position(pos_id, 0.0, tx_hash)
    mode = "📄 PAPER" if PAPER_TRADING else "💰 LIVE"
    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"{mode} <b>Position Closed</b>\n"
            f"{symbol} on {chain.upper()}\n"
            f"Sold: {remaining_pct:.1f}%\n"
            f"Received: {native_received:.4f} W{NATIVE_SYMBOL[chain]}\n"
            f"Tx: <code>{tx_hash}</code>"
        ),
        parse_mode=ParseMode.HTML
    )
    return True, "Closed"

async def sell_position_pct(pos_id: int, pct: float):
    pos = db_get_position(pos_id)
    if not pos: return False, "Position not found"
    if pos['remaining_pct'] <= 0: return False, "Already closed"
    actual_pct = min(pct, pos['remaining_pct'])
    success, tx_hash, native_received = await execute_sell(pos['chain'], pos['token_address'], actual_pct)
    if not success: return False, f"Sell failed: {tx_hash}"
    # Calculate rough PnL
    entry = pos['entry_price']
    current_price = await get_token_price_usd(None, pos['chain'], pos['token_address'])
    pnl = ((current_price - entry) / entry * 100) if entry > 0 and current_price > 0 else 0
    db_reduce_position(pos_id, actual_pct, pnl, tx_hash)
    mode = "📄 PAPER" if pos['paper_trade'] else "💰 LIVE"
    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"{mode} <b>Sold {actual_pct:.0f}% of {pos['symbol']}</b>\n"
            f"Remaining: {pos['remaining_pct'] - actual_pct:.1f}%\n"
            f"Received: {native_received:.4f} W{NATIVE_SYMBOL[pos['chain']]}\n"
            f"Tx: <code>{tx_hash}</code>"
        ),
        parse_mode=ParseMode.HTML
    )
    return True, "Sold"

# ═══════════════════════════════════════════════════════════════════════════════
# POSITION MONITOR (Trailing Stop + Take Profits)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_token_price_usd(session, chain, token_address):
    try:
        url = f"https://api.dexscreener.com/tokens/v1/{chain}/{token_address}"
        data = await fetch_json(session, url)
        if data and isinstance(data, list) and len(data) > 0:
            return float(data[0].get("priceUsd") or 0)
    except Exception as e:
        logger.debug(f"Price fetch failed: {e}")
    return 0.0

async def monitor_positions(session):
    while not shutdown_flag:
        try:
            positions = db_get_open_positions()
            for pos in positions:
                current_price = await get_token_price_usd(session, pos['chain'], pos['token_address'])
                if current_price <= 0: continue
                entry_price = pos['entry_price']
                highest_price = pos['highest_price']
                if current_price > highest_price:
                    db_update_position_price(pos['id'], current_price, current_price)
                    highest_price = current_price
                pnl_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                drop_from_peak = ((highest_price - current_price) / highest_price) * 100 if highest_price > 0 else 0
                trailing_stop = pos['trailing_stop_pct']
                # Check take profits
                tp_levels = json.loads(pos['take_profit_levels'])
                tp_triggered = False
                for level in tp_levels:
                    if level.get('executed'): continue
                    if pnl_pct >= level['pct']:
                        sell_pct = level['sell_pct']
                        remaining = pos['remaining_pct']
                        actual_sell = min(sell_pct, remaining)
                        if actual_sell > 0:
                            success, tx_hash, native_received = await execute_sell(pos['chain'], pos['token_address'], actual_sell)
                            if success:
                                level['executed'] = True
                                db_reduce_position(pos['id'], actual_sell, pnl_pct, tx_hash)
                                db_conn.execute("UPDATE positions SET take_profit_levels=? WHERE id=?", (json.dumps(tp_levels), pos['id']))
                                db_conn.commit()
                                mode = "📄 PAPER" if pos['paper_trade'] else "💰 LIVE"
                                await bot.send_message(
                                    chat_id=CHAT_ID,
                                    text=(
                                        f"🎯 {mode} <b>Take Profit Hit!</b>\n"
                                        f"{pos['symbol']} on {pos['chain'].upper()}\n"
                                        f"P&L: +{pnl_pct:.1f}%\n"
                                        f"Sold: {actual_sell:.1f}%\n"
                                        f"Received: {native_received:.4f} W{NATIVE_SYMBOL[pos['chain']]}\n"
                                        f"Tx: <code>{tx_hash}</code>"
                                    ),
                                    parse_mode=ParseMode.HTML
                                )
                                tp_triggered = True
                                break
                if tp_triggered: continue
                # Check trailing stop
                if drop_from_peak >= trailing_stop and pos['remaining_pct'] > 0:
                    success, tx_hash, native_received = await execute_sell(pos['chain'], pos['token_address'], 100.0)
                    if success:
                        db_close_position(pos['id'], pnl_pct, tx_hash)
                        mode = "📄 PAPER" if pos['paper_trade'] else "💰 LIVE"
                        emoji = "🛑" if pnl_pct >= 0 else "🔴"
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text=(
                                f"{emoji} {mode} <b>Trailing Stop Hit!</b>\n"
                                f"{pos['symbol']} on {pos['chain'].upper()}\n"
                                f"Peak: ${highest_price:.6f}\n"
                                f"Current: ${current_price:.6f}\n"
                                f"Drop: {drop_from_peak:.1f}%\n"
                                f"Final P&L: {pnl_pct:+.1f}%\n"
                                f"Tx: <code>{tx_hash}</code>"
                            ),
                            parse_mode=ParseMode.HTML
                        )
            await asyncio.sleep(POSITION_CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Position monitor error: {e}")
            await asyncio.sleep(POSITION_CHECK_INTERVAL)

# ═══════════════════════════════════════════════════════════════════════════════
# CA PASTE DETECTION (BonkBot Style)
# ═══════════════════════════════════════════════════════════════════════════════

CA_REGEX = re.compile(r'0x[a-fA-F0-9]{40}')

async def detect_chain_for_ca(session, ca: str):
    """Try all chains on DexScreener, return first match."""
    for chain in NETWORKS:
        try:
            url = f"https://api.dexscreener.com/tokens/v1/{chain}/{ca}"
            data = await fetch_json(session, url)
            if data and isinstance(data, list) and len(data) > 0:
                return chain, data[0]
        except Exception:
            continue
    return None, None

async def handle_ca_paste(ca: str, message):
    """When user pastes a CA, fetch token and show buy UI."""
    await bot.send_message(chat_id=CHAT_ID, text=f"🔍 Looking up <code>{ca}</code>...", parse_mode=ParseMode.HTML)

    async with aiohttp.ClientSession() as temp_session:
        chain, pair = await detect_chain_for_ca(temp_session, ca)
        if not chain or not pair:
            await bot.send_message(chat_id=CHAT_ID, text=f"❌ Token not found on any supported chain.", parse_mode=ParseMode.HTML)
            return

        symbol = pair.get("baseToken", {}).get("symbol", "???")
        name = pair.get("baseToken", {}).get("name", "Unknown")
        price_usd = float(pair.get("priceUsd") or 0)
        price_native = float(pair.get("priceNative") or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
        market_cap = float(pair.get("marketCap") or 0)
        vol_5m = float(pair.get("volume", {}).get("m5") or 0)
        chg_5m = float(pair.get("priceChange", {}).get("m5") or 0)

        text = (
            f"🪙 <b>{name}</b> ({symbol})\n"
            f"🔗 Chain: <b>{chain.upper()}</b>\n"
            f"💰 Price: ${price_usd:.6f}\n"
            f"💧 Liquidity: ${liquidity:,.0f}\n"
            f"📊 Market Cap: ${market_cap:,.0f}\n"
            f"📈 5m Volume: ${vol_5m:,.0f}\n"
            f"📈 5m Change: {chg_5m:+.1f}%\n\n"
            f"📝 <b>CA:</b> <code>{ca}</code>"
        )

        keyboard = build_buy_keyboard(chain, ca, symbol, price_native)
        await bot.send_message(
            chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=keyboard
        )

# ═══════════════════════════════════════════════════════════════════════════════
# BUY KEYBOARD BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_buy_amounts(chain: str):
    """Get preset buy amounts for a chain, with USD risk conversion."""
    amounts_key = f"buy_amounts_{NATIVE_SYMBOL[chain].lower()}"
    amounts_raw = db_get_setting(amounts_key, json.dumps(DEFAULT_BUY_AMOUNTS.get(chain, [0.001, 0.003, 0.005, 0.01])))
    return json.loads(amounts_raw)

def build_buy_keyboard(chain, token_address, symbol, price_native=None):
    """Build inline keyboard with buy buttons."""
    amounts = get_buy_amounts(chain)
    keyboard = []
    row = []
    for amt in amounts:
        label = f"💰 {amt} {NATIVE_SYMBOL[chain]}"
        callback = f"buy:{chain}:{token_address}:{amt}"
        row.append(InlineKeyboardButton(label, callback_data=callback))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Custom amount button
    keyboard.append([InlineKeyboardButton("✏️ Custom Amount", callback_data=f"custom:{chain}:{token_address}")])
    keyboard.append([
        InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/{chain}/{token_address}"),
        InlineKeyboardButton("🚫 Skip", callback_data="noop"),
    ])
    return InlineKeyboardMarkup(keyboard)

def build_alert_keyboard(chain, token_address, symbol):
    """Build inline keyboard for scan alerts."""
    return build_buy_keyboard(chain, token_address, symbol)

# ═══════════════════════════════════════════════════════════════════════════════
# POSITIONS KEYBOARD
# ═══════════════════════════════════════════════════════════════════════════════

def build_positions_keyboard(positions):
    """List of positions as clickable buttons."""
    keyboard = []
    for pos in positions:
        mode = "📄" if pos['paper_trade'] else "💰"
        label = f"{mode} #{pos['id']} {pos['symbol']} ({pos['remaining_pct']:.0f}%)"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"pos:{pos['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd:start")])
    return InlineKeyboardMarkup(keyboard)

def build_position_actions_keyboard(pos_id):
    """Action buttons for a specific position."""
    keyboard = [
        [InlineKeyboardButton("💸 Sell 25%", callback_data=f"sellpct:{pos_id}:25"),
         InlineKeyboardButton("💸 Sell 50%", callback_data=f"sellpct:{pos_id}:50")],
        [InlineKeyboardButton("💸 Sell 100%", callback_data=f"sellpct:{pos_id}:100"),
         InlineKeyboardButton("💰 Buy More", callback_data=f"buymore:{pos_id}")],
        [InlineKeyboardButton("🛡 Set SL", callback_data=f"setsl:{pos_id}"),
         InlineKeyboardButton("🔙 Back", callback_data="cmd:positions")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_callback_query(query):
    """Process inline button clicks."""
    data = query.data
    if not data: return
    try:
        await bot.answer_callback_query(query.id)
    except:
        pass

    parts = data.split(":")
    action = parts[0]

    if action == "buy" and len(parts) >= 4:
        chain = parts[1]
        token_address = parts[2]
        amount = float(parts[3])
        symbol = "???"; price_usd = 0.0; price_native = 0.0
        try:
            url = f"https://api.dexscreener.com/tokens/v1/{chain}/{token_address}"
            async with aiohttp.ClientSession() as temp_session:
                async with temp_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        d = await resp.json()
                        if d and isinstance(d, list) and len(d) > 0:
                            symbol = d[0].get("baseToken", {}).get("symbol", "???")
                            price_usd = float(d[0].get("priceUsd") or 0)
                            price_native = float(d[0].get("priceNative") or 0)
        except:
            pass
        await bot.send_message(chat_id=CHAT_ID, text=f"⏳ Buying {amount} {NATIVE_SYMBOL[chain]} of {symbol}...", parse_mode=ParseMode.HTML)
        pos_id = await open_position(chain, token_address, symbol, amount, price_usd, price_native)
        if pos_id:
            logger.info(f"Position opened via callback: {symbol} id={pos_id}")

    elif action == "custom" and len(parts) >= 3:
        chain = parts[1]
        token_address = parts[2]
        # Set user state to await custom amount
        user_state[CHAT_ID] = {"action": "custom_buy", "chain": chain, "token": token_address}
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"✏️ <b>Custom Buy</b>\nReply with the amount of {NATIVE_SYMBOL[chain]} you want to spend:",
            parse_mode=ParseMode.HTML
        )

    elif action == "pos" and len(parts) >= 2:
        pos_id = int(parts[1])
        pos = db_get_position(pos_id)
        if not pos:
            await bot.send_message(chat_id=CHAT_ID, text="Position not found.", parse_mode=ParseMode.HTML)
            return
        mode = "📄 PAPER" if pos['paper_trade'] else "💰 LIVE"
        text = (
            f"{mode} <b>Position #{pos['id']}</b>\n"
            f"{pos['symbol']} on {pos['chain'].upper()}\n"
            f"Entry: ${pos['entry_price']:.6f}\n"
            f"Highest: ${pos['highest_price']:.6f}\n"
            f"Amount: {pos['amount_tokens']:.4f} tokens\n"
            f"Remaining: {pos['remaining_pct']:.1f}%\n"
            f"Trailing SL: {pos['trailing_stop_pct']}%"
        )
        keyboard = build_position_actions_keyboard(pos_id)
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    elif action == "sellpct" and len(parts) >= 3:
        pos_id = int(parts[1])
        pct = float(parts[2])
        await sell_position_pct(pos_id, pct)

    elif action == "buymore" and len(parts) >= 2:
        pos_id = int(parts[1])
        pos = db_get_position(pos_id)
        if not pos:
            await bot.send_message(chat_id=CHAT_ID, text="Position not found.", parse_mode=ParseMode.HTML)
            return
        keyboard = build_buy_keyboard(pos['chain'], pos['token_address'], pos['symbol'])
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"💰 <b>Buy More {pos['symbol']}</b>\nSelect amount:",
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )

    elif action == "setsl" and len(parts) >= 2:
        pos_id = int(parts[1])
        user_state[CHAT_ID] = {"action": "set_sl", "pos_id": pos_id}
        await bot.send_message(
            chat_id=CHAT_ID,
            text="🛡 <b>Set Trailing Stop Loss</b>\nReply with the new trailing stop % (e.g., 10, 15, 20):",
            parse_mode=ParseMode.HTML
        )

    elif action == "cmd":
        if len(parts) > 1 and parts[1] == "positions":
            await send_positions_menu()
        elif len(parts) > 1 and parts[1] == "start":
            await send_start_menu()

    elif action == "noop":
        pass

async def handle_text_message(message):
    """Handle regular text messages — CA detection + state replies."""
    text = message.text or ""
    chat_id = message.chat.id

    # Check if we're awaiting user input
    state = user_state.get(str(chat_id))
    if state:
        action = state.get("action")

        if action == "custom_buy":
            try:
                amount = float(text.strip())
                if amount <= 0:
                    await bot.send_message(chat_id=chat_id, text="Amount must be > 0.")
                    return
                chain = state["chain"]
                token = state["token"]
                symbol = "???"; price_usd = 0.0; price_native = 0.0
                try:
                    url = f"https://api.dexscreener.com/tokens/v1/{chain}/{token}"
                    async with aiohttp.ClientSession() as temp_session:
                        async with temp_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                d = await resp.json()
                                if d and isinstance(d, list) and len(d) > 0:
                                    symbol = d[0].get("baseToken", {}).get("symbol", "???")
                                    price_usd = float(d[0].get("priceUsd") or 0)
                                    price_native = float(d[0].get("priceNative") or 0)
                except:
                    pass
                del user_state[str(chat_id)]
                await bot.send_message(chat_id=chat_id, text=f"⏳ Buying {amount} {NATIVE_SYMBOL[chain]} of {symbol}...", parse_mode=ParseMode.HTML)
                await open_position(chain, token, symbol, amount, price_usd, price_native)
            except ValueError:
                await bot.send_message(chat_id=chat_id, text="❌ Invalid amount. Please send a number.")
            return

        elif action == "set_sl":
            try:
                new_sl = float(text.strip())
                if new_sl <= 0 or new_sl > 100:
                    await bot.send_message(chat_id=chat_id, text="SL must be between 0 and 100.")
                    return
                pos_id = state["pos_id"]
                db_update_trailing_stop(pos_id, new_sl)
                del user_state[str(chat_id)]
                await bot.send_message(chat_id=chat_id, text=f"✅ Trailing stop updated to {new_sl}%", parse_mode=ParseMode.HTML)
            except ValueError:
                await bot.send_message(chat_id=chat_id, text="❌ Invalid number.")
            return

    # Check for CA paste
    ca_match = CA_REGEX.search(text)
    if ca_match:
        ca = ca_match.group()
        await handle_ca_paste(ca, message)
        return

    # Handle commands
    if text.startswith("/"):
        await handle_command(message)

async def handle_command(message):
    text = message.text or ""
    if not text.startswith("/"): return
    cmd = text.split()[0].lower()
    args = text.split()[1:]

    if cmd == "/start":
        await send_start_menu()

    elif cmd == "/positions":
        await send_positions_menu()

    elif cmd == "/sell" and args:
        try:
            pos_id = int(args[0])
            success, msg = await close_position_manual(pos_id)
            if not success:
                await bot.send_message(chat_id=CHAT_ID, text=f"❌ {msg}", parse_mode=ParseMode.HTML)
        except ValueError:
            await bot.send_message(chat_id=CHAT_ID, text="Usage: /sell <position_id>", parse_mode=ParseMode.HTML)

    elif cmd == "/balance":
        balances = []
        for chain in NETWORKS:
            bal = await get_wallet_balance(chain)
            balances.append(f"{chain.upper()}: {bal:.4f} {NATIVE_SYMBOL[chain]}")
        await bot.send_message(
            chat_id=CHAT_ID,
            text="<b>Wallet Balances</b>\n\n" + "\n".join(balances),
            parse_mode=ParseMode.HTML
        )

    elif cmd == "/settings":
        await send_settings_menu()

    elif cmd == "/risk" and args:
        try:
            usd = float(args[0])
            db_set_setting("risk_usd", str(usd))
            await bot.send_message(chat_id=CHAT_ID, text=f"✅ Risk per trade set to ${usd}", parse_mode=ParseMode.HTML)
        except ValueError:
            await bot.send_message(chat_id=CHAT_ID, text="Usage: /risk <usd_amount>", parse_mode=ParseMode.HTML)

    elif cmd == "/setamounts" and len(args) >= 2:
        chain = args[0].lower()
        amounts_str = " ".join(args[1:])
        try:
            amounts = [float(x.strip()) for x in amounts_str.split(",")]
            key = f"buy_amounts_{NATIVE_SYMBOL.get(chain, chain).lower()}"
            db_set_setting(key, json.dumps(amounts))
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"✅ Buy amounts for {chain.upper()} updated: {amounts}",
                parse_mode=ParseMode.HTML
            )
        except ValueError:
            await bot.send_message(chat_id=CHAT_ID, text="Usage: /setamounts <chain> <amt1,amt2,amt3>", parse_mode=ParseMode.HTML)

async def send_start_menu():
    balances = []
    for chain in NETWORKS:
        bal = await get_wallet_balance(chain)
        if bal > 0:
            balances.append(f"  {chain.upper()}: {bal:.4f} {NATIVE_SYMBOL[chain]}")
    bal_text = "\n".join(balances) if balances else "  (empty or RPC error)"
    risk = db_get_setting("risk_usd", str(DEFAULT_RISK_USD))

    text = (
        f"🚀 <b>Pump Bot v5.2</b> — Manual Trader\n\n"
        f"Wallet: <code>{WALLET_ADDRESS or 'Not configured'}</code>\n"
        f"Mode: {'📄 PAPER' if PAPER_TRADING else '💰 LIVE'}\n"
        f"Risk/Trade: ${risk}\n\n"
        f"<b>Balances:</b>\n{bal_text}\n\n"
        f"<b>How to use:</b>\n"
        f"1. Paste any CA in this chat\n"
        f"2. Tap buy amount\n"
        f"3. Bot manages exits automatically\n\n"
        f"Commands:\n"
        f"/positions — Manage open positions\n"
        f"/balance — Check balances\n"
        f"/settings — View config\n"
        f"/risk <usd> — Set $ risk per trade\n"
        f"/setamounts <chain> <a,b,c> — Custom buy sizes"
    )
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def send_positions_menu():
    positions = db_get_open_positions()
    if not positions:
        await bot.send_message(chat_id=CHAT_ID, text="No open positions.", parse_mode=ParseMode.HTML)
        return
    text = "📊 <b>Your Positions</b>\nTap one to manage:"
    keyboard = build_positions_keyboard(positions)
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def send_settings_menu():
    slippage = db_get_setting("slippage", DEFAULT_SLIPPAGE)
    trailing = db_get_setting("trailing_stop", DEFAULT_TRAILING_STOP)
    tp = db_get_setting("take_profit_levels", json.dumps(DEFAULT_TP_LEVELS))
    tp_pretty = "\n".join([f"  Sell {l['sell_pct']}% at +{l['pct']}%" for l in json.loads(tp)])
    risk = db_get_setting("risk_usd", str(DEFAULT_RISK_USD))

    text = (
        f"⚙️ <b>Current Settings</b>\n\n"
        f"Slippage: {slippage}%\n"
        f"Trailing Stop: {trailing}%\n"
        f"Take Profit Levels:\n{tp_pretty}\n\n"
        f"Risk per Trade: ${risk}\n"
        f"Paper Trading: {'✅ ON' if PAPER_TRADING else '❌ OFF'}\n"
        f"Wallet: <code>{WALLET_ADDRESS or 'Not set'}</code>"
    )
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def telegram_polling_task():
    offset = 0
    while not shutdown_flag:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=["callback_query", "message"])
            for update in updates:
                offset = update.update_id + 1
                if update.callback_query:
                    await handle_callback_query(update.callback_query)
                elif update.message and update.message.text:
                    await handle_text_message(update.message)
        except Exception as e:
            logger.error(f"Telegram polling error: {e}")
            await asyncio.sleep(5)

# ═══════════════════════════════════════════════════════════════════════════════
# ALERT SENDER (manual buy buttons only)
# ═══════════════════════════════════════════════════════════════════════════════

async def send_alert(alert):
    global alerts_sent
    sec = alert.get("security") or {}
    pct10, pct50, pct100 = alert.get("holder_pct", (0, 0, 0))
    buy_pct_5m = (alert['buys_5m'] / (alert['buys_5m'] + alert['sells_5m']) * 100) if (alert['buys_5m'] + alert['sells_5m']) > 0 else 0
    buy_pct_1h = (alert['buys_1h'] / (alert['buys_1h'] + alert['sells_1h']) * 100) if (alert['buys_1h'] + alert['sells_1h']) > 0 else 0

    if sec.get("source") == "blockscout":
        sec_text = (
            f"<b>Security (Blockscout):</b>\n"
            f"  Verified: {'✅' if sec.get('is_verified') else '⚠️ No'}\n"
            f"  Proxy: {'❌ YES' if sec.get('is_proxy') else '✅ No'}\n"
        )
    else:
        sec_text = (
            f"<b>Security (GoPlus):</b> {'✅' if sec else '❓'}\n"
            f"  Honeypot: {'✅ No' if not sec or not sec.get('is_honeypot') else '❌ YES'}\n"
            f"  Buy Tax: {sec.get('buy_tax', 0):.1f}%\n"
            f"  Sell Tax: {sec.get('sell_tax', 0):.1f}%\n"
        )

    text = (
        f"🚨 <b>ONCHAIN PUMP — Score {alert['total_score']}/100</b>\n\n"
        f"<b>{alert['name']}</b> ({alert['symbol']})\n"
        f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n"
        f"🕒 Age: <b>{alert['age_minutes']:.0f} min</b>\n\n"
        f"<b>Liquidity:</b> ${alert['liquidity']:,.0f}\n"
        f"<b>Market Cap:</b> ${alert['market_cap']:,.0f}\n"
        f"<b>Volume (5m):</b> ${alert['vol_5m']:,.0f}\n"
        f"<b>Price Δ 5m:</b> {alert['chg_5m']:+.1f}%\n"
        f"<b>Price Δ 1h:</b> {alert['chg_1h']:+.1f}%\n"
        f"<b>Price Δ 6h:</b> {alert['chg_6h']:+.1f}%\n\n"
        f"<b>Buy Pressure 5m:</b> {buy_pct_5m:.1f}% ({alert['buys_5m']}B / {alert['sells_5m']}S)\n"
        f"<b>Buy Pressure 1h:</b> {buy_pct_1h:.1f}% ({alert['buys_1h']}B / {alert['sells_1h']}S)\n\n"
        f"{sec_text}\n"
        f"<b>Holder Concentration:</b>\n"
        f"  Top 10: {pct10:.1f}%\n"
        f"  Top 50: {pct50:.1f}%\n"
        f"  Top 100: {pct100:.1f}%\n\n"
        f"<b>CEX Listings:</b> {alert['cex_count']} (perps: {'✅' if alert['has_perps'] else '❌'})\n\n"
        f"📝 <b>Contract:</b> <code>{alert['token_address']}</code>"
    )

    keyboard = build_alert_keyboard(alert['chain'], alert['token_address'], alert['symbol'])
    try:
        await bot.send_message(
            chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=keyboard
        )
        alerts_sent += 1
        logger.info(f"ALERT #{alerts_sent} sent → {alert['symbol']}@{alert['chain']} score={alert['total_score']}")
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
    logger.info("Pump Bot v5.2 starting (Manual Trader + CA Paste)...")

    async with aiohttp.ClientSession() as session:
        if COINGECKO_API_KEY:
            await build_coingecko_id_map(session)
        monitor_task = asyncio.create_task(monitor_positions(session))
        polling_task = asyncio.create_task(telegram_polling_task())

        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"✅ <b>Pump Bot v5.2</b> started\n"
                    f"Mode: {'📄 PAPER' if PAPER_TRADING else '💰 LIVE'}\n"
                    f"Wallet: <code>{WALLET_ADDRESS or 'Not set'}</code>\n"
                    f"Scan interval: {SCAN_INTERVAL}s\n"
                    f"Alert threshold: {ALERT_THRESHOLD}/100\n\n"
                    f"<b>Paste any CA to buy instantly!</b>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

        while not shutdown_flag:
            try:
                cycle_start = time.time()
                cycle_alerts = 0
                prune_caches()

                if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                    uptime = (time.time() - start_time) / 3600
                    positions = db_get_open_positions()
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=(
                            f"🫀 <b>Bot v5.2 Heartbeat</b>\n"
                            f"Uptime: {uptime:.1f}h\n"
                            f"Pairs scanned: {total_pairs_scanned}\n"
                            f"Alerts sent: {alerts_sent}\n"
                            f"Open positions: {len(positions)}"
                        ),
                        parse_mode=ParseMode.HTML
                    )
                    last_heartbeat_time = time.time()

                for network in NETWORKS:
                    if shutdown_flag: break
                    pairs = await get_all_pairs(session, network)
                    total_pairs_scanned += len(pairs)
                    logger.info(f"{network}: {len(pairs)} pairs to evaluate")
                    tasks = [evaluate_token(session, p) for p in pairs]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if not result or isinstance(result, Exception): continue
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
                logger.info(f"Cycle complete in {cycle_duration:.1f}s. Alerts: {cycle_alerts}. Sleeping {SCAN_INTERVAL}s...")
                await asyncio.sleep(max(0, SCAN_INTERVAL - cycle_duration))
            except Exception as e:
                logger.error(f"CRITICAL CYCLE ERROR: {e}", exc_info=True)
                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"⚠️ <b>Bot cycle crashed</b>\n<code>{str(e)[:300]}</code>\nRetrying in 60s...",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass
                await asyncio.sleep(60)

        monitor_task.cancel()
        polling_task.cancel()
        try:
            await monitor_task
            await polling_task
        except asyncio.CancelledError:
            pass

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
    positions = db_get_open_positions() if db_conn else []
    return {
        "status": "alive",
        "alerts_sent": alerts_sent,
        "pairs_scanned": total_pairs_scanned,
        "open_positions": len(positions),
        "threshold": ALERT_THRESHOLD,
        "paper_trading": PAPER_TRADING,
        "wallet": WALLET_ADDRESS,
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting Pump Bot v5.2 on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
