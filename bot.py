#!/usr/bin/env python3
"""
================================================================================
PUMP SIGNAL BOT v5.4 — Manual Trader + CA Paste (Bug-Fixed)
================================================================================
Fixes in v5.4:
  • Runtime env var resolution (fixes "No Web3/WETH for robinhood")
  • asyncio.to_thread(lambda: w3.eth.gas_price) fixes "int not callable"
  • raw_transaction / rawTransaction safe getter for web3.py v6
  • Quote-first swap: all fee tiers quoted before ANY tx submitted
  • Parallel CA detection across all chains
  • Gas estimation with 30% buffer + fallback
  • Pending nonce support
  • Revert reason extraction on failed txs
  • All f-strings use \n (no literal line breaks inside strings)
  • /debug command to verify loaded config
================================================================================
"""

import asyncio
import aiohttp
import time
import os
import json
import logging
import logging.handlers
import random
import queue
import sqlite3
import re
import threading
from contextlib import asynccontextmanager
from collections import deque
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Dict, List, NamedTuple, Optional, Tuple, Any
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
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

try:
    import signals
    SIGNALS_AVAILABLE = True
except ImportError:
    SIGNALS_AVAILABLE = False

try:
    import discovery
    DISCOVERY_AVAILABLE = True
except ImportError:
    DISCOVERY_AVAILABLE = False

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

ALERT_THRESHOLD = int(os.getenv("MIN_SCORE", os.getenv("ALERT_THRESHOLD", "65")))

# Opt-in runner/false-positive signal engine (see signals.py) and GeckoTerminal
# discovery (see discovery.py). Both default OFF so behaviour is unchanged
# until you turn them on in .env.
USE_SIGNALS = os.getenv("USE_SIGNALS", "false").lower() == "true" and SIGNALS_AVAILABLE
USE_GECKOTERMINAL = os.getenv("USE_GECKOTERMINAL", "false").lower() == "true" and DISCOVERY_AVAILABLE

# Early-runner fast lane: lets a genuinely strong *young* pool alert even though
# its 1h/6h/24h windows are empty (and so its hand-tuned score can never reach
# MIN_SCORE). Requires USE_SIGNALS and every AND-condition in
# signals.early_runner_reasons() to hold. Default OFF.
EARLY_RUNNER_MODE = os.getenv("EARLY_RUNNER_MODE", "false").lower() == "true" and SIGNALS_AVAILABLE

# Optional Telegram allowlist. Empty -> only CHAT_ID is accepted. Set this when
# CHAT_ID is a group so other members cannot run /sell, /risk, /setamounts.
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("ALLOWED_USER_IDS", "")) if x.strip().isdigit()
}

# Feature logging for offline model training (see FeatureLogger below).
# Default OFF so a long-running bot cannot silently fill the disk.
LOG_FEATURES = os.getenv("LOG_FEATURES", "false").lower() == "true"
FEATURE_DB_PATH = os.getenv("FEATURE_DB_PATH", "").strip()  # empty -> DB_PATH

# Trading safety / cost knobs.
APPROVAL_MULTIPLIER = max(1.0, float(os.getenv("APPROVAL_MULTIPLIER", "1.0")))
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "500"))
PAPER_FEE_PCT = float(os.getenv("PAPER_FEE_PCT", "1.0"))  # modelled fee per side

# Security lookup cache lifetimes. A *failed* lookup is cached much more briefly
# than a good one so a provider outage cannot hide every token for 30 minutes.
SECURITY_TTL = 1800
SECURITY_FAIL_TTL = 120

# Holder concentration rarely changes scan-to-scan, so it caches longer than
# security. Applies to every provider (GeckoTerminal, Moralis, Blockscout).
HOLDER_TTL = 600

# Whether to accept a honeypot.is record when GoPlus has no data for a token.
# OFF by default: the original bot DROPPED unknown tokens, and accepting a
# substitute let brand-new unverified Base/BSC tokens straight through — that is
# what caused most of the extra Base noise.
ALLOW_SECURITY_FALLBACK = os.getenv("ALLOW_SECURITY_FALLBACK", "false").lower() == "true"

# Weight applied to the signal engine's bonus (0.0-1.0). Default 0.0 means the
# signal engine can only REMOVE candidates (hard rejects + penalties); it can
# never promote a sub-threshold token into an alert. The hand-tuned score stays
# the gate, exactly as in the original bot.
SIGNAL_BONUS_WEIGHT = min(1.0, max(0.0, float(os.getenv("SIGNAL_BONUS_WEIGHT", "0.0"))))

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
# honeypot.is uses numeric chain ids; used as a second opinion when GoPlus is down.
HONEYPOT_IS_CHAIN_ID = {"bsc": "56", "ethereum": "1", "base": "8453"}
BLOCKSCOUT_URLS = {"robinhood": "https://robinhoodchain.blockscout.com/api/v2"}

# ── Etherscan v2 ─────────────────────────────────────────────────────────────
# One key covers every EAAS chain, and Robinhood Chain is chainid 4663 (status
# "Ok" in Etherscan's chain list, explorer https://robin.etherscan.io/).
#
# SCANNER_API_KEY is accepted as an alias: that is the name already present in
# the deployed .env, and the value in it is in fact an Etherscan key.
#
# Used for: contract verification (``getsourcecode``) and supply sanity. Note
# that Etherscan's *holder* endpoints (``tokenholderlist``, ``topholders``,
# ``tokenholdercount``) are API-Pro only, so holders come from GeckoTerminal.
ETHERSCAN_API_KEY = (
    os.getenv("ETHERSCAN_API_KEY", "").strip()
    or os.getenv("SCANNER_API_KEY", "").strip()
)
ETHERSCAN_CHAIN_ID = {
    "ethereum": "1", "bsc": "56", "base": "8453", "robinhood": "4663",
}
ETHERSCAN_API = "https://api.etherscan.io/v2/api"

# ── GeckoTerminal token info ─────────────────────────────────────────────────
# Free, keyless, and — unlike Blockscout (Cloudflare 403) and the now-suspended
# Moralis free tier — actually reachable on every supported chain. Publishes the
# holder distribution as exact bands: top_10, 11_30 and 31_50. That yields real
# top10/top50 figures; there is no 51-100 band, so top100 stays *unmeasured*
# (None) rather than being guessed, and the alert gate is scaled down to match.
GT_CHAIN_SLUG = {"ethereum": "eth", "bsc": "bsc", "base": "base", "robinhood": "robinhood"}

RPCS = {
    "ethereum": os.getenv("ETH_RPC", "https://ethereum-rpc.publicnode.com"),
    "bsc": os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org/"),
    "base": os.getenv("BASE_RPC", "https://mainnet.base.org"),
    "robinhood": os.getenv("ROBINHOOD_RPC", "https://rpc.mainnet.chain.robinhood.com"),
}

# Hardcoded fallbacks — env vars take precedence at RUNTIME.
# NOTE: these must match the 7-field exactInputSingle ABI below. The old
# Ethereum SwapRouter (0xE592427A...) uses an 8-field struct WITH deadline and
# silently failed every quote; SwapRouter02 (0x68b34658...) is the correct one.
# The old Ethereum quoter (0x...dc0b0e10) was not a deployed contract at all.
_HARD_ROUTERS = {
    "ethereum": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
    "base": "0x2626664c2603336E57B271c5C0b26F421741e481",
    "bsc": "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
    "robinhood": "0xcaf681a66d020601342297493863e78c959e5cb2",
}
_HARD_QUOTERS = {
    "ethereum": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    "base": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    "bsc": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
    "robinhood": "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7",
}
_HARD_WETH = {
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "base": "0x4200000000000000000000000000000000000006",
    "robinhood": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
}

_HARD_V2_ROUTERS = {
    "ethereum": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    "bsc": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
    "base": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
    "robinhood": "",
}

# Env prefix mapping — matches what v5.2 used and what users actually set
_CHAIN_ENV_PREFIX = {
    "ethereum": "ETH",
    "bsc": "BSC",
    "base": "BASE",
    "robinhood": "ROBINHOOD",
}

def _env_key(chain: str, suffix: str) -> str:
    prefix = _CHAIN_ENV_PREFIX.get(chain, chain.upper())
    return f"{prefix}_{suffix}"

def _chain_floor(chain: str, suffix: str, default: float) -> float:
    """Per-chain threshold override, e.g. BASE_MIN_LIQUIDITY_USD=25000.

    Lets you tighten one noisy chain (Base) without changing the others.
    """
    raw = os.getenv(_env_key(chain, suffix), "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning(f"Ignoring invalid {_env_key(chain, suffix)}={raw!r}")
    return default

# Runtime resolvers — env vars read fresh every time (fixes import-time caching)
def get_router_v3(chain: str) -> str:
    env_key = _env_key(chain, "ROUTER_V3")
    return os.getenv(env_key, "").strip() or _HARD_ROUTERS.get(chain, "")

def get_quoter_v2(chain: str) -> str:
    env_key = _env_key(chain, "QUOTER_V2")
    return os.getenv(env_key, "").strip() or _HARD_QUOTERS.get(chain, "")

def get_weth_address(chain: str) -> str:
    env_key = _env_key(chain, "WNATIVE")
    addr = os.getenv(env_key, "").strip()
    if addr:
        return addr
    return _HARD_WETH.get(chain, "") or os.getenv("ROBINHOOD_WNATIVE", "").strip()

def get_v2_router(chain: str) -> str:
    env_key = _env_key(chain, "ROUTER_V2")
    return os.getenv(env_key, "").strip() or _HARD_V2_ROUTERS.get(chain, "")

NATIVE_SYMBOL = {"ethereum": "ETH", "bsc": "BNB", "base": "ETH", "robinhood": "ETH"}
NATIVE_DECIMALS = {"ethereum": 18, "bsc": 18, "base": 18, "robinhood": 18}

V3_FEE_TIERS_BY_CHAIN = {
    "ethereum": [100, 500, 3000, 10000],
    "base": [100, 500, 3000, 10000],
    "bsc": [100, 500, 2500, 10000],
    "robinhood": [100, 500, 3000, 10000],
}
V3_FEE_TIERS = [100, 500, 2500, 3000, 10000]

# Intermediate tokens tried when a direct V2 path has no pool (e.g. a token that
# only has a USDC pair). Direct path is always tried first.
V2_HOP_STABLES = {
    "ethereum": ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "0xdAC17F958D2ee523a2206206994597C13D831ec7"],
    "bsc": ["0x55d398326f99059fF775485246999027B3197955", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"],
    "base": ["0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"],
    "robinhood": [],
}

SCAN_INTERVAL = 30
HEARTBEAT_INTERVAL = 3600
POSITION_CHECK_INTERVAL = 30

MIN_LIQUIDITY_USD = 3_000.0
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
DEFAULT_RISK_USD = 10.0

user_state: Dict[str, dict] = {}


def state_key(chat_id) -> str:
    """Single key convention for user_state (issue 4.12)."""
    return str(chat_id).strip()


# Short callback references (issue 4.13): Telegram caps callback_data at 64
# bytes and "buy:robinhood:0x<40 hex>:0.005" is already ~62. The long target is
# registered server-side and the button carries only a short ref.
_callback_refs: Dict[str, dict] = {}
_callback_ref_seq = 0


def register_callback_target(chain, token_address, symbol="", price_usd=0.0, price_native=0.0) -> str:
    global _callback_ref_seq
    _callback_ref_seq += 1
    ref = format(_callback_ref_seq, "x")
    _callback_refs[ref] = {
        "chain": chain, "token": token_address, "symbol": symbol,
        "price_usd": price_usd, "price_native": price_native,
    }
    if len(_callback_refs) > 5000:  # bound memory; very old buttons just expire
        for old in list(_callback_refs)[:1000]:
            _callback_refs.pop(old, None)
    return ref


def get_callback_target(ref: str) -> Optional[dict]:
    return _callback_refs.get(ref)


# Update handlers run as their own tasks so a slow buy (up to 120s receipt wait)
# cannot stall polling and make the bot miss commands (issue 5.3).
_handler_tasks: set = set()


def _spawn_handler(coro):
    task = asyncio.create_task(coro)
    _handler_tasks.add(task)

    def _done(t):
        _handler_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error(f"Update handler failed: {exc}", exc_info=exc)

    task.add_done_callback(_done)
    return task

# Signal engine state (only populated when USE_SIGNALS=true).
SIGNAL_FILTERS = signals.Filters.from_env() if SIGNALS_AVAILABLE else None
PAIR_HISTORY = signals.PairHistory() if SIGNALS_AVAILABLE else None


def is_authorized(chat_id, user_id=None) -> bool:
    """Only the configured chat (and allowlisted users) may control the bot."""
    if str(chat_id).strip() != str(CHAT_ID).strip():
        return False
    if ALLOWED_USER_IDS and user_id is not None:
        try:
            if int(user_id) not in ALLOWED_USER_IDS:
                return False
        except (TypeError, ValueError):
            return False
    return True

# ═══════════════════════════════════════════════════════════════════════════════
# ABIs
# ═══════════════════════════════════════════════════════════════════════════════

V3_ROUTER_ABI = [
    {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct ISwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"},
]

QUOTER_V2_ABI = [
    {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}
]

ERC20_ABI = [
    {"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
    {"constant":False,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
]

V2_ROUTER_ABI = [
    {"inputs":[{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactETHForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForETH","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"},
]

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING & GLOBALS
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # Rotating so a long-running deployment cannot fill the disk (issue 4.14).
        logging.handlers.RotatingFileHandler(
            "pump_bot_v5.log", maxBytes=5 * 1024 * 1024, backupCount=3
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("pump_bot_v5")

bot = Bot(token=TELEGRAM_TOKEN)
api_semaphore = asyncio.Semaphore(CONCURRENT_API_LIMIT)
web3_semaphore = asyncio.Semaphore(3)  # Limit concurrent RPC calls to prevent 429s

# Per-chain transaction lock (issue 4.7). A Telegram buy and a monitor sell can
# otherwise fetch the same 'pending' nonce and one transaction replaces the other.
_tx_locks: Dict[str, asyncio.Lock] = {}

def tx_lock(chain: str) -> asyncio.Lock:
    lock = _tx_locks.get(chain)
    if lock is None:
        lock = asyncio.Lock()
        _tx_locks[chain] = lock
    return lock


async def tg_send(text: str, *, retries: int = 3, **kwargs):
    """bot.send_message with Telegram RetryAfter / transient-error backoff (5.4)."""
    kwargs.setdefault("chat_id", CHAT_ID)
    for attempt in range(retries + 1):
        try:
            return await bot.send_message(text=text, **kwargs)
        except RetryAfter as e:
            ra = getattr(e, "retry_after", 5)
            wait = int(ra.total_seconds()) if hasattr(ra, "total_seconds") else int(ra)
            wait = max(1, wait) + 1
            logger.warning(f"Telegram rate limited, sleeping {wait}s")
            if attempt == retries:
                raise
            await asyncio.sleep(wait)
        except TelegramError as e:
            logger.error(f"Telegram send failed: {e}")
            if attempt == retries:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    return None


def esc(value) -> str:
    """Escape untrusted text (token names/symbols) for ParseMode.HTML (issue 5.5)."""
    return html_escape(str(value), quote=False)

security_cache: Dict[str, Tuple[dict, float]] = {}
holder_cache: Dict[str, Tuple[Tuple[float, float, float], float]] = {}
coingecko_id_map: Dict[str, Dict[str, str]] = {}
coingecko_ticker_cache: Dict[str, Tuple[dict, float]] = {}
_native_price_cache: Dict[str, Tuple[float, float]] = {}

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
# FEATURE LOGGING (Phase 2) — every evaluated token, not just alerts
# ═══════════════════════════════════════════════════════════════════════════════
#
# One row per token evaluation (including rejects), with the raw features a
# linear/logistic regression can learn from. Enable with LOG_FEATURES=true.
# Rows for the same token accumulate over scans, which doubles as the price
# time-series used by label_outcomes.py to build forward-return labels.

FEATURE_FIELDS = [
    "ts_utc", "ts_epoch", "chain", "token_address", "pair_address", "symbol", "source",
    "age_minutes", "liquidity_usd", "market_cap_usd", "fdv_usd", "price_usd", "price_native",
    "vol_5m", "vol_15m", "vol_1h", "vol_6h", "vol_24h",
    "vol_liq_ratio", "vol_5m_1h", "vol_1h_6h", "vol_6h_24h",
    "buys_5m", "sells_5m", "buyers_5m", "sellers_5m", "buys_1h", "sells_1h",
    "buy_ratio_5m", "buy_ratio_1h",
    "chg_5m", "chg_15m", "chg_1h", "chg_6h", "chg_24h",
    "top10", "top50", "top100", "holder_count", "creator_pct", "holder_source",
    "buy_tax", "sell_tax",
    "is_honeypot", "is_open_source", "is_proxy", "is_mintable",
    "owner_change_balance", "transfer_pausable", "slippage_modifiable",
    "hidden_owner", "cannot_sell_all", "selfdestruct", "trading_cooldown",
    "is_blacklisted", "is_whitelisted", "lp_locked",
    "security_source", "security_known",
    "signal_bonus", "signal_penalty", "signal_notes",
    "base_score", "hand_score", "phase1_pass", "alert_threshold",
    "rejected", "reject_reasons", "passed_threshold", "alert_sent", "paper_mode",
    "early_runner",
]

# Columns stored as INTEGER (flags/counts). Everything else is REAL unless it is
# one of the few TEXT columns, so the schema stays readable.
_FEATURE_INT_FIELDS = {
    "buys_5m", "sells_5m", "buyers_5m", "sellers_5m", "buys_1h", "sells_1h",
    "holder_count", "is_honeypot", "is_open_source", "is_proxy", "is_mintable",
    "owner_change_balance", "transfer_pausable", "slippage_modifiable",
    "hidden_owner", "cannot_sell_all", "selfdestruct", "trading_cooldown",
    "is_blacklisted", "is_whitelisted", "lp_locked", "security_known",
    "rejected", "passed_threshold", "alert_sent", "paper_mode", "early_runner",
}
_FEATURE_TEXT_FIELDS = {
    "ts_utc", "chain", "token_address", "pair_address", "symbol", "source",
    "security_source", "signal_notes", "reject_reasons", "holder_source",
}


def _feature_col_type(field: str) -> str:
    if field in _FEATURE_TEXT_FIELDS:
        return "TEXT"
    if field in _FEATURE_INT_FIELDS:
        return "INTEGER"
    return "REAL"


def _feature_schema_sql() -> str:
    cols = [f"    {field} {_feature_col_type(field)}" for field in FEATURE_FIELDS]
    return (
        "CREATE TABLE IF NOT EXISTS features (\n"
        "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        + ",\n".join(cols)
        + "\n);"
    )


def ensure_feature_columns(conn: sqlite3.Connection):
    """Add any newly introduced feature columns to an existing DB.

    ``CREATE TABLE IF NOT EXISTS`` does not add columns, so a deployment that
    already has a ``features`` table needs this migration.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(features)")}
    for field in FEATURE_FIELDS:
        if field not in existing:
            conn.execute(f"ALTER TABLE features ADD COLUMN {field} {_feature_col_type(field)}")
    conn.commit()


def _empty_feature() -> dict:
    return {field: None for field in FEATURE_FIELDS}


class FeatureLogger:
    """Non-blocking feature writer.

    ``evaluate_token`` only ever calls :meth:`log_row`, which does a
    ``queue.put_nowait`` — the scanner never blocks on disk I/O. A daemon thread
    owns its own SQLite connection and drains the queue in small batches.
    """

    def __init__(self, db_path: str, enabled: bool, max_queue: int = 20000):
        self.db_path = db_path
        self.enabled = enabled
        self._q: "queue.Queue" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.logged = 0
        self.dropped = 0
        self.written = 0

    def start(self):
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="feature-writer", daemon=True)
        self._thread.start()
        logger.info(f"Feature logging ENABLED -> {self.db_path}")

    def log_row(self, row: dict):
        if not self.enabled:
            return
        self.logged += 1
        try:
            self._q.put_nowait(("insert", row))
        except queue.Full:
            self.dropped += 1

    def mark_alert_sent(self, chain: str, token_address: str):
        if not self.enabled:
            return
        try:
            self._q.put_nowait(("alert", {"chain": chain, "token": token_address}))
        except queue.Full:
            self.dropped += 1

    def _apply(self, conn: sqlite3.Connection, kind: str, payload: dict):
        if kind == "insert":
            placeholders = ",".join("?" * len(FEATURE_FIELDS))
            conn.execute(
                f"INSERT INTO features ({','.join(FEATURE_FIELDS)}) VALUES ({placeholders})",
                [payload.get(f) for f in FEATURE_FIELDS],
            )
        elif kind == "alert":
            conn.execute(
                "UPDATE features SET alert_sent=1 WHERE id=("
                "SELECT MAX(id) FROM features WHERE chain=? AND token_address=?)",
                (payload["chain"], payload["token"]),
            )

    def _run(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            while True:
                try:
                    item = self._q.get(timeout=1.0)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                if item[0] == "stop":
                    break
                self._apply(conn, *item)
                # Opportunistically drain a batch before paying the commit cost.
                for _ in range(199):
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt[0] == "stop":
                        item = nxt
                        break
                    self._apply(conn, *nxt)
                conn.commit()
                self.written += 1
        except Exception as e:
            logger.error(f"Feature writer stopped: {e}")
        finally:
            if conn is not None:
                try:
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

    def stop(self, timeout: float = 5.0):
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._q.put_nowait(("stop", {}))
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "queued": self._q.qsize(),
            "logged": self.logged,
            "written_batches": self.written,
            "dropped": self.dropped,
        }


FEATURE_LOGGER = FeatureLogger(FEATURE_DB_PATH or DB_PATH, LOG_FEATURES)

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # WAL lets the feature-writer thread write while the bot thread reads.
    conn.execute("PRAGMA journal_mode=WAL")
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
    conn.executescript(_feature_schema_sql())
    ensure_feature_columns(conn)  # migrate older feature tables
    conn.executescript(
        "CREATE INDEX IF NOT EXISTS idx_features_token ON features(chain, token_address);"
        "CREATE INDEX IF NOT EXISTS idx_features_ts ON features(ts_epoch);"
    )
    defaults = {
        "slippage": str(DEFAULT_SLIPPAGE),
        "trailing_stop": str(DEFAULT_TRAILING_STOP),
        "take_profit_levels": json.dumps(DEFAULT_TP_LEVELS),
        "risk_usd": str(DEFAULT_RISK_USD),
    }
    # Per-chain presets (issue 4.4). Previously keyed by native symbol, so
    # base/robinhood/ethereum all shared "buy_amounts_eth".
    for chain, amounts in DEFAULT_BUY_AMOUNTS.items():
        defaults[_buy_amounts_key(chain)] = json.dumps(amounts)
    # One-time migration: keep any customised legacy (symbol-keyed) presets, but
    # only when the new per-chain key does not exist yet.
    legacy_keys = {
        "ethereum": "buy_amounts_eth",
        "bsc": "buy_amounts_bnb",
        "base": "buy_amounts_base",
        "robinhood": "buy_amounts_eth",
    }
    for chain, old_key in legacy_keys.items():
        new_key = _buy_amounts_key(chain)
        if conn.execute("SELECT 1 FROM settings WHERE key=?", (new_key,)).fetchone():
            continue
        old = conn.execute("SELECT value FROM settings WHERE key=?", (old_key,)).fetchone()
        if old:
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (new_key, old[0]))
            logger.info(f"Migrated buy amounts {old_key} -> {new_key}")
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    return conn


def _buy_amounts_key(chain: str) -> str:
    """Settings key for a chain's preset buy amounts (issue 4.4)."""
    return f"buy_amounts_{chain}"

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
    if PAIR_HISTORY is not None:
        PAIR_HISTORY.prune(now)

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
    """GET + JSON with retries on 429, 5xx and network errors (issue 4.10).

    Retries use jittered backoff and honour Retry-After when present so a
    provider hiccup does not silently drop a candidate.
    """
    if use_coingecko_limiter:
        await _coingecko_rate_limit()
    async with api_semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json(content_type=None)
                        except Exception:
                            return None
                    if resp.status == 429 or 500 <= resp.status < 600:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after and retry_after.replace(".", "", 1).isdigit():
                            delay = float(retry_after)
                        else:
                            delay = float(2 ** attempt)
                        await asyncio.sleep(min(delay, 30.0) + random.uniform(0, 0.5))
                        continue
                    if VERBOSE_LOGGING:
                        logger.debug(f"HTTP {resp.status} for {url[:80]}")
                    return None
            except Exception as e:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.5 * attempt + random.uniform(0, 0.5))
                elif VERBOSE_LOGGING:
                    logger.debug(f"Fetch failed for {url[:80]}: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# PAIR DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

_gt_client = None

# ── Shared GeckoTerminal budget ──────────────────────────────────────────────
# GT's free tier allows roughly 30 calls/minute and answers a burst with 429.
# Discovery (discovery.py) throttles itself, but the token-info calls for holder
# concentration go through fetch_json directly, so without a *shared* limiter the
# two paths would each believe they owned the whole budget and collectively blow
# it. One process-wide limiter therefore covers both.
GT_MIN_INTERVAL = float(os.getenv("GT_MIN_INTERVAL_S", "2.1"))  # ~28 calls/min
_gt_throttle_lock: Optional[asyncio.Lock] = None
_gt_throttle_loop = None
_gt_last_call = 0.0


def _gt_lock() -> asyncio.Lock:
    """A limiter lock bound to the running loop.

    Recreated when the loop changes so this stays safe under the per-test
    ``asyncio.run()`` pattern, which spins up a fresh loop each time.
    """
    global _gt_throttle_lock, _gt_throttle_loop
    loop = asyncio.get_running_loop()
    if _gt_throttle_lock is None or _gt_throttle_loop is not loop:
        _gt_throttle_lock = asyncio.Lock()
        _gt_throttle_loop = loop
    return _gt_throttle_lock


async def gt_fetch_json(session, url, **kwargs):
    """``fetch_json`` plus the shared GeckoTerminal rate limit."""
    global _gt_last_call
    if GT_MIN_INTERVAL > 0:
        async with _gt_lock():
            wait = GT_MIN_INTERVAL - (time.time() - _gt_last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            _gt_last_call = time.time()
    return await fetch_json(session, url, **kwargs)


async def get_geckoterminal_pairs(session, network):
    """Candidate discovery via GeckoTerminal (see discovery.py).

    Replaces the DexScreener `latest/dex/pairs/{chain}` call, which 404s and
    silently left the scanner with only paid boost/profile tokens.
    """
    global _gt_client
    sources = tuple(
        s.strip() for s in os.getenv("GT_SOURCES", "new_pools,trending").split(",") if s.strip()
    )
    fetcher = lambda url: gt_fetch_json(session, url)  # noqa: E731
    if _gt_client is None:
        # min_interval_s=0: gt_fetch_json already applies the shared budget, and
        # stacking a second 2.1s wait would halve throughput for no benefit.
        _gt_client = discovery.GeckoTerminal(fetcher, min_interval_s=0.0)
    else:
        _gt_client.fetch = fetcher
    pairs = await _gt_client.candidates(network, kinds=sources)
    pairs.sort(key=lambda x: float((x.get("volume") or {}).get("m5", 0) or 0), reverse=True)
    logger.info(f"{network}: {len(pairs)} GeckoTerminal candidates ({','.join(sources)})")
    return pairs[:300]


async def get_all_pairs(session, network):
    if USE_GECKOTERMINAL:
        return await get_geckoterminal_pairs(session, network)

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

def _security_placeholder(source: str) -> dict:
    """Neutral security record so all providers return the same keys."""
    return {
        "is_honeypot": False, "buy_tax": 0.0, "sell_tax": 0.0,
        "is_whitelisted": False, "is_blacklisted": False,
        "is_open_source": None, "is_proxy": False,
        "can_take_back_ownership": False, "owner_change_balance": False,
        "is_mintable": False, "slippage_modifiable": False,
        "transfer_pausable": False, "lp_locked": None,
        "hidden_owner": False, "cannot_sell_all": False,
        "selfdestruct": False, "trading_cooldown": False,
        "holder_count": 0, "creator_percent": 0.0,
        "source": source,
    }


async def get_honeypot_is_security(session, chain, token):
    """Second-opinion honeypot/tax check via honeypot.is v2.

    Only trusted when honeypot.is actually simulated the token
    (``simulationSuccess`` true and a ``honeypotResult`` present). Without that
    requirement the fallback returned a permissive record for brand-new tokens
    the service could not analyse, which let unvetted Base tokens through.
    """
    chain_id = HONEYPOT_IS_CHAIN_ID.get(chain)
    if not chain_id:
        return None
    url = f"https://api.honeypot.is/v2/IsHoneypot?address={token}&chainID={chain_id}"
    data = await fetch_json(session, url)
    if not isinstance(data, dict):
        return None
    hp = data.get("honeypotResult")
    if not data.get("simulationSuccess") or not isinstance(hp, dict) or "isHoneypot" not in hp:
        logger.debug(f"honeypot.is could not simulate {chain} {token[:10]}... — treated as unknown")
        return None
    sim = data.get("simulationResult") or {}
    code = data.get("contractCode") or {}
    sec = _security_placeholder("honeypot.is")
    sec.update({
        "is_honeypot": bool(hp.get("isHoneypot")),
        "buy_tax": float(sim.get("buyTax") or 0),
        "sell_tax": float(sim.get("sellTax") or 0),
        "is_open_source": bool(code.get("openSource")) if code else None,
        "is_proxy": bool(code.get("isProxy")),
    })
    return sec


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
        # A failed lookup (cached None) is retried far sooner than a good one.
        ttl = SECURITY_TTL if cached else SECURITY_FAIL_TTL
        if now - ts < ttl:
            return cached
    url = f"https://api.gopluslabs.io/api/v1/token_security/{goplus_chain}?contract_addresses={token.lower()}"
    data = await fetch_json(session, url)
    result = None
    if data and "result" in data:
        result = data["result"].get(token.lower())
    if not result:
        # GoPlus has no record for this token. Default behaviour (matching the
        # original bot) is to DROP it: unknown != safe. Set
        # ALLOW_SECURITY_FALLBACK=true to accept a *successfully simulated*
        # honeypot.is record instead.
        if ALLOW_SECURITY_FALLBACK:
            fallback = await get_honeypot_is_security(session, chain, token)
            if fallback:
                security_cache[cache_key] = (fallback, now)
                logger.info(f"GoPlus miss for {token[:10]}... — used honeypot.is fallback")
                return fallback
        security_cache[cache_key] = (None, now)
        if VERBOSE_LOGGING:
            logger.info(f"Security unknown for {chain} {token[:10]}... (short-cached, dropped)")
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
        # Extra GoPlus flags that signals.py uses to catch rugs the old subset missed.
        "hidden_owner": result.get("hidden_owner") == "1",
        "cannot_sell_all": result.get("cannot_sell_all") == "1",
        "selfdestruct": result.get("selfdestruct") == "1",
        "trading_cooldown": result.get("trading_cooldown") == "1",
        "holder_count": int(float(result.get("holder_count", "0") or 0)),
        "creator_percent": float(result.get("creator_percent", "0") or 0),
        "source": "goplus",
    }
    security_cache[cache_key] = (security, now)
    return security

async def get_etherscan_security(session, chain, token):
    """Contract verification (+ supply sanity) via Etherscan v2.

    Etherscan's EAAS covers Robinhood Chain (chainid 4663), which is what makes
    this work where Blockscout does not: ``robinhoodchain.blockscout.com`` now
    answers every request with a Cloudflare managed challenge (HTTP 403), so the
    old code marked *every* Robinhood token unverified and applied the
    ``PENALTY_UNVERIFIED_CONTRACT`` penalty to all of them. Tokens like SCHIFFY
    are in fact verified.

    Returns ``None`` when no API key is configured or the call fails, so the
    caller can tell "unverified" apart from "could not check".
    """
    chain_id = ETHERSCAN_CHAIN_ID.get(chain)
    if not chain_id or not ETHERSCAN_API_KEY:
        return None
    params = (
        f"chainid={chain_id}&module=contract&action=getsourcecode"
        f"&address={token}&apikey={ETHERSCAN_API_KEY}"
    )
    data = await fetch_json(session, f"{ETHERSCAN_API}?{params}")
    if not isinstance(data, dict) or data.get("status") != "1":
        return None
    rows = data.get("result")
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0] or {}
    source_code = (row.get("SourceCode") or "").strip()
    # Etherscan returns a result row with an empty SourceCode for an unverified
    # contract, and `status: 1` either way — so verification is the presence of
    # source, not the status field.
    verified = bool(source_code)
    return {
        "is_verified": verified,
        "is_open_source": verified,
        "is_proxy": (row.get("Proxy") or "0") == "1",
        "contract_name": row.get("ContractName") or "",
        "verification_known": True,
        "source": "etherscan",
    }


async def get_robinhood_security(session, token):
    """Security record for a Robinhood Chain token.

    Etherscan is authoritative and works; Blockscout is kept only as a fallback
    for deployments that have no Etherscan key (it is usually Cloudflare-blocked,
    in which case ``verification_known`` stays False and no penalty is applied —
    we must not punish a token for our own provider outage).
    """
    cache_key = f"robinhood_sec:{token.lower()}"
    now = time.time()
    if cache_key in security_cache:
        cached, ts = security_cache[cache_key]
        if now - ts < SECURITY_TTL:
            return cached

    security = {
        "is_honeypot": False, "buy_tax": 0, "sell_tax": 0,
        "is_whitelisted": False, "is_blacklisted": False,
        "is_open_source": False, "is_proxy": False,
        "can_take_back_ownership": False, "owner_change_balance": False,
        "is_mintable": False, "slippage_modifiable": False,
        "transfer_pausable": False, "lp_locked": False,
        "is_verified": False, "verification_known": False,
        "source": "none",
    }

    eth = await get_etherscan_security(session, "robinhood", token)
    if eth:
        security.update(eth)
        security_cache[cache_key] = (security, now)
        return security

    # ── Fallback: Blockscout (frequently Cloudflare-blocked) ────────────────
    base = BLOCKSCOUT_URLS["robinhood"]
    url = f"{base}/smart-contracts/{token}"
    data = await fetch_json(session, url)
    if data:
        security["is_verified"] = bool(data.get("is_verified"))
        security["is_open_source"] = bool(data.get("is_verified"))
        security["is_proxy"] = data.get("proxy_type") is not None
        security["verification_known"] = True
        security["source"] = "blockscout"
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

class HolderData(NamedTuple):
    """Holder concentration as *measured*, with provenance.

    ``source`` is empty when nothing could be measured — that is deliberately
    distinct from measuring 0%, because the alert gate scales down for data it
    could not obtain, and must not treat "provider down" as "perfectly
    distributed" or vice-versa.

    ``top100`` is ``None`` when the provider publishes no 51-100 band. Guessing
    it would silently inflate the score.
    """
    top10: float = 0.0
    top50: float = 0.0
    top100: Optional[float] = None
    source: str = ""
    holder_count: int = 0

    @property
    def measured(self) -> bool:
        return bool(self.source)


def _gt_holder_data(info: dict) -> HolderData:
    """Parse a GeckoTerminal token-info payload into :class:`HolderData`.

    GT publishes cumulative-per-band percentages: ``top_10``, ``11_30`` and
    ``31_50``. So top-10 is direct, and top-50 is their sum. There is no
    ``51_100`` band, hence ``top100=None``.
    """
    attrs = ((info or {}).get("data") or {}).get("attributes") or {}
    holders = attrs.get("holders") or {}
    dist = holders.get("distribution_percentage") or {}
    if not dist:
        return HolderData()
    try:
        top10 = float(dist.get("top_10") or 0)
        top50 = top10 + float(dist.get("11_30") or 0) + float(dist.get("31_50") or 0)
    except (TypeError, ValueError):
        return HolderData()
    try:
        count = int(holders.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    return HolderData(
        top10=top10, top50=min(top50, 100.0), top100=None,
        source="geckoterminal", holder_count=count,
    )


async def get_geckoterminal_holders(session, chain, token) -> HolderData:
    """Holder distribution from GeckoTerminal — free, keyless, and reachable.

    This is the replacement for Blockscout (Cloudflare 403 on
    ``robinhoodchain.blockscout.com``) and for Moralis (whose free tier is
    suspended). Works on all four chains including Robinhood.
    """
    net = GT_CHAIN_SLUG.get(chain)
    if not net:
        return HolderData()
    cache_key = f"gt_holders:{chain}:{token.lower()}"
    now = time.time()
    if cache_key in holder_cache:
        data, ts = holder_cache[cache_key]
        if now - ts < HOLDER_TTL:
            return data
    url = f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{token}/info"
    data = await gt_fetch_json(session, url, headers={"Accept": "application/json"})
    result = _gt_holder_data(data) if data else HolderData()
    # A miss is cached briefly so a provider outage cannot hide every token.
    holder_cache[cache_key] = (result, now)
    return result


async def get_holder_concentration(session, chain, token) -> HolderData:
    """Best available holder concentration for a chain.

    Preference order per chain:

    * Robinhood — GeckoTerminal. Moralis has no Robinhood support and the
      Blockscout instance is Cloudflare-blocked.
    * Others — Moralis first (it is the only source that also yields an exact
      top-100 figure), then GeckoTerminal.

    A blank ``source`` means nothing could be measured; callers must scale the
    alert gate accordingly instead of scoring 0% concentration.
    """
    gt = await get_geckoterminal_holders(session, chain, token)
    if chain == "robinhood":
        # Blockscout is Cloudflare-blocked (HTTP 403) so it is no longer tried
        # by default. Keep it behind an opt-in flag for anyone self-hosting an
        # un-proxied instance.
        if not gt.measured and os.getenv("TRY_BLOCKSCOUT_HOLDERS", "false").lower() == "true":
            legacy = await get_robinhood_holders(session, token)
            if legacy[0] or legacy[1] or legacy[2]:
                return HolderData(top10=legacy[0], top50=legacy[1], top100=legacy[2],
                                  source="blockscout")
        return gt

    moralis = await get_moralis_holders(session, chain, token)
    if moralis.measured:
        # Moralis gives top-100, which GT cannot; prefer it when it answers.
        return moralis
    return gt


async def get_moralis_holders(session, chain, token) -> HolderData:
    if not MORALIS_API_KEY:
        return HolderData()
    moralis_chain = CHAIN_TO_MORALIS.get(chain)
    if not moralis_chain:
        return HolderData()
    cache_key = f"moralis:{moralis_chain}:{token.lower()}"
    now = time.time()
    if cache_key in holder_cache:
        data, ts = holder_cache[cache_key]
        if now - ts < HOLDER_TTL:
            return data
    url = f"https://deep-index.moralis.io/api/v2.2/erc20/{token}/owners?chain={moralis_chain}&order=DESC&limit=100"
    headers = {"X-API-Key": MORALIS_API_KEY}
    data = await fetch_json(session, url, headers=headers)
    if not data or "result" not in data:
        # Distinguish "provider refused/unavailable" from "0% concentration".
        holder_cache[cache_key] = (HolderData(), now)
        return HolderData()
    holders = data.get("result", [])
    total_supply = float(data.get("total_supply") or 0)
    if total_supply == 0 or not holders:
        holder_cache[cache_key] = (HolderData(), now)
        return HolderData()
    balances = [float(h.get("balance", 0)) for h in holders]
    top10 = sum(balances[:10])
    top50 = sum(balances[:50]) if len(balances) >= 50 else sum(balances)
    top100 = sum(balances[:100]) if len(balances) >= 100 else sum(balances)
    result = HolderData(
        top10=(top10 / total_supply) * 100,
        top50=(top50 / total_supply) * 100,
        top100=(top100 / total_supply) * 100,
        source="moralis", holder_count=len(holders),
    )
    holder_cache[cache_key] = (result, now)
    return result

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
    """Holder-concentration points.

    ``top100`` may be ``None`` when the provider publishes no 51-100 band (as
    GeckoTerminal does not). That block is then skipped rather than scored as if
    concentration were zero — ``_max_possible_score`` compensates by lowering the
    alert gate for the points that were genuinely unmeasurable.
    """
    pts = 0
    age_discount = 0.3 if age_minutes and age_minutes < 120 else 1.0
    if top10 >= 80: pts += HOLDER_TOP10_PTS * age_discount
    elif top10 >= 60: pts += HOLDER_TOP10_PTS * 0.75 * age_discount
    elif top10 >= 40: pts += HOLDER_TOP10_PTS * 0.5 * age_discount
    elif top10 >= 25: pts += HOLDER_TOP10_PTS * 0.25 * age_discount
    if top50 >= 90: pts += HOLDER_TOP50_PTS
    elif top50 >= 75: pts += HOLDER_TOP50_PTS * 0.75
    elif top50 >= 60: pts += HOLDER_TOP50_PTS * 0.5
    if top100 is not None:
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
# ALERT GATE — chain- and data-aware
# ═══════════════════════════════════════════════════════════════════════════════

def max_possible_score(chain, age_minutes, *, has_top100=True, has_cex=True):
    """Highest score a token on this chain, at this age, could possibly reach.

    The 0-100 scale is only meaningful if all 100 points are *earnable*. They are
    not, and never were:

    * **Holder concentration (20 pts)** came back empty on every chain, because
      Moralis's free tier is suspended and Robinhood's Blockscout instance is
      Cloudflare-blocked. Now supplied by GeckoTerminal, but GT publishes no
      51-100 band, so 4 of those 20 points stay unmeasurable (``has_top100``).
    * **CEX listings (5 pts)** are unreachable for Robinhood: no Robinhood token
      is listed on CoinGecko, so ``get_cex_listings`` can never return anything.
    * **Age gates** cap the long-window branches: a 1-hour-old pool cannot score
      the 1h/6h or 6h/24h tiers at all.

    This mirrors the real scorers by calling them with ideal inputs, so it cannot
    drift out of sync with them. It is used to scale the alert threshold down to
    what is actually reachable, instead of silently requiring 65 out of ~43.
    """
    v = 1.0  # any positive volume lets each ratio branch be driven to its top tier
    top = 0.0
    top += score_volume_liquidity(v, v)                 # vol/liq >= 1.0
    top += score_5m_1h(v, v, age_minutes)               # 5m/1h normalised >= 8
    top += score_1h_6h(v, v, age_minutes)               # 1h/6h normalised >= 6
    top += score_6h_24h(v, v, age_minutes)              # 6h/24h normalised >= 4
    top += BUY_PRESSURE_5M_PTS                          # buy ratio >= 0.85
    if age_minutes is not None and age_minutes > 60:
        top += BUY_PRESSURE_1H_PTS                      # buy ratio 1h >= 0.80
    top += score_price(1000.0, 1000.0, 1000.0, age_minutes)
    top += SECURITY_PTS
    top += score_holder(100.0, 100.0, 100.0 if has_top100 else None, age_minutes)
    if has_cex:
        top += score_cex(CEX_LISTING_PTS, True, 1)
    return top


def has_cex_data(chain) -> bool:
    """Whether CEX-listing scoring can ever fire for this chain."""
    platform = CHAIN_TO_COINGECKO_PLATFORM.get(chain)
    return bool(platform and platform != "robinhood")


def effective_threshold(chain, age_minutes, *, has_top100=True, has_cex=None):
    """The alert threshold, scaled to the points actually reachable here.

    ``ROBINHOOD_MIN_SCORE`` / ``BASE_MIN_SCORE`` … override the base for one
    chain using the same convention as the ``*_MIN_LIQUIDITY_USD`` floors. Set
    ``SCORE_NORMALIZE=false`` to disable scaling entirely and gate on the raw
    ``MIN_SCORE`` for every chain.
    """
    base = _chain_floor(chain, "MIN_SCORE", float(ALERT_THRESHOLD))
    if os.getenv("SCORE_NORMALIZE", "true").lower() != "true":
        return base
    if has_cex is None:
        has_cex = has_cex_data(chain)
    ceiling = max_possible_score(chain, age_minutes, has_top100=has_top100, has_cex=has_cex)
    if ceiling <= 0:
        return base
    scaled = base * (ceiling / 100.0)
    # Never let scaling push the bar below the floor: a token still has to look
    # genuinely strong, not merely "best of a bad batch". The floor is itself
    # capped by ``base`` so an explicit MIN_SCORE below the floor (e.g. 0, to
    # alert on everything while tuning) still means what it says.
    floor = min(base, float(os.getenv("MIN_EFFECTIVE_SCORE", "35")))
    return max(floor, min(base, scaled))


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_security_to_feature(feat: dict, security: Optional[dict]):
    """Copy security flags into the feature row (Phase 2 logging)."""
    if not security:
        return
    feat["security_source"] = security.get("source")
    # ``security_known`` means "we actually got an answer", not merely "a dict
    # exists" — the Robinhood path always returns a dict, even when both
    # providers failed, and logging that as known would poison the training set.
    known = security.get("verification_known")
    feat["security_known"] = 1 if (known is None or known) else 0
    for key in (
        "is_honeypot", "is_open_source", "is_proxy", "is_mintable",
        "owner_change_balance", "transfer_pausable", "slippage_modifiable",
        "hidden_owner", "cannot_sell_all", "selfdestruct", "trading_cooldown",
        "is_blacklisted", "is_whitelisted", "lp_locked",
    ):
        value = security.get(key)
        feat[key] = None if value is None else int(bool(value))
    feat["buy_tax"] = float(security.get("buy_tax") or 0)
    feat["sell_tax"] = float(security.get("sell_tax") or 0)
    # GeckoTerminal does not report a creator share, so only overwrite the
    # provider's holder count when it actually supplied one.
    if security.get("holder_count"):
        feat["holder_count"] = int(security.get("holder_count") or 0)
    # GoPlus reports creator_percent as a fraction; store it as a percentage.
    feat["creator_pct"] = float(security.get("creator_percent") or 0) * 100


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

    # ── Phase 2: feature snapshot, persisted for every evaluation (incl. rejects) ──
    feat = _empty_feature()
    feat.update({
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ts_epoch": time.time(),
        "chain": chain, "token_address": token, "pair_address": pair_id,
        "symbol": symbol, "source": str(pair.get("source") or ""),
        "price_usd": float(pair.get("priceUsd") or 0),
        "price_native": float(pair.get("priceNative") or 0),
        "fdv_usd": float(pair.get("fdv") or 0),
        "paper_mode": 1 if PAPER_TRADING else 0,
    })

    def finish(reason: Optional[str] = None, result: Optional[dict] = None):
        """Persist the feature row (non-blocking) and return the eval result."""
        feat["phase1_pass"] = feat.get("phase1_pass") or 0
        feat["rejected"] = 1 if reason else 0
        feat["reject_reasons"] = reason or ""
        feat["passed_threshold"] = 1 if result is not None else 0
        feat["alert_sent"] = 0  # the main loop marks this after a real send
        FEATURE_LOGGER.log_row(feat)
        return result

    def reject(reason: str):
        return finish(reason)

    # Per-chain floors so one noisy chain (e.g. Base) can be tightened without
    # changing the others: BASE_MIN_LIQUIDITY_USD, BASE_MIN_VOL_5M_USD, ...
    default_liq = ROBINHOOD_MIN_LIQUIDITY_USD if chain == "robinhood" else MIN_LIQUIDITY_USD
    min_liq = _chain_floor(chain, "MIN_LIQUIDITY_USD", default_liq)
    min_vol_5m = _chain_floor(chain, "MIN_VOL_5M_USD", MIN_VOL_5M_USD)
    min_mcap = _chain_floor(chain, "MIN_MARKET_CAP_USD", MIN_MARKET_CAP_USD)
    liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
    feat["liquidity_usd"] = liquidity
    if liquidity < min_liq: return reject("liquidity")
    price = float(pair.get("priceUsd") or 0)
    if price < MIN_PRICE: return reject("price_too_low")
    market_cap = float(pair.get("marketCap") or 0)
    feat["market_cap_usd"] = market_cap
    if market_cap < min_mcap: return reject("market_cap")
    vol_5m = float(pair.get("volume", {}).get("m5") or 0)
    if vol_5m < min_vol_5m: return reject("vol_5m")

    vol_15m = float(pair.get("volume", {}).get("m15") or 0)
    vol_1h = float(pair.get("volume", {}).get("h1") or 0)
    vol_24h = float(pair.get("volume", {}).get("h24") or 0)
    vol_6h = float(pair.get("volume", {}).get("h6") or 0)
    txns_5m = pair.get("txns", {}).get("m5", {}) or {}
    buys_5m = int(txns_5m.get("buys", 0))
    sells_5m = int(txns_5m.get("sells", 0))
    buyers_5m = int(txns_5m.get("buyers", 0))
    sellers_5m = int(txns_5m.get("sellers", 0))
    txns_1h = pair.get("txns", {}).get("h1", {}) or {}
    buys_1h = int(txns_1h.get("buys", 0))
    sells_1h = int(txns_1h.get("sells", 0))
    chg_5m = float(pair.get("priceChange", {}).get("m5") or 0)
    chg_15m = float(pair.get("priceChange", {}).get("m15") or 0)
    chg_1h = float(pair.get("priceChange", {}).get("h1") or 0)
    chg_6h = float(pair.get("priceChange", {}).get("h6") or 0)
    chg_24h = float(pair.get("priceChange", {}).get("h24") or 0)
    pair_created = pair.get("pairCreatedAt")
    age_minutes = (time.time() - pair_created / 1000) / 60 if pair_created else None

    feat.update({
        "age_minutes": age_minutes,
        "vol_5m": vol_5m, "vol_15m": vol_15m, "vol_1h": vol_1h,
        "vol_6h": vol_6h, "vol_24h": vol_24h,
        "vol_liq_ratio": (vol_5m / liquidity) if liquidity else 0.0,
        "vol_5m_1h": (vol_5m / vol_1h) if vol_1h else 0.0,
        "vol_1h_6h": (vol_1h / vol_6h) if vol_6h else 0.0,
        "vol_6h_24h": (vol_6h / vol_24h) if vol_24h else 0.0,
        "buys_5m": buys_5m, "sells_5m": sells_5m,
        "buyers_5m": buyers_5m, "sellers_5m": sellers_5m,
        "buys_1h": buys_1h, "sells_1h": sells_1h,
        "buy_ratio_5m": (buys_5m / (buys_5m + sells_5m)) if (buys_5m + sells_5m) else 0.0,
        "buy_ratio_1h": (buys_1h / (buys_1h + sells_1h)) if (buys_1h + sells_1h) else 0.0,
        "chg_5m": chg_5m, "chg_15m": chg_15m, "chg_1h": chg_1h,
        "chg_6h": chg_6h, "chg_24h": chg_24h,
    })

    if chain == "robinhood" and age_minutes is not None and age_minutes < ROBINHOOD_MIN_PAIR_AGE_MIN:
        return reject("robinhood_too_new")

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
    _apply_security_to_feature(feat, security)
    if security:
        if security.get("is_honeypot"): return reject("honeypot")
        if security.get("buy_tax", 0) > MAX_ALLOWED_TAX or security.get("sell_tax", 0) > MAX_ALLOWED_TAX: return reject("tax")
        if chain != "robinhood":
            if any([
                security.get("is_whitelisted"), security.get("is_blacklisted"),
                security.get("is_proxy"), security.get("can_take_back_ownership"),
                security.get("owner_change_balance"), security.get("is_mintable"),
                security.get("slippage_modifiable"), security.get("transfer_pausable"),
            ]): return reject("risky_contract_flag")
        else:
            # Only penalise when we actually obtained a verification answer.
            # ``verification_known`` is False when Etherscan has no key and
            # Blockscout is Cloudflare-blocked — an outage on our side must not
            # look like an unverified token.
            if security.get("verification_known") and not security.get("is_verified", False):
                penalties += PENALTY_UNVERIFIED_CONTRACT
        score += SECURITY_PTS
    else:
        return reject("security_unknown")

    # ── Optional signal engine (signals.py): reject rugs before enrichment ──
    verdict = None
    if USE_SIGNALS:
        verdict = signals.evaluate(pair, security=security, filters=SIGNAL_FILTERS)
        PAIR_HISTORY.observe(pair, security=security, score=score - penalties)
        feat["signal_bonus"] = verdict.bonus
        feat["signal_penalty"] = verdict.penalty
        feat["signal_notes"] = ", ".join(verdict.notes)[:500]
        if verdict.rejected:
            if VERBOSE_LOGGING:
                logger.info(f"Signal reject {symbol}@{chain}: {','.join(verdict.reject_reasons)}")
            return reject("signal:" + ",".join(verdict.reject_reasons))

    base_score = score - penalties
    feat["base_score"] = base_score
    if base_score < PHASE1_MIN_SCORE:
        if VERBOSE_LOGGING:
            logger.info(f"Phase gate skip {symbol}@{chain}: base_score={base_score:.1f}")
        return reject("phase1_gate")
    feat["phase1_pass"] = 1

    holders = await get_holder_concentration(session, chain, token)
    top10, top50, top100 = holders.top10, holders.top50, holders.top100
    holder_pts = score_holder(top10, top50, top100, age_minutes)
    score += holder_pts
    feat["top10"], feat["top50"] = top10, top50
    feat["top100"] = top100
    feat["holder_source"] = holders.source
    feat["holder_count"] = holders.holder_count

    cex_count, has_perps, tier1 = await get_cex_listings(session, chain, token)
    cex_pts = score_cex(cex_count, has_perps, tier1)
    score += cex_pts

    # The hand-tuned score is the alert gate, exactly as in the original bot.
    # The signal engine is noise-reducing by default: its hard rejects and
    # penalties always apply, but its bonus is weighted (default 0.0) so it can
    # never promote a sub-threshold token into an alert.
    legacy_total = max(0.0, score - penalties)
    signal_bonus = verdict.bonus if verdict else 0.0
    signal_penalty = verdict.penalty if verdict else 0.0
    total_score = max(0.0, legacy_total - signal_penalty + signal_bonus * SIGNAL_BONUS_WEIGHT)
    feat["hand_score"] = legacy_total

    # ── Chain- and data-aware gate ───────────────────────────────────────────
    # 25 of the 100 points are not earnable on Robinhood (20 holder, 5 CEX) and
    # the age gates cap the long-window branches for young pools. Requiring a raw
    # 65 of a reachable ~43 is what produced total silence. Scale the bar to what
    # this chain/age can actually reach.
    threshold = effective_threshold(
        chain, age_minutes,
        has_top100=top100 is not None,
        has_cex=has_cex_data(chain),
    )
    feat["alert_threshold"] = threshold

    # Early-runner fast lane: a pool younger than its long volume windows can
    # never reach the threshold, so allow an AND-gated exception. This is what
    # makes USE_GECKOTERMINAL=new_pools useful rather than just noisy.
    early_ok = False
    if EARLY_RUNNER_MODE and total_score < threshold:
        early_reasons = signals.early_runner_reasons(pair, security=security, filters=SIGNAL_FILTERS)
        if not early_reasons:
            early_ok = True
            if VERBOSE_LOGGING:
                logger.info(f"Early-runner lane {symbol}@{chain} age={age_minutes:.0f}m score={total_score:.0f}")
    feat["early_runner"] = 1 if early_ok else 0

    if VERBOSE_LOGGING:
        t100 = f"{top100:.1f}%" if top100 is not None else "n/a"
        logger.info(
            f"{symbol}@{chain} score={total_score:.0f} (hand={legacy_total:.0f}/{threshold:.1f}) | "
            f"vol={vol_5m/liquidity:.2f}xliq 5m/1h={vol_5m/vol_1h if vol_1h>0 else 0:.2f} "
            f"1h/6h={vol_1h/vol_6h if vol_6h>0 else 0:.2f} 6h/24h={vol_6h/vol_24h if vol_24h>0 else 0:.2f} | "
            f"buy5m={buys_5m}/{sells_5m} buy1h={buys_1h}/{sells_1h} | "
            f"age={age_minutes:.0f}m | holders top10={top10:.1f}% top50={top50:.1f}% top100={t100} "
            f"({holders.source or 'unavailable'}) | "
            f"cex={cex_count} perps={has_perps} | base={base_score:.1f} penalties={penalties} "
            f"signal=+{signal_bonus:.0f}/-{signal_penalty:.0f}"
            + ("  [EARLY]" if early_ok else "")
        )

    # Original gate: the token must clear the (scaled) threshold on the
    # hand-tuned score, unless the strict early-runner lane vouched for it.
    if legacy_total < threshold and not early_ok:
        return reject("below_threshold")
    # Signals may still veto a token the hand-tuned score would have alerted on.
    if total_score < threshold and not early_ok:
        return reject("signal_penalised")

    return finish(None, {
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
        "signal_bonus": verdict.bonus if verdict else 0.0,
        "signal_penalty": verdict.penalty if verdict else 0.0,
        "signal_notes": verdict.notes if verdict else [],
    })

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

async def get_token_decimals(chain: str, token_address: str) -> int:
    """Only fetch decimals (issue 4.9: execute_buy did a needless balanceOf too)."""
    w3 = w3_instances.get(chain)
    if not w3:
        return 18
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        return int(await asyncio.to_thread(token.functions.decimals().call))
    except Exception as e:
        logger.warning(f"Token decimals check failed: {e}")
        return 18


async def build_gas_fields(w3, chain: str, multiplier: float = 1.2) -> dict:
    """EIP-1559 fee fields when the chain supports them, legacy otherwise (4.8).

    Also enforces ``MAX_GAS_PRICE_GWEI`` so a base-fee spike cannot make the bot
    overpay enormously; set it to 0 to disable the ceiling.
    """
    ceiling = w3.to_wei(MAX_GAS_PRICE_GWEI, 'gwei') if MAX_GAS_PRICE_GWEI > 0 else 0
    base_fee = None
    try:
        latest = await asyncio.to_thread(w3.eth.get_block, 'latest')
        base_fee = latest.get('baseFeePerGas') if hasattr(latest, 'get') else None
    except Exception:
        base_fee = None
    if base_fee:
        try:
            priority = int(await asyncio.to_thread(lambda: w3.eth.max_priority_fee))
        except Exception:
            priority = w3.to_wei(1.5, 'gwei')
        max_fee = int(base_fee * multiplier) + priority
        if ceiling and max_fee > ceiling:
            max_fee = ceiling
        return {'maxFeePerGas': int(max_fee), 'maxPriorityFeePerGas': int(min(priority, max_fee))}
    gas_price = int(await asyncio.to_thread(lambda: w3.eth.gas_price))
    gas_price = int(gas_price * multiplier)
    if ceiling and gas_price > ceiling:
        gas_price = ceiling
    return {'gasPrice': gas_price}

async def ensure_token_approval(chain: str, token_address: str, spender: str, amount: int):
    w3 = w3_instances.get(chain)
    if not w3 or not PRIVATE_KEY: return None
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        async with web3_semaphore:
            allowance = await asyncio.to_thread(
                token.functions.allowance(WALLET_ADDRESS, Web3.to_checksum_address(spender)).call
            )
        if allowance >= amount: return None
        # Approve only what this sell needs (issue 2.5) instead of 2**256-1.
        # APPROVAL_MULTIPLIER > 1 reduces approval transactions but leaves a
        # larger standing allowance that a router bug could drain — keep it at
        # 1.0 unless you accept that trade-off.
        approve_amount = int(amount * APPROVAL_MULTIPLIER)
        gas_fields = await build_gas_fields(w3, chain, multiplier=1.1)
        async with tx_lock(chain):
            nonce = await asyncio.to_thread(w3.eth.get_transaction_count, WALLET_ADDRESS, 'pending')
            approve_tx = token.functions.approve(
                Web3.to_checksum_address(spender), approve_amount
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 100000, 'nonce': nonce, **gas_fields,
            })
            signed = w3.eth.account.sign_transaction(approve_tx, PRIVATE_KEY)
            raw_tx = getattr(signed, 'raw_transaction', getattr(signed, 'rawTransaction', None))
            if raw_tx is None:
                logger.error("Approval failed: could not get raw transaction bytes")
                return None
            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, raw_tx)
        receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
        if receipt.status == 1:
            logger.info(f"Approval confirmed: {tx_hash.hex()} (allowance {approve_amount})")
            return tx_hash.hex()
        else:
            logger.error(f"Approval failed: {tx_hash.hex()}")
            return None
    except Exception as e:
        logger.error(f"Approval failed: {e}")
        return None

async def quote_exact_output_v3(w3, chain, token_in, token_out, amount_in, fee_tier):
    quoter_addr = get_quoter_v2(chain)
    if not quoter_addr or not Web3.is_address(quoter_addr):
        logger.warning(f"QuoterV2 not configured for {chain}")
        return None
    try:
        async with web3_semaphore:
            quoter = w3.eth.contract(address=Web3.to_checksum_address(quoter_addr), abi=QUOTER_V2_ABI)
            params = (Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out), amount_in, fee_tier, 0)
            result = await asyncio.to_thread(quoter.functions.quoteExactInputSingle(params).call)
            amount_out = int(result[0])
            logger.info(f"QuoterV2 success {chain} fee={fee_tier}: {amount_out}")
            return amount_out
    except Exception as e:
        err = str(e).lower()
        logger.debug(f"QuoterV2 call failed {chain} fee={fee_tier}: {e}")
        return None

async def try_v3_swap(w3, chain, token_in, token_out, amount_in,
                      is_eth_input, slippage):
    """
    Quote ALL fee tiers first via QuoterV2, pick the best, submit ONE transaction.
    No fallback estimates — if the quoter can't find a pool, we don't swap.
    (issue 4.9: the unused price_native/token_decimals params were removed.)
    """
    router_addr = get_router_v3(chain)
    if not router_addr or not Web3.is_address(router_addr):
        return False, f"No V3 router configured for {chain}. Set {chain.upper()}_ROUTER_V3 in .env.", 0

    router = w3.eth.contract(address=Web3.to_checksum_address(router_addr), abi=V3_ROUTER_ABI)
    fee_tiers = V3_FEE_TIERS_BY_CHAIN.get(chain, V3_FEE_TIERS)

    # Phase 1: Quote every fee tier (no transactions submitted)
    best_amount_out = 0
    best_fee = None
    quotes_tried = []

    for fee in fee_tiers:
        amount_out = await quote_exact_output_v3(w3, chain, token_in, token_out, amount_in, fee)
        if amount_out and amount_out > 0:
            quotes_tried.append(f"fee={fee} -> {amount_out}")
            if amount_out > best_amount_out:
                best_amount_out = amount_out
                best_fee = fee
        else:
            quotes_tried.append(f"fee={fee} -> no pool")

    # No valid quote found -> fail gracefully (NO transaction submitted)
    if best_fee is None or best_amount_out == 0:
        detail = " | ".join(quotes_tried)
        logger.warning(f"No V3 pool found for {chain} token={token_out[:10]}... tried: {detail}")
        return False, f"No V3 pool found on {chain} (tried fees {fee_tiers}) — check Render logs for QuoterV2 errors", 0

    # Phase 2: Submit ONE transaction with the best quote
    amount_out_min = int(best_amount_out * (1 - slippage / 100))
    if amount_out_min <= 0:
        amount_out_min = 1

    params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        best_fee,
        Web3.to_checksum_address(WALLET_ADDRESS),
        amount_in,
        amount_out_min,
        0,
    )

    try:
        gas_fields = await build_gas_fields(w3, chain, multiplier=1.2)
        # Serialize nonce allocation per chain (issue 4.7): fetch nonce, build,
        # sign and send while holding the lock, so a concurrent buy/sell cannot
        # grab the same nonce. The receipt wait happens outside the lock.
        async with tx_lock(chain):
            nonce = await asyncio.to_thread(w3.eth.get_transaction_count, WALLET_ADDRESS, 'pending')
            tx_dict = {
                'from': WALLET_ADDRESS,
                'nonce': nonce,
                **gas_fields,
            }
            if is_eth_input:
                tx_dict['value'] = amount_in

            # Try to estimate gas; fallback to hardcoded if it fails
            try:
                estimated_gas = await asyncio.to_thread(
                    router.functions.exactInputSingle(params).estimate_gas, tx_dict
                )
                tx_dict['gas'] = int(estimated_gas * 1.3)
                logger.info(f"Gas estimated: {estimated_gas} | using {tx_dict['gas']} for {chain}")
            except Exception as gas_err:
                tx_dict['gas'] = 350000
                logger.warning(f"Gas estimation failed for {chain}, using fallback 350k: {gas_err}")

            tx = router.functions.exactInputSingle(params).build_transaction(tx_dict)
            signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            raw_tx = getattr(signed, 'raw_transaction', getattr(signed, 'rawTransaction', None))
            if raw_tx is None:
                return False, "Failed to get raw transaction bytes from signed tx", 0

            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, raw_tx)
        logger.info(f"V3 swap tx sent: {tx_hash.hex()} | fee={best_fee} | minOut={amount_out_min} | gas={tx_dict['gas']}")
        receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)

        if receipt.status != 1:
            revert_reason = ""
            try:
                replay = w3.eth.call(tx, receipt.blockNumber)
            except Exception as revert_err:
                revert_reason = str(revert_err)
            return False, f"Tx reverted: {tx_hash.hex()} | Reason: {revert_reason[:200]}", 0

        return True, tx_hash.hex(), best_amount_out

    except Exception as e:
        logger.error(f"V3 swap exception for fee={best_fee}: {e}")
        return False, str(e), 0


async def quote_v2_best_path(router, chain: str, token_in: str, token_out: str, amount_in: int):
    """Try direct + multi-hop V2 paths, return (path, amounts) with best output.

    Many runner tokens only have a stablecoin pair, so a direct WNATIVE route
    fails even though a two-hop route exists. This is why 'no quote' was common.
    """
    def cs(addr): return Web3.to_checksum_address(addr)
    weth = get_weth_address(chain)
    candidates = [[cs(token_in), cs(token_out)]]
    for mid in ([weth] if weth else []) + V2_HOP_STABLES.get(chain, []):
        if not mid:
            continue
        if mid.lower() in (token_in.lower(), token_out.lower()):
            continue
        candidates.append([cs(token_in), cs(mid), cs(token_out)])

    best = None
    for path in candidates:
        try:
            amounts = await asyncio.to_thread(router.functions.getAmountsOut(amount_in, path).call)
            if amounts and len(amounts) == len(path) and int(amounts[-1]) > 0:
                if best is None or int(amounts[-1]) > int(best[1][-1]):
                    best = (path, [int(a) for a in amounts])
        except Exception:
            continue
    return best


async def try_v2_swap(w3, chain, token_in, token_out, amount_in, is_eth_input, slippage):
    """Fallback V2 swap using Uniswap/PancakeSwap V2 Router (with multi-hop)."""
    v2_router_addr = get_v2_router(chain)
    if not v2_router_addr or not Web3.is_address(v2_router_addr):
        return False, f"No V2 router configured for {chain}", 0

    router = w3.eth.contract(address=Web3.to_checksum_address(v2_router_addr), abi=V2_ROUTER_ABI)
    deadline = int(time.time()) + 300

    try:
        async with web3_semaphore:
            # Quote first, across direct and multi-hop paths
            best = await quote_v2_best_path(router, chain, token_in, token_out, amount_in)
            if not best:
                return False, f"No V2 route on {chain} (tried direct + WNATIVE/stable hops)", 0
            path, amounts_out = best
            expected_out = amounts_out[-1]
            amount_out_min = int(expected_out * (1 - slippage / 100))
            if amount_out_min <= 0:
                amount_out_min = 1

            gas_fields = await build_gas_fields(w3, chain, multiplier=1.2)
            async with tx_lock(chain):
                nonce = await asyncio.to_thread(w3.eth.get_transaction_count, WALLET_ADDRESS, 'pending')
                tx_dict = {
                    'from': WALLET_ADDRESS,
                    'nonce': nonce,
                    'gas': 250000,
                    **gas_fields,
                }

                if is_eth_input:
                    tx_dict['value'] = amount_in
                    tx = router.functions.swapExactETHForTokens(amount_out_min, path, WALLET_ADDRESS, deadline).build_transaction(tx_dict)
                else:
                    tx = router.functions.swapExactTokensForETH(amount_in, amount_out_min, path, WALLET_ADDRESS, deadline).build_transaction(tx_dict)

                signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                raw_tx = getattr(signed, 'raw_transaction', getattr(signed, 'rawTransaction', None))
                if raw_tx is None:
                    return False, "Failed to get raw transaction bytes from signed tx", 0

                tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, raw_tx)
            logger.info(f"V2 swap tx sent: {tx_hash.hex()} | minOut={amount_out_min} | gas={tx_dict['gas']}")
            receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)

            if receipt.status != 1:
                return False, f"V2 tx reverted: {tx_hash.hex()}", 0

            return True, tx_hash.hex(), expected_out

    except Exception as e:
        logger.error(f"V2 swap exception: {e}")
        return False, str(e), 0

def paper_fill_tokens(amount_native: float, price_native: float, slippage_pct: float) -> Optional[float]:
    """Model a paper fill at the observed native price minus fee/slippage (4.16).

    Returns None when there is no usable price so the caller can fall back.
    """
    if price_native is None or price_native <= 0:
        return None
    effective_price = price_native * (1 + slippage_pct / 100.0)
    if effective_price <= 0:
        return None
    return (amount_native * (1 - PAPER_FEE_PCT / 100.0)) / effective_price


async def execute_buy(chain: str, token_address: str, amount_native: float, slippage: float = None, price_native: float = None):
    if slippage is None:
        slippage = float(db_get_setting("slippage", DEFAULT_SLIPPAGE))
    # Paper mode is handled first: it must work with no web3/wallet at all.
    if PAPER_TRADING:
        tokens = paper_fill_tokens(amount_native, price_native or 0.0, slippage)
        if tokens is None:
            logger.warning("Paper buy without price_native; using nominal 1000 tokens/native")
            tokens = amount_native * 1000
        db_log_paper_trade(chain, token_address, "???", "BUY", amount_native, tokens, 0.0)
        logger.info(f"PAPER BUY: {amount_native} {NATIVE_SYMBOL[chain]} -> {tokens:.4f} tokens on {chain}")
        return True, "PAPER_TRADE", tokens
    w3 = w3_instances.get(chain)
    weth_address = get_weth_address(chain)
    if not w3:
        return False, f"No Web3 RPC connection for {chain}. Check RPCS config or redeploy.", 0.0
    if not weth_address:
        return False, f"No WETH/Wrapped Native address for {chain}. Set {chain.upper()}_WNATIVE or ROBINHOOD_WNATIVE in .env.", 0.0
    amount_in_wei = w3.to_wei(amount_native, 'ether')
    balance = await asyncio.to_thread(w3.eth.get_balance, WALLET_ADDRESS)
    if balance < amount_in_wei:
        return False, f"Insufficient {NATIVE_SYMBOL[chain]}: {w3.from_wei(balance, 'ether')}", 0.0
    decimals = await get_token_decimals(chain, token_address)
    success, tx_hash, tokens_received = await try_v3_swap(
        w3, chain, weth_address, token_address, amount_in_wei,
        is_eth_input=True, slippage=slippage
    )
    if success:
        human_tokens = tokens_received / (10 ** decimals) if tokens_received > 0 else 0
        logger.info(f"V3 Buy confirmed: {human_tokens} tokens for {amount_native} {NATIVE_SYMBOL[chain]}")
        return True, tx_hash, human_tokens
    else:
        # Fallback to V2
        logger.info(f"V3 failed, trying V2 fallback for {chain} {token_address[:10]}...")
        success2, tx_hash2, tokens_received2 = await try_v2_swap(
            w3, chain, weth_address, token_address, amount_in_wei,
            is_eth_input=True, slippage=slippage
        )
        if success2:
            human_tokens = tokens_received2 / (10 ** decimals) if tokens_received2 > 0 else 0
            logger.info(f"V2 Buy confirmed: {human_tokens} tokens for {amount_native} {NATIVE_SYMBOL[chain]}")
            return True, tx_hash2, human_tokens
        return False, f"V3: {tx_hash} | V2: {tx_hash2}", 0.0

async def execute_sell(chain: str, token_address: str, percentage: float = 100.0, slippage: float = None):
    if slippage is None:
        slippage = float(db_get_setting("slippage", DEFAULT_SLIPPAGE))
    # Paper mode first so it works with no web3/wallet.
    if PAPER_TRADING:
        # Value the sold tokens at the live native price so paper P&L is no
        # longer fiction (issue 4.16).
        pos_row = db_conn.execute(
            "SELECT amount_tokens FROM positions WHERE chain=? AND token_address=? "
            "AND status='open' ORDER BY entry_time DESC LIMIT 1",
            (chain, token_address),
        ).fetchone()
        native_received = 0.0
        if pos_row and pos_row[0]:
            tokens_sold = float(pos_row[0]) * (percentage / 100.0)
            price_native = await get_token_price_native(None, chain, token_address)
            if price_native > 0:
                native_received = tokens_sold * price_native * (1 - PAPER_FEE_PCT / 100.0)
        db_log_paper_trade(chain, token_address, "???", "SELL", native_received, 0.0, 0.0)
        logger.info(f"PAPER SELL: {percentage}% on {chain} -> ~{native_received:.6f} {NATIVE_SYMBOL[chain]}")
        return True, "PAPER_TRADE", float(native_received)
    w3 = w3_instances.get(chain)
    router_addr = get_router_v3(chain)
    weth_address = get_weth_address(chain)
    if not w3:
        return False, f"No Web3 RPC connection for {chain}.", 0.0
    if not router_addr:
        return False, f"No V3 router for {chain}. Set {chain.upper()}_ROUTER_V3 in .env.", 0.0
    if not weth_address:
        return False, f"No WETH address for {chain}. Set {chain.upper()}_WNATIVE in .env.", 0.0
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
        logger.info(f"V3 Sell confirmed: {percentage}% sold, received {native_received} W{NATIVE_SYMBOL[chain]}")
        return True, tx_hash, float(native_received)
    else:
        # Fallback to V2
        logger.info(f"V3 sell failed, trying V2 fallback for {chain} {token_address[:10]}...")
        success2, tx_hash2, weth_received2 = await try_v2_swap(
            w3, chain, token_address, weth_address, sell_amount,
            is_eth_input=False, slippage=slippage
        )
        if success2:
            native_received = w3.from_wei(weth_received2, 'ether') if weth_received2 > 0 else 0
            logger.info(f"V2 Sell confirmed: {percentage}% sold, received {native_received} W{NATIVE_SYMBOL[chain]}")
            return True, tx_hash2, float(native_received)
        return False, f"V3: {tx_hash} | V2: {tx_hash2}", 0.0

async def open_position(chain, token_address, symbol, amount_native, price_usd, price_native=None):
    trailing_stop = float(db_get_setting("trailing_stop", DEFAULT_TRAILING_STOP))
    tp_levels_raw = db_get_setting("take_profit_levels", json.dumps(DEFAULT_TP_LEVELS))
    tp_levels = json.loads(tp_levels_raw)
    success, tx_hash, tokens_received = await execute_buy(chain, token_address, amount_native, price_native=price_native)
    if not success:
        await tg_send(f"❌ <b>Buy failed for {esc(symbol)}</b>\n{esc(tx_hash)}", parse_mode=ParseMode.HTML)
        return None
    pos_id = db_add_position(
        chain, token_address, symbol, price_usd, tokens_received,
        amount_native, trailing_stop, tp_levels, tx_hash,
        paper=1 if PAPER_TRADING else 0
    )
    mode = "📄 PAPER" if PAPER_TRADING else "💰 LIVE"
    await tg_send(
        f"{mode} <b>Position Opened</b>\n"
        f"{esc(symbol)} on {chain.upper()}\n"
        f"Spent: {amount_native} {NATIVE_SYMBOL[chain]}\n"
        f"Received: {tokens_received:.4f} {esc(symbol)}\n"
        f"Entry: ${price_usd:.6f}\n"
        f"Trailing stop: {trailing_stop}%\n"
        f"Tx: <code>{esc(tx_hash)}</code>",
        parse_mode=ParseMode.HTML,
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
    await tg_send(
        f"{mode} <b>Position Closed</b>\n"
        f"{esc(symbol)} on {chain.upper()}\n"
        f"Sold: {remaining_pct:.1f}%\n"
        f"Received: {native_received:.4f} W{NATIVE_SYMBOL[chain]}\n"
        f"Tx: <code>{esc(tx_hash)}</code>",
        parse_mode=ParseMode.HTML,
    )
    return True, "Closed"

async def sell_position_pct(pos_id: int, pct: float):
    pos = db_get_position(pos_id)
    if not pos: return False, "Position not found"
    if pos['remaining_pct'] <= 0: return False, "Already closed"
    actual_pct = min(pct, pos['remaining_pct'])
    success, tx_hash, native_received = await execute_sell(pos['chain'], pos['token_address'], actual_pct)
    if not success: return False, f"Sell failed: {tx_hash}"
    entry = pos['entry_price']
    current_price = await get_token_price_usd(None, pos['chain'], pos['token_address'])
    pnl = ((current_price - entry) / entry * 100) if entry > 0 and current_price > 0 else 0
    db_reduce_position(pos_id, actual_pct, pnl, tx_hash)
    mode = "📄 PAPER" if pos['paper_trade'] else "💰 LIVE"
    await tg_send(
        f"{mode} <b>Sold {actual_pct:.0f}% of {esc(pos['symbol'])}</b>\n"
        f"Remaining: {pos['remaining_pct'] - actual_pct:.1f}%\n"
        f"Received: {native_received:.4f} W{NATIVE_SYMBOL[pos['chain']]}\n"
        f"Tx: <code>{esc(tx_hash)}</code>",
        parse_mode=ParseMode.HTML,
    )
    return True, "Sold"

# ═══════════════════════════════════════════════════════════════════════════════
# POSITION MONITOR (Trailing Stop + Take Profits)
# ═══════════════════════════════════════════════════════════════════════════════

def _deepest_pair(pairs):
    """Pick the pair with the most liquidity (issue 4.16: was blindly pairs[0])."""
    if not pairs or not isinstance(pairs, list):
        return None
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


async def _fetch_token_pairs(session, chain, token_address):
    url = f"https://api.dexscreener.com/tokens/v1/{chain}/{token_address}"
    if session is None:
        # sell_position_pct calls this without a session; without the temporary
        # one the fetch always failed and P&L was recorded as 0 (issue 4.3).
        async with aiohttp.ClientSession() as temp_session:
            return await fetch_json(temp_session, url)
    return await fetch_json(session, url)


async def get_token_quote(session, chain, token_address) -> Tuple[float, float]:
    """Return (price_usd, price_native) from the token's deepest pair."""
    try:
        data = await _fetch_token_pairs(session, chain, token_address)
        pair = _deepest_pair(data)
        if pair:
            return float(pair.get("priceUsd") or 0), float(pair.get("priceNative") or 0)
    except Exception as e:
        logger.debug(f"Price fetch failed: {e}")
    return 0.0, 0.0


async def get_token_price_usd(session, chain, token_address):
    return (await get_token_quote(session, chain, token_address))[0]


async def get_token_price_native(session, chain, token_address):
    return (await get_token_quote(session, chain, token_address))[1]


async def get_native_usd_price(session, chain: str) -> float:
    """USD price of the chain's wrapped native token, for USD-risk sizing (4.5)."""
    now = time.time()
    cached = _native_price_cache.get(chain)
    if cached and now - cached[1] < 300:
        return cached[0]
    weth = get_weth_address(chain)
    if not weth:
        return cached[0] if cached else 0.0
    try:
        data = await _fetch_token_pairs(session, chain, weth)
        pair = _deepest_pair(data)
        if pair:
            price = float(pair.get("priceUsd") or 0)
            if price > 0:
                _native_price_cache[chain] = (price, now)
                return price
    except Exception as e:
        logger.debug(f"Native price fetch failed for {chain}: {e}")
    return cached[0] if cached else 0.0

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
                                await tg_send(
                                    f"🎯 {mode} <b>Take Profit Hit!</b>\n"
                                    f"{esc(pos['symbol'])} on {pos['chain'].upper()}\n"
                                    f"P&L: +{pnl_pct:.1f}%\n"
                                    f"Sold: {actual_sell:.1f}%\n"
                                    f"Received: {native_received:.4f} W{NATIVE_SYMBOL[pos['chain']]}\n"
                                    f"Tx: <code>{esc(tx_hash)}</code>",
                                    parse_mode=ParseMode.HTML,
                                )
                                tp_triggered = True
                                break
                if tp_triggered: continue
                if drop_from_peak >= trailing_stop and pos['remaining_pct'] > 0:
                    success, tx_hash, native_received = await execute_sell(pos['chain'], pos['token_address'], 100.0)
                    if success:
                        db_close_position(pos['id'], pnl_pct, tx_hash)
                        mode = "📄 PAPER" if pos['paper_trade'] else "💰 LIVE"
                        emoji = "🛑" if pnl_pct >= 0 else "🔴"
                        await tg_send(
                            f"{emoji} {mode} <b>Trailing Stop Hit!</b>\n"
                            f"{esc(pos['symbol'])} on {pos['chain'].upper()}\n"
                            f"Peak: ${highest_price:.6f}\n"
                            f"Current: ${current_price:.6f}\n"
                            f"Drop: {drop_from_peak:.1f}%\n"
                            f"Final P&L: {pnl_pct:+.1f}%\n"
                            f"Tx: <code>{esc(tx_hash)}</code>",
                            parse_mode=ParseMode.HTML,
                        )
            await asyncio.sleep(POSITION_CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Position monitor error: {e}")
            await asyncio.sleep(POSITION_CHECK_INTERVAL)

# ═══════════════════════════════════════════════════════════════════════════════
# CA PASTE DETECTION (BonkBot Style) — PARALLEL across all chains
# ═══════════════════════════════════════════════════════════════════════════════

CA_REGEX = re.compile(r'0x[a-fA-F0-9]{40}')

async def detect_chain_for_ca(session, ca: str):
    """Try all chains on DexScreener sequentially, return first match."""
    for chain in NETWORKS:
        try:
            url = f"https://api.dexscreener.com/tokens/v1/{chain}/{ca}"
            data = await fetch_json(session, url)
            if data and isinstance(data, list) and len(data) > 0:
                logger.info(f"CA found on {chain}: {ca[:20]}...")
                return chain, data[0]
        except Exception as e:
            logger.debug(f"CA check failed for {chain}: {e}")
            continue
    # Fallback: try DexScreener search API
    try:
        search_url = f"https://api.dexscreener.com/latest/dex/search?q={ca}"
        search_data = await fetch_json(session, search_url)
        if search_data and "pairs" in search_data and len(search_data["pairs"]) > 0:
            pair = search_data["pairs"][0]
            chain = pair.get("chainId")
            if chain in NETWORKS:
                logger.info(f"CA found via search on {chain}: {ca[:20]}...")
                return chain, pair
    except Exception as e:
        logger.debug(f"CA search fallback failed: {e}")
    logger.warning(f"CA not found on any chain: {ca[:20]}...")
    return None, None

async def handle_ca_paste(ca: str, message):
    """When user pastes a CA, fetch token and show buy UI."""
    await tg_send(f"🔍 Looking up <code>{esc(ca)}</code>...", parse_mode=ParseMode.HTML)

    async with aiohttp.ClientSession() as temp_session:
        chain, pair = await detect_chain_for_ca(temp_session, ca)
        if not chain or not pair:
            await tg_send(
                f"❌ Token not found on any supported chain.\nTried: {', '.join(NETWORKS)}",
                parse_mode=ParseMode.HTML,
            )
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
            f"🪙 <b>{esc(name)}</b> ({esc(symbol)})\n"
            f"🔗 Chain: <b>{chain.upper()}</b>\n"
            f"💰 Price: ${price_usd:.6f}\n"
            f"💧 Liquidity: ${liquidity:,.0f}\n"
            f"📊 Market Cap: ${market_cap:,.0f}\n"
            f"📈 5m Volume: ${vol_5m:,.0f}\n"
            f"📈 5m Change: {chg_5m:+.1f}%\n\n"
            f"📝 <b>CA:</b> <code>{ca}</code>"
        )

        keyboard = build_buy_keyboard(chain, ca, symbol, price_native, price_usd)
        await tg_send(
            text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=keyboard,
        )

# ═══════════════════════════════════════════════════════════════════════════════
# BUY KEYBOARD BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_buy_amounts(chain: str):
    """Preset buy amounts for a chain.

    Keyed per chain (issue 4.4) so /setamounts base no longer overwrites the
    Ethereum presets the way the old native-symbol key did.
    """
    fallback = DEFAULT_BUY_AMOUNTS.get(chain, [0.001, 0.003, 0.005, 0.01])
    amounts_raw = db_get_setting(_buy_amounts_key(chain), json.dumps(fallback))
    try:
        amounts = json.loads(amounts_raw)
    except (TypeError, ValueError):
        return fallback
    return amounts if isinstance(amounts, list) and amounts else fallback

def build_buy_keyboard(chain, token_address, symbol, price_native=None, price_usd=None):
    """Build inline keyboard with buy buttons.

    The chain/token pair is stored server-side and referenced by a short id, so
    callback_data stays well under Telegram's 64-byte cap (issue 4.13).
    When a USD risk is configured, a one-tap "Risk $X" button is added (4.5).
    """
    amounts = get_buy_amounts(chain)
    ref = register_callback_target(
        chain, token_address, symbol, price_usd or 0.0, price_native or 0.0
    )
    keyboard = []
    row = []
    for amt in amounts:
        label = f"💰 {amt} {NATIVE_SYMBOL[chain]}"
        row.append(InlineKeyboardButton(label, callback_data=f"buy:{ref}:{amt}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    risk_usd = float(db_get_setting("risk_usd", DEFAULT_RISK_USD) or 0)
    if risk_usd > 0:
        keyboard.append([
            InlineKeyboardButton(f"💵 Risk ${risk_usd:g}", callback_data=f"riskbuy:{ref}")
        ])

    keyboard.append([InlineKeyboardButton("✏️ Custom Amount", callback_data=f"custom:{ref}")])
    keyboard.append([
        InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/{chain}/{token_address}"),
        InlineKeyboardButton("🚫 Skip", callback_data="noop"),
    ])
    return InlineKeyboardMarkup(keyboard)

def build_alert_keyboard(chain, token_address, symbol, price_usd=None, price_native=None):
    """Build inline keyboard for scan alerts."""
    return build_buy_keyboard(chain, token_address, symbol, price_usd, price_native)

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

async def fetch_pair_info(chain: str, token_address: str) -> dict:
    """Symbol + price info for a token, shared by every buy path."""
    info = {"symbol": "???", "price_usd": 0.0, "price_native": 0.0}
    try:
        async with aiohttp.ClientSession() as temp_session:
            data = await fetch_json(
                temp_session, f"https://api.dexscreener.com/tokens/v1/{chain}/{token_address}"
            )
        pair = _deepest_pair(data)
        if pair:
            info["symbol"] = (pair.get("baseToken") or {}).get("symbol") or "???"
            info["price_usd"] = float(pair.get("priceUsd") or 0)
            info["price_native"] = float(pair.get("priceNative") or 0)
    except Exception as e:
        logger.debug(f"Pair info fetch failed for {chain} {token_address[:10]}...: {e}")
    return info


async def _buy_via_callback(chain, token_address, amount, target=None):
    info = await fetch_pair_info(chain, token_address)
    symbol = info["symbol"]
    if symbol == "???" and target:
        symbol = target.get("symbol") or "???"
    await tg_send(
        f"⏳ Buying {amount:g} {NATIVE_SYMBOL[chain]} of {esc(symbol)}...",
        parse_mode=ParseMode.HTML,
    )
    pos_id = await open_position(
        chain, token_address, symbol, amount, info["price_usd"], info["price_native"]
    )
    if pos_id:
        logger.info(f"Position opened via callback: {symbol} id={pos_id}")


async def handle_callback_query(query):
    """Process inline button clicks."""
    data = query.data
    if not data: return
    try:
        await bot.answer_callback_query(query.id)
    except Exception:
        pass

    parts = data.split(":")
    action = parts[0]

    if action == "buy" and len(parts) >= 3:
        target = get_callback_target(parts[1])
        if not target:
            await tg_send("⌛ This button expired. Paste the CA again to buy.")
            return
        try:
            amount = float(parts[2])
        except ValueError:
            await tg_send("❌ Invalid amount in button.")
            return
        await _buy_via_callback(target["chain"], target["token"], amount, target)

    elif action == "riskbuy" and len(parts) >= 2:
        # USD-risk position sizing (issue 4.5): /risk now actually sizes trades.
        target = get_callback_target(parts[1])
        if not target:
            await tg_send("⌛ This button expired. Paste the CA again to buy.")
            return
        chain = target["chain"]
        risk_usd = float(db_get_setting("risk_usd", DEFAULT_RISK_USD) or 0)
        if risk_usd <= 0:
            await tg_send("Set a USD risk first with /risk <usd>.")
            return
        native_usd = await get_native_usd_price(None, chain)
        if native_usd <= 0:
            await tg_send("Could not price the native token for risk sizing; use a preset amount.")
            return
        await _buy_via_callback(chain, target["token"], risk_usd / native_usd, target)

    elif action == "custom" and len(parts) >= 2:
        target = get_callback_target(parts[1])
        if not target:
            await tg_send("⌛ This button expired. Paste the CA again to buy.")
            return
        user_state[state_key(CHAT_ID)] = {
            "action": "custom_buy", "chain": target["chain"], "token": target["token"],
        }
        await tg_send(
            f"✏️ <b>Custom Buy</b>\nReply with the amount of {NATIVE_SYMBOL[target['chain']]} you want to spend:",
            parse_mode=ParseMode.HTML,
        )

    elif action == "pos" and len(parts) >= 2:
        pos_id = int(parts[1])
        pos = db_get_position(pos_id)
        if not pos:
            await tg_send("Position not found.")
            return
        mode = "📄 PAPER" if pos['paper_trade'] else "💰 LIVE"
        text = (
            f"{mode} <b>Position #{pos['id']}</b>\n"
            f"{esc(pos['symbol'])} on {pos['chain'].upper()}\n"
            f"Entry: ${pos['entry_price']:.6f}\n"
            f"Highest: ${pos['highest_price']:.6f}\n"
            f"Amount: {pos['amount_tokens']:.4f} tokens\n"
            f"Remaining: {pos['remaining_pct']:.1f}%\n"
            f"Trailing SL: {pos['trailing_stop_pct']}%"
        )
        keyboard = build_position_actions_keyboard(pos_id)
        await tg_send(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    elif action == "sellpct" and len(parts) >= 3:
        pos_id = int(parts[1])
        pct = float(parts[2])
        await sell_position_pct(pos_id, pct)

    elif action == "buymore" and len(parts) >= 2:
        pos_id = int(parts[1])
        pos = db_get_position(pos_id)
        if not pos:
            await tg_send("Position not found.")
            return
        keyboard = build_buy_keyboard(pos['chain'], pos['token_address'], pos['symbol'])
        await tg_send(
            f"💰 <b>Buy More {esc(pos['symbol'])}</b>\nSelect amount:",
            parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )

    elif action == "setsl" and len(parts) >= 2:
        pos_id = int(parts[1])
        user_state[state_key(CHAT_ID)] = {"action": "set_sl", "pos_id": pos_id}
        await tg_send(
            "🛡 <b>Set Trailing Stop Loss</b>\nReply with the new trailing stop % (e.g., 10, 15, 20):",
            parse_mode=ParseMode.HTML,
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
    key = state_key(chat_id)

    state = user_state.get(key)
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
                del user_state[key]
                info = await fetch_pair_info(chain, token)
                symbol = info["symbol"]
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ Buying {amount:g} {NATIVE_SYMBOL[chain]} of {esc(symbol)}...",
                    parse_mode=ParseMode.HTML,
                )
                await open_position(chain, token, symbol, amount, info["price_usd"], info["price_native"])
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
                del user_state[key]
                await bot.send_message(chat_id=chat_id, text=f"✅ Trailing stop updated to {new_sl}%", parse_mode=ParseMode.HTML)
            except ValueError:
                await bot.send_message(chat_id=chat_id, text="❌ Invalid number.")
            return

    ca_match = CA_REGEX.search(text)
    if ca_match:
        ca = ca_match.group()
        await handle_ca_paste(ca, message)
        return

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

    elif cmd == "/debug":
        await log_trading_config()
        await bot.send_message(
            chat_id=CHAT_ID,
            text="✅ Config logged to console. Check your Render logs.",
            parse_mode=ParseMode.HTML
        )

    elif cmd == "/features":
        stats = FEATURE_LOGGER.stats()
        await bot.send_message(
            chat_id=CHAT_ID,
            text=(
                "<b>Feature logging</b>\n"
                f"Enabled: {stats['enabled']}\n"
                f"Rows logged: {stats['logged']}\n"
                f"Batches written: {stats['written_batches']}\n"
                f"Queued: {stats['queued']}\n"
                f"Dropped: {stats['dropped']}"
            ),
            parse_mode=ParseMode.HTML
        )

    elif cmd == "/risk" and args:
        try:
            usd = float(args[0])
            db_set_setting("risk_usd", str(usd))
            await bot.send_message(chat_id=CHAT_ID, text=f"✅ Risk per trade set to ${usd}", parse_mode=ParseMode.HTML)
        except ValueError:
            await bot.send_message(chat_id=CHAT_ID, text="Usage: /risk <usd_amount>", parse_mode=ParseMode.HTML)

    elif cmd == "/setamounts" and len(args) >= 2:
        chain = args[0].lower()
        if chain not in DEFAULT_BUY_AMOUNTS:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"Unknown chain. Use one of: {', '.join(NETWORKS)}",
                parse_mode=ParseMode.HTML
            )
            return
        amounts_str = " ".join(args[1:])
        try:
            amounts = [float(x.strip()) for x in amounts_str.split(",") if x.strip()]
            if not amounts or any(a <= 0 for a in amounts):
                raise ValueError("amounts must be positive")
            db_set_setting(_buy_amounts_key(chain), json.dumps(amounts))
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"✅ Buy amounts for {chain.upper()} updated: {amounts}",
                parse_mode=ParseMode.HTML
            )
        except ValueError:
            await bot.send_message(chat_id=CHAT_ID, text="Usage: /setamounts <chain> <amt1,amt2,amt3>", parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG DEBUG
# ═══════════════════════════════════════════════════════════════════════════════

async def address_has_code(chain: str, addr: str) -> bool:
    """True if the address is a deployed contract. Catches typo'd router/quoter
    addresses that pass Web3.is_address but silently return no quotes."""
    w3 = w3_instances.get(chain)
    if not w3 or not addr or not Web3.is_address(addr):
        return False
    try:
        code = await asyncio.to_thread(w3.eth.get_code, Web3.to_checksum_address(addr))
        return len(code) > 0
    except Exception:
        return False


async def log_trading_config():
    logger.info("========== TRADING CONFIG DEBUG ==========")
    for chain in NETWORKS:
        router = get_router_v3(chain)
        weth = get_weth_address(chain)
        quoter = get_quoter_v2(chain)
        rpc = RPCS.get(chain, "")
        w3 = w3_instances.get(chain)

        def status(addr, deployed):
            if not addr:
                return "MISSING"
            return "OK" if deployed else "NO CODE ⚠"

        logger.info(
            f"{chain.upper():12} | Router: {status(router, await address_has_code(chain, router))} "
            f"| WETH: {status(weth, await address_has_code(chain, weth))} "
            f"| Quoter: {status(quoter, await address_has_code(chain, quoter))} "
            f"| RPC: {'CONNECTED' if w3 and w3.is_connected() else 'OFFLINE'} "
            f"| RPC_URL: {rpc[:40]}..."
        )
    logger.info(f"Wallet: {WALLET_ADDRESS or 'NOT SET'}")
    logger.info(f"Paper Trading: {PAPER_TRADING}")
    logger.info("==========================================")


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM MENUS
# ═══════════════════════════════════════════════════════════════════════════════

async def send_start_menu():
    balances = []
    for chain in NETWORKS:
        bal = await get_wallet_balance(chain)
        if bal > 0:
            balances.append(f"  {chain.upper()}: {bal:.4f} {NATIVE_SYMBOL[chain]}")
    bal_text = "\n".join(balances) if balances else "  (empty or RPC error)"
    risk = db_get_setting("risk_usd", str(DEFAULT_RISK_USD))

    text = (
        f"🚀 <b>Pump Bot v5.4</b> — Manual Trader\n\n"
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
        f"/debug — Log config to console\n"
        f"/risk <usd> — Set $ risk per trade (adds a Risk buy button)\n"
        f"/setamounts <chain> <a,b,c> — Custom buy sizes (per chain)\n"
        f"/features — Feature-logging stats"
    )
    await tg_send(text, parse_mode=ParseMode.HTML)


async def send_positions_menu():
    positions = db_get_open_positions()
    if not positions:
        await tg_send("No open positions.", parse_mode=ParseMode.HTML)
        return
    text = "📊 <b>Your Positions</b>\nTap one to manage:"
    keyboard = build_positions_keyboard(positions)
    await tg_send(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


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
        f"Risk per Trade: ${risk} (used by the 💵 Risk buy button)\n"
        f"Paper Trading: {'✅ ON' if PAPER_TRADING else '❌ OFF'}\n"
        f"Paper fee model: {PAPER_FEE_PCT}%/side\n"
        f"Feature logging: {'✅ ON' if LOG_FEATURES else 'off'}\n"
        f"Wallet: <code>{WALLET_ADDRESS or 'Not set'}</code>"
    )
    await tg_send(text, parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM POLLING
# ═══════════════════════════════════════════════════════════════════════════════

async def telegram_polling_task():
    offset = 0
    while not shutdown_flag:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=["callback_query", "message"])
            for update in updates:
                offset = update.update_id + 1
                if update.callback_query:
                    cq = update.callback_query
                    chat_id = cq.message.chat_id if cq.message else None
                    user_id = cq.from_user.id if cq.from_user else None
                    if not is_authorized(chat_id, user_id):
                        logger.warning(f"Ignored callback from unauthorized chat/user: {chat_id}/{user_id}")
                        continue
                    _spawn_handler(handle_callback_query(cq))
                elif update.message and update.message.text:
                    user_id = update.message.from_user.id if update.message.from_user else None
                    if not is_authorized(update.message.chat.id, user_id):
                        logger.warning(f"Ignored message from unauthorized chat/user: {update.message.chat.id}/{user_id}")
                        continue
                    _spawn_handler(handle_text_message(update.message))
        except Exception as e:
            logger.error(f"Telegram polling error: {e}")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT SENDER
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

    age_display = f"{alert['age_minutes']:.0f}" if alert.get("age_minutes") is not None else "?"
    text = (
        f"🚨 <b>ONCHAIN PUMP — Score {alert['total_score']:.0f}/100</b>\n\n"
        f"<b>{esc(alert['name'])}</b> ({esc(alert['symbol'])})\n"
        f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n"
        f"🕒 Age: <b>{age_display} min</b>\n\n"
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
        f"<b>Signal Engine:</b> +{alert.get('signal_bonus', 0):.0f} / -{alert.get('signal_penalty', 0):.0f}\n"
        + (f"<i>{esc(', '.join(alert.get('signal_notes', [])))}</i>\n" if alert.get('signal_notes') else "")
        + f"📝 <b>Contract:</b> <code>{esc(alert['token_address'])}</code>"
    )

    keyboard = build_alert_keyboard(
        alert['chain'], alert['token_address'], alert['symbol'],
        price_usd=alert.get("price_usd"), price_native=alert.get("price_native"),
    )
    try:
        await tg_send(
            text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=keyboard,
        )
        alerts_sent += 1
        # Mark the feature row as actually alerted (Phase 2 labelling).
        FEATURE_LOGGER.mark_alert_sent(alert['chain'], alert['token_address'])
        logger.info(f"ALERT #{alerts_sent} sent → {alert['symbol']}@{alert['chain']} score={alert['total_score']}")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════════════════════

async def bot_task():
    global start_time, last_heartbeat_time, total_pairs_scanned, db_conn
    db_conn = init_db()
    FEATURE_LOGGER.start()
    start_time = time.time()
    last_heartbeat_time = start_time
    logger.info("Pump Bot v5.4 starting (Manual Trader + CA Paste)...")
    await log_trading_config()

    async with aiohttp.ClientSession() as session:
        if COINGECKO_API_KEY:
            await build_coingecko_id_map(session)
        monitor_task = asyncio.create_task(monitor_positions(session))
        polling_task = asyncio.create_task(telegram_polling_task())

        try:
            await tg_send(
                f"✅ <b>Pump Bot v5.4</b> started\n"
                f"Mode: {'📄 PAPER' if PAPER_TRADING else '💰 LIVE'}\n"
                f"Wallet: <code>{WALLET_ADDRESS or 'Not set'}</code>\n"
                f"Scan interval: {SCAN_INTERVAL}s\n"
                f"Alert threshold: {ALERT_THRESHOLD}/100\n"
                f"Feature logging: {'ON' if LOG_FEATURES else 'off'}\n\n"
                f"<b>Paste any CA to buy instantly!</b>",
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
                    await tg_send(
                        f"🫀 <b>Bot v5.4 Heartbeat</b>\n"
                        f"Uptime: {uptime:.1f}h\n"
                        f"Pairs scanned: {total_pairs_scanned}\n"
                        f"Tokens evaluated: {tokens_evaluated}\n"
                        f"Alerts sent: {alerts_sent}\n"
                        f"Open positions: {len(positions)}"
                        + (f"\nFeatures queued: {FEATURE_LOGGER.stats()['queued']}" if LOG_FEATURES else ""),
                        parse_mode=ParseMode.HTML,
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
                    await tg_send(
                        f"⚠️ <b>Bot cycle crashed</b>\n<code>{esc(str(e)[:300])}</code>\nRetrying in 60s...",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                await asyncio.sleep(60)

        monitor_task.cancel()
        polling_task.cancel()
        try:
            await monitor_task
            await polling_task
        except asyncio.CancelledError:
            pass
    FEATURE_LOGGER.stop()


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
    FEATURE_LOGGER.stop()
    if db_conn:
        db_conn.close()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health_check():
    positions = db_get_open_positions() if db_conn else []
    # The wallet address is deliberately NOT exposed here (issue 2.4): this
    # endpoint is bound to 0.0.0.0 and may be publicly reachable.
    return {
        "status": "alive",
        "alerts_sent": alerts_sent,
        "pairs_scanned": total_pairs_scanned,
        "tokens_evaluated": tokens_evaluated,
        "open_positions": len(positions),
        "threshold": ALERT_THRESHOLD,
        "paper_trading": PAPER_TRADING,
        "features": FEATURE_LOGGER.stats(),
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting Pump Bot v5.4 on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
