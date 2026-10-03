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
import csv
import time
import io
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
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
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

try:
    from uniswap_universal_router_decoder.router_codec import RouterCodec as _V4RouterCodec
    V4_CODEC_AVAILABLE = True
except ImportError:
    _V4RouterCodec = None
    V4_CODEC_AVAILABLE = False

# Load the .env that sits NEXT TO this file, never "whichever directory the
# process happened to start in". `load_dotenv()` with no argument resolves
# relative to the caller's working directory, so `python /path/to/bot.py`, a
# systemd unit with a different WorkingDirectory, or a container whose WORKDIR
# is not the repo loads *no* config at all — while every documented default that
# matters stays at its code value. The visible symptom is exactly the one
# reported against this repo: "MAX_MARKET_CAP_USD=100000 is in my .env, why do
# I still get alerted on $5M tokens?" — because that process never read the file.
# Which file was loaded is logged at startup and served at /health as `env_file`.
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_ENV_FILE):
    load_dotenv(_ENV_FILE)
    ENV_FILE_LOADED = _ENV_FILE
else:
    # No .env beside the module: keep the historical cwd search so an operator
    # who deliberately runs with the config elsewhere still gets it.
    load_dotenv()
    try:
        from dotenv import find_dotenv
        ENV_FILE_LOADED = find_dotenv() or "(no .env found)"
    except Exception:
        ENV_FILE_LOADED = "(no .env found)"


def _env_float(key: str, default: float) -> float:
    """Parse an env float, warn-and-default on anything unparseable.

    ``float(os.getenv(...))`` has three failure modes in this codebase, all of
    them bad: raised *per candidate* (``MIN_EFFECTIVE_SCORE``, ``MAX_MARKET_CAP_USD``,
    ``NEAR_MISS_POINTS``) where ``dispatch_alerts`` dropped the exception and the
    bot silently sent zero alerts for as long as the value stayed broken; raised
    *at import* (``RE_ALERT_COOLDOWN_HOURS``, ``MAX_ALLOWED_TAX``, ``WATCHLIST_*``)
    where the process exits before it can explain itself; and raised *per cycle*
    (``GT_PAGES_*``) where it crash-looped the whole scan. One helper, one
    behaviour: log the offending key and use the default. Defined this early
    because import-time constants call it too.
    """
    raw = os.getenv(key, "")
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(str(raw).strip().replace(",", ""))
    except (TypeError, ValueError):
        logging.getLogger("pump_bot_v5").warning(
            f"Ignoring invalid {key}={raw!r} — using {default}"
        )
        return float(default)

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
MAX_ALLOWED_TAX = _env_float("MAX_ALLOWED_TAX", 0.0)
VERBOSE_LOGGING = os.getenv("VERBOSE_LOGGING", "false").lower() == "true"

ALERT_THRESHOLD = int(os.getenv("MIN_SCORE", os.getenv("ALERT_THRESHOLD", "65")))

# Opt-in runner/false-positive signal engine (see signals.py) and GeckoTerminal
# discovery (see discovery.py). Both default OFF so behaviour is unchanged
# until you turn them on in .env.
USE_SIGNALS = os.getenv("USE_SIGNALS", "false").lower() == "true" and SIGNALS_AVAILABLE
USE_GECKOTERMINAL = os.getenv("USE_GECKOTERMINAL", "false").lower() == "true" and DISCOVERY_AVAILABLE

# DexScreener as a SECOND discovery source, alongside GeckoTerminal rather than
# instead of it. Measured 2026-09-30: boosts/profiles surface tokens at a median
# ~24h old (much later than new_pools' 3-5 minutes), but the list is tiny (~20
# tokens across four chains per cycle), skewed small (median mcap ~$107k) and
# mostly on uniswap/pancakeswap — venues the routers can actually trade. Empty
# string disables it; set "boosts,profiles".
DEXSCREENER_SOURCES = tuple(
    s.strip().lower() for s in os.getenv("DEXSCREENER_SOURCES", "").split(",") if s.strip()
)
DEXSCREENER_ENDPOINTS = {
    "boosts": "https://api.dexscreener.com/token-boosts/latest/v1",
    "boosts_top": "https://api.dexscreener.com/token-boosts/top/v1",
    "profiles": "https://api.dexscreener.com/token-profiles/latest/v1",
}

# Early-runner fast lane: lets a genuinely strong *young* pool alert even though
# its 1h/6h/24h windows are empty (and so its hand-tuned score can never reach
# MIN_SCORE). Requires USE_SIGNALS and every AND-condition in
# signals.early_runner_reasons() to hold. Default OFF.
EARLY_RUNNER_MODE = os.getenv("EARLY_RUNNER_MODE", "false").lower() == "true" and SIGNALS_AVAILABLE

# Abnormal-volume lane: alerts when a pool's 5m volume is a multiple of its OWN
# trailing baseline, AND-gated. This is the "screen the top coins by volume every
# scan and catch abnormal volume fast" strategy, as opposed to launch sniping: no
# age cap by default, and the signal is a step change rather than a size.
# Needs USE_SIGNALS (it lives in signals.py) and history from repeated scans of
# the same universe, which a volume-ranked feed provides.
VOLUME_SURGE_MODE = os.getenv("VOLUME_SURGE_MODE", "false").lower() == "true" and SIGNALS_AVAILABLE

# Seed-only sources: pools discovered here are recorded into the volume baseline
# history and the standing universe, but are NEVER scored and NEVER alert.
#
# This exists because a per-pool surge baseline needs ~3 sightings, and no list
# endpoint shows a pool while it is spiking (measured: 0/20 on every GT ranking).
# A pool the bot has never seen therefore cannot trigger the surge lane on its
# first appearance. Seeding the universe from a firehose like `new_pools` — which
# carries pools 3-5 minutes old — means every new pool is already being watched
# with a baseline by the time its volume moves, WITHOUT re-introducing the alert
# spam that feed causes when it is scored directly.
SEED_ONLY_SOURCES = tuple(
    x.strip().lower() for x in os.getenv("SEED_ONLY_SOURCES", "").split(",") if x.strip()
)

# Optional Telegram allowlist. Empty -> only CHAT_ID is accepted. Set this when
# CHAT_ID is a group so other members cannot run /sell, /risk, /setamounts.
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("ALLOWED_USER_IDS", "")) if x.strip().isdigit()
}

# /export guardrails: exporting the features table off-host is powerful but the
# CSV grows without bound, and a 30 MB document send can take minutes on a free
# host (or fail outright). Both knobs are env-tunable.
EXPORT_MAX_ROWS = int(os.getenv("EXPORT_MAX_ROWS", "20000"))
EXPORT_CHUNK_ROWS = int(os.getenv("EXPORT_CHUNK_ROWS", "5000"))

# ── Ignition watchlist ───────────────────────────────────────────────────────
# Discovery is event-based: GeckoTerminal's `new_pools` only carries pools from
# roughly the last 20 minutes, and `trending` only lists pools that are already
# hot. A pool that is born quiet, drops out of both feeds, and then runs hours
# later is therefore never looked at again — which is exactly what happened to
# boar on 2026-09-26: first evaluated at 13:53, while its $34k → $800k leg had
# already happened at 09:00 with +2242% in the following hour.
#
# This lane remembers pools the bot has already seen and re-prices the promising
# ones on a timer, so a pool that re-ignites after going quiet gets scored even
# though no feed is listing it. Re-checks go through DexScreener's batched
# /tokens/v1/{chain}/{a,b,c} endpoint (30 addresses per call, ~10 calls for a
# 300-pool universe), a *different* provider from GeckoTerminal, so they do not
# consume the shared GT budget that discovery and holder lookups compete for.
#
# Default OFF, like the other lanes. When enabling, watch the noise: a re-check
# carries no unique-buyer data (DexScreener has none), so the AND-gates in
# signals.py that depend on `buyers` are skipped for these candidates.
WATCHLIST_ENABLED = os.getenv("WATCHLIST_ENABLED", "false").lower() == "true"
WATCHLIST_MAX = max(0, int(os.getenv("WATCHLIST_MAX", "300")))           # re-checks per cycle
WATCHLIST_TTL_HOURS = float(os.getenv("WATCHLIST_TTL_HOURS", "24"))
WATCHLIST_RECHECK_MINUTES = float(os.getenv("WATCHLIST_RECHECK_MINUTES", "10"))
# Minimum best hand score for a pool to be *remembered*. 0 = remember
# everything evaluated, which is what a standing universe needs: the whole
# point is to be watching a pool BEFORE its volume spikes, and a pool that
# scores 0 today is exactly the one that can surge tomorrow.
WATCHLIST_MIN_BEST_SCORE = float(os.getenv("WATCHLIST_MIN_BEST_SCORE", "0"))

# CoingGecko ticker cache lifetime: the read side and the pruner must agree.
TICKER_TTL = 3600

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

# Holder concentration rarely changes scan-to-scan, so this cache is long —
# though shorter than SECURITY_TTL (1800), which is the one that must survive a
# provider outage. Applies to every provider (GeckoTerminal, Moralis, Blockscout).
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
SIGNAL_BONUS_WEIGHT = min(1.0, max(0.0, _env_float("SIGNAL_BONUS_WEIGHT", 0.0)))

# ── Configuration self-audit ─────────────────────────────────────────────────
# Nearly everything here is deliberately opt-in, so the code defaults are "off".
# A deployment that sets only the required variables therefore runs a different
# bot from the documented one, and the difference is invisible from outside: it
# discovers from the lagging DexScreener boost list, has no rug/wash gates and no
# early lane, and has no alert ceiling.
#
# This was not hypothetical. Alerting on $3M and $22M tokens, false positives on
# wash-traded pools, and no early entries is exactly the output of those defaults.
# Saying so at startup is cheaper than diagnosing it from alert screenshots later.
def config_warnings() -> list:
    """Settings left at a default that silently degrades this bot."""
    warns = []
    ceiling = _env_float("MAX_MARKET_CAP_USD", 0.0)
    if ceiling <= 0:
        warns.append(
            "MAX_MARKET_CAP_USD=0 -> NO alert ceiling. It will alert on tokens that "
            "already ran. Set 100000 for sub-100k entries, 50000 to be stricter."
        )
    if not USE_SIGNALS:
        warns.append(
            "USE_SIGNALS=false -> rug / wash-trade / unique-buyer gates AND the "
            "early-runner lane are ALL off (the lane needs USE_SIGNALS=true)."
        )
    if not USE_GECKOTERMINAL:
        warns.append(
            "USE_GECKOTERMINAL=false -> GeckoTerminal is off, so discovery is only "
            "as good as DEXSCREENER_SOURCES (boost/profile list: median ~24h old, "
            "never a pool while it is minutes old). If that is empty too, the only "
            "fallback left is DexScreener's dead /latest/dex/pairs endpoint -> NO "
            "candidates at all. Either way this is the main cause of 'it pinged me "
            "at $3M instead of $300k' — or of a bot that never pings."
        )
    if not EARLY_RUNNER_MODE:
        warns.append(
            "EARLY_RUNNER_MODE=false -> a young sub-50k-mcap pool can never alert."
        )
    if not VOLUME_SURGE_MODE and not EARLY_RUNNER_MODE:
        warns.append(
            "VOLUME_SURGE_MODE=false and EARLY_RUNNER_MODE=false -> the alert gate "
            "is the hand-tuned score alone, which the audit measures at AUC 0.46-0.57 "
            "(a coin flip). One of the AND-gated lanes should be on."
        )
    if not LOG_FEATURES:
        warns.append(
            "LOG_FEATURES=false -> no training rows, so the score can never be "
            "refitted against outcomes."
        )
    return warns


def config_notes() -> list:
    """Trade-offs that are configured correctly but worth stating once."""
    notes = []
    if not ALLOW_SECURITY_FALLBACK:
        notes.append(
            "ALLOW_SECURITY_FALLBACK=false -> BSC/ETH/Base pools GoPlus has not yet "
            "indexed are dropped, which is most pools in their first minutes."
        )
    if PAPER_TRADING:
        notes.append("PAPER_TRADING=true -> no real orders will be placed.")
    if RE_ALERTS_ENABLED:
        notes.append(
            "RE_ALERTS=true -> tokens already reported to you CAN be reported "
            "again (after RE_ALERT_COOLDOWN_HOURS, or earlier once the score "
            "gains RE_ALERT_MIN_IMPROVEMENT points). RE_ALERTS=false sends one "
            "message per token."
        )
    return notes


def effective_mcap_ceilings() -> Dict[str, float]:
    """The alert ceiling each chain is actually gated on, read fresh from env.

    Exposed at startup and in ``/health`` because the ceiling is the setting
    whose absence is least obvious from the outside: ``MAX_MARKET_CAP_USD``
    missing (or the process not having read the ``.env`` that sets it) means
    ``0`` = *no ceiling at all*, and the only symptom is alerts on tokens that
    already ran. This makes the running value inspectable instead of inferred.
    """
    default = _env_float("MAX_MARKET_CAP_USD", 0.0)
    return {chain: _chain_floor(chain, "MAX_MARKET_CAP_USD", default) for chain in NETWORKS}


def _fmt_ceilings(ceilings: Dict[str, float]) -> str:
    values = set(ceilings.values())
    if len(values) == 1:
        value = values.pop()
        return "⚠️ NONE (0)" if value <= 0 else f"${value:,.0f}"
    return ", ".join(
        f"{chain.upper()} {'off' if value <= 0 else f'${value:,.0f}'}"
        for chain, value in ceilings.items()
    )


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


def get_gt_sources(chain: str) -> tuple:
    """GeckoTerminal discovery sources for one chain.

    Precedence:
      1. ``GT_SOURCES_<CHAIN>``  — recommended (e.g. GT_SOURCES_ROBINHOOD)
      2. ``<CHAIN>_GT_SOURCES``  — the repo's existing per-chain prefix style
      3. ``GT_SOURCES``          — global default

    Per chain, because the feeds are not equally clean. Measured 2026-09-29:
    Robinhood ``new_pools`` page 1 carries pools 3.2-5.5 min old at a median
    $6,663 mcap (15/20 under $50k), while BSC ``new_pools`` is dominated by
    template-liquidity placeholder pools ($3,504 / $4,381 reserve, zero volume)
    plus the occasional already-$1.9M launch. Running ``new_pools`` only where
    it pays keeps the early lane without importing that noise.
    """
    for key in (f"GT_SOURCES_{chain.upper()}", _env_key(chain, "GT_SOURCES")):
        raw = os.getenv(key, "").strip()
        if raw:
            return tuple(s.strip() for s in raw.split(",") if s.strip())
    # A present-but-blank key must not mean "no GeckoTerminal at all": dotenv
    # yields "" for `GT_SOURCES=`, and the per-chain branch above only falls
    # through when *its* key is blank, so one stray empty line blinded every
    # chain while GT_SOURCES still looked configured. To actually switch the
    # source off, use USE_GECKOTERMINAL=false.
    raw = os.getenv("GT_SOURCES", "") or ""
    if not raw.strip():
        raw = "new_pools,trending,top_volume"
    return tuple(s.strip() for s in raw.split(",") if s.strip())

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


# ── Tradeable venues ─────────────────────────────────────────────────────────
# A router only routes through pools created by its *own* factory, so the bot
# can execute on a DEX only if it holds that DEX's router. Two consequences
# that otherwise show up as alerts with buy buttons that silently fail:
#
#   * a Uniswap V3 router cannot route Aerodrome, Pons, up-v3, Ramses,
#     PancakeSwap-Infinity or any other V3 fork — each needs its own router;
#   * Uniswap V4 pools are not reachable through a V3 router at all. V4 needs
#     the Universal Router + Permit2, which this bot does not implement yet.
#
# Measured on GeckoTerminal page 1, 20 pools per feed, 2026-09-30 — the share of
# pools the configured routers can actually reach:
#
#   chain      feed        router-reachable
#   robinhood  new_pools    0%   (pons-v2 14/20, uniswap-v4 6/20)
#   base       new_pools   25%   (uniswap-v4 8/20, bankr 4, aerodrome 2, o1 1)
#   bsc        new_pools   10%   (uniswap-v4 14/20, four-meme 4)
#   eth        new_pools   10%   (uniswap-v4 18/20)
#
# The earliest feed on every chain is therefore mostly unroutable *today*. The
# default is to keep alerting (the information still has value) but to say so in
# the alert and withhold buy buttons. Set REQUIRE_TRADEABLE_VENUE=true to drop
# these candidates before any enrichment budget is spent on them.
#
# Patterns are matched against the feed's ``dexId`` with ``re.search``, so both
# GeckoTerminal (``uniswap-v3-robinhood``) and DexScreener (``uniswap``) spellings
# work. Deliberately no ``v4`` pattern: matching it would claim a route that
# does not exist.
_TRADEABLE_DEX_PATTERNS = {
    "ethereum": ((r"^uniswap$", r"uniswap[-_]?v?3"), (r"uniswap[-_]?v?2",)),
    "base": ((r"^uniswap$", r"uniswap[-_]?v?3"), (r"uniswap[-_]?v?2",)),
    "bsc": ((r"^pancakeswap$", r"pancakeswap[-_]?v?3"), (r"pancakeswap[-_]?v?2",)),
    "robinhood": ((r"^uniswap$", r"uniswap[-_]?v?3"), (r"uniswap[-_]?v?2",)),
}

# When true, a pool whose DEX the configured routers cannot reach is rejected
# before scoring/enrichment instead of alerting with buy buttons hidden.
REQUIRE_TRADEABLE_VENUE = os.getenv("REQUIRE_TRADEABLE_VENUE", "false").lower() == "true"


def get_tradeable_dex_patterns(chain: str):
    """Override patterns for one chain, via <CHAIN>_TRADEABLE_DEXES or global."""
    raw = (
        os.getenv(_env_key(chain, "TRADEABLE_DEXES"), "").strip()
        or os.getenv("TRADEABLE_DEXES", "").strip()
    )
    if not raw:
        return None
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


# What each venue would need before the bot could execute on it. This exists so
# an alert says "needs the Aerodrome router" rather than a bare "no route" — the
# missing unit is the DEX, not the router *version*. Evidence for that claim,
# measured 2026-09-30 on Base: for a token actively trading on Aerodrome, the
# Uniswap V3 factory returned address(0) for every fee tier (100/500/3000/10000)
# and both quotes (WETH, USDC). A Uniswap V3 router routes only Uniswap V3
# pools; having "a V3 router" says nothing about any other DEX's pools.
_VENUE_REQUIREMENTS = (
    (r"uniswap[-_]?v4", "Uniswap V4 (Universal Router + Permit2)"),
    (r"bankr", "bankr launchpad (V4-based)"),
    (r"o1[-_]?launchpad", "o1 launchpad (V4-based)"),
    (r"^uniswap$|uniswap[-_]?v?3", "Uniswap V3 router (already configured)"),
    (r"uniswap[-_]?v?2", "Uniswap V2 router (already configured)"),
    (r"^pancakeswap$|pancakeswap[-_]?v?3", "PancakeSwap V3 router (already configured)"),
    (r"pancakeswap[-_]?v?2", "PancakeSwap V2 router (already configured)"),
    (r"pancakeswap[-_]?infinity", "PancakeSwap Infinity (CLMM) router"),
    (r"aerodrome", "Aerodrome router (Slipstream for CL pools)"),
    (r"pons", "Pons router (Robinhood V2-style)"),
    (r"ramses", "Ramses router"),
    (r"^up[-_]?v3", "up-v3 router"),
    (r"alandale", "alandale router"),
    (r"four[-_]?meme", "four.meme launchpad contract"),
    (r"sushi", "SushiSwap router"),
)


def venue_requirement(chain: str, dex_id) -> str:
    """Short human label for what trading this venue would take.

    Purely descriptive: it never claims a route exists. Used in alerts and the
    feature row so the gap per chain is a measured list rather than a guess.
    """
    dex = str(dex_id or "").strip().lower()
    if not dex:
        return "unknown venue (feed gave no dexId)"
    for pattern, label in _VENUE_REQUIREMENTS:
        if re.search(pattern, dex):
            return label
    return f"no integration for '{dex}'"


def dex_is_supported(chain: str, dex_id) -> Optional[bool]:
    """Whether the configured routers can actually execute on this pool's DEX.

    Returns ``True`` / ``False`` for a known venue and ``None`` when the feed
    supplied no ``dexId`` — "unknown metadata" must stay distinguishable from
    "known-bad venue", exactly as holder/security provenance is.
    """
    dex = str(dex_id or "").strip().lower()
    if not dex:
        return None

    override = get_tradeable_dex_patterns(chain)
    if override is not None:
        if not any(re.search(p, dex) for p in override):
            return False
        # A user-supplied list does not say which router, so accept it if any
        # path is configured (V3, then V2, then V4 when V4_TRADING is on).
        return bool(get_router_v3(chain) or get_v2_router(chain)
                    or (V4_TRADING and get_universal_router(chain)))

    # V4 pools route through the Universal Router, not the V2/V3 routers — but
    # only once V4_TRADING is on. Until then a v4 label stays unroutable so no
    # buy button is offered for a pool the bot cannot execute (the UPAY case).
    # Note DexScreener labels V4 pools as plain `uniswap` (the v4 badge is in
    # `labels`, which the tokens endpoint does not return), so a bare `uniswap`
    # dexId on a V4-enabled chain is treated as "covered by V3 router or UR".
    if re.search(r"uniswap[-_]?v4", dex):
        return bool(V4_TRADING and get_universal_router(chain))
    if dex == "uniswap" and V4_TRADING and get_universal_router(chain):
        return True

    v3_patterns, v2_patterns = _TRADEABLE_DEX_PATTERNS.get(chain, ((), ()))
    if any(re.search(p, dex) for p in v3_patterns):
        # V3-patterned pools are reached through the V3 router; the V2 router is
        # accepted as a fallback exactly as execute_buy does.
        return bool(get_router_v3(chain) or get_v2_router(chain))
    if any(re.search(p, dex) for p in v2_patterns):
        # A V2-patterned pool needs a V2 router specifically. Robinhood has
        # none, so pons-v2 there is correctly reported unroutable.
        return bool(get_v2_router(chain))
    return False


NATIVE_SYMBOL = {"ethereum": "ETH", "bsc": "BNB", "base": "ETH", "robinhood": "ETH"}

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

# ── Uniswap V4 execution ─────────────────────────────────────────────────────
# V4 pools live in the singleton PoolManager, so the V2/V3 quoter+router path can
# never see them — a V4 pool always failed with "No V3 pool found" even though
# the Uniswap app showed liquidity (measured on UPAY@robinhood, 2026-10-03).
# V4 execution goes through the Universal Router with calldata encoded by the
# `uniswap-universal-router-decoder` package (RouterCodec). OFF by default:
# set V4_TRADING=true once a quote+simulation has been verified for the chain.
V4_TRADING = os.getenv("V4_TRADING", "false").lower() == "true" and V4_CODEC_AVAILABLE

# Canonical deployments (Uniswap docs → v4 deployments). Env overrides win at
# runtime via <CHAIN>_UNIVERSAL_ROUTER / <CHAIN>_V4_POSITION_MANAGER.
_HARD_UNIVERSAL_ROUTERS = {
    "ethereum": "0x66a9893cc07d91d95644aedd05d03f95e1dba8af",
    "base": "0x6ff5693b99212da76ad316178a184ab56d299b43",
    "bsc": "",
    "robinhood": "0x8876789976DEcbFcBbBE364623c63652db8C0904",
}
_HARD_V4_POSITION_MANAGERS = {
    "ethereum": "0x4529A01c7fF8d87a10C4000Bfc73987edcA87d82",
    "base": "0x7c5f5a4bbd8fd63184577525326123b519429bdc",
    "bsc": "",
    "robinhood": "0x58daec3116aae6d93017baaea7749052e8a04fa7",
}

# Native currency sentinel for V4 pool keys (currency0 == address(0) means the
# pool's token0 side is native ETH, not WETH).
V4_NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"

_POSITION_MANAGER_POOLKEYS_ABI = [
    {"inputs": [{"internalType": "bytes25", "name": "poolId", "type": "bytes25"}],
     "name": "poolKeys",
     "outputs": [{"internalType": "address", "name": "currency0", "type": "address"},
                 {"internalType": "address", "name": "currency1", "type": "address"},
                 {"internalType": "uint24", "name": "fee", "type": "uint24"},
                 {"internalType": "int24", "name": "tickSpacing", "type": "int24"},
                 {"internalType": "address", "name": "hooks", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]


def get_universal_router(chain: str) -> str:
    env_key = _env_key(chain, "UNIVERSAL_ROUTER")
    return os.getenv(env_key, "").strip() or _HARD_UNIVERSAL_ROUTERS.get(chain, "")


def get_v4_position_manager(chain: str) -> str:
    env_key = _env_key(chain, "V4_POSITION_MANAGER")
    return os.getenv(env_key, "").strip() or _HARD_V4_POSITION_MANAGERS.get(chain, "")


def v4_pool_id_to_bytes25(pool_id: str) -> bytes:
    """PoolManager pool ids are bytes32; PositionManager.poolKeys takes bytes25."""
    raw = pool_id.strip()
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    return bytes.fromhex(raw[:50].ljust(50, "0"))


async def resolve_v4_pool_key(chain: str, pool_id: str) -> Optional[dict]:
    """PoolKey for a V4 pool id via PositionManager.poolKeys (read-only).

    Returns {currency0, currency1, fee, tickSpacing, hooks} or None. Pure
    on-chain read: no gas, safe to call from the alert path.
    """
    if not WEB3_AVAILABLE:
        return None
    w3 = w3_instances.get(chain)
    posm = get_v4_position_manager(chain)
    if not w3 or not posm or not Web3.is_address(posm):
        logger.warning(f"V4 pool-key resolve skipped for {chain}: no w3/position manager")
        return None
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(posm),
            abi=_POSITION_MANAGER_POOLKEYS_ABI,
        )
        res = await asyncio.to_thread(
            contract.functions.poolKeys(v4_pool_id_to_bytes25(pool_id)).call
        )
        return {
            "currency0": res[0], "currency1": res[1], "fee": int(res[2]),
            "tickSpacing": int(res[3]), "hooks": res[4],
        }
    except Exception as e:
        logger.warning(f"V4 pool-key resolve failed for {chain} {pool_id[:10]}...: {e}")
        return None

SCAN_INTERVAL = max(5, int(_env_float("SCAN_INTERVAL", 30.0)))  # seconds between cycles
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

# ── Re-alert policy ──────────────────────────────────────────────────────────
# OFF by default: a (chain, token) pair is alerted ONCE, for the lifetime of the
# alerts table. Duplicate messages were the complaint this replaced — the old
# policy re-alerted after 4 hours unconditionally, and *within* those 4 hours
# whenever the score had climbed 12 points, and scores bounce by more than that
# between scans (windows roll, holder data arrives late, the bar itself is
# normalised), so one token could ping repeatedly while it stayed in the feeds.
#
# RE_ALERTS=true restores that policy, with both thresholds now tunable instead
# of hard-coded:
#   RE_ALERT_COOLDOWN_HOURS  silent period after an alert (default 4)
#   RE_ALERT_MIN_IMPROVEMENT points the score must gain to beat the cooldown
#                            early (default 12; 0 = never beat it early)
RE_ALERTS_ENABLED = os.getenv("RE_ALERTS", "false").lower() == "true"
RE_ALERT_COOLDOWN_HOURS = _env_float("RE_ALERT_COOLDOWN_HOURS", 4.0)
SCORE_IMPROVEMENT_THRESHOLD = _env_float("RE_ALERT_MIN_IMPROVEMENT", 12.0)
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

# Signal engine state: built whenever signals.py imports; whether it is USED is
# USE_SIGNALS (import-time), re-checked at each gate that depends on it.
SIGNAL_FILTERS = signals.Filters.from_env() if SIGNALS_AVAILABLE else None


def _filters_for_chain(chain: str):
    """SIGNAL_FILTERS plus any ``<CHAIN>_SIG_EARLY_*`` overrides for this chain.

    The early-lane population differs by chain by an order of magnitude (p90
    birth liquidity ~$5.4k on Robinhood vs ~$14.6k on BSC, measured 2026-09-30),
    so one global floor is either spam on one chain or blindness on the other.
    Resolved per call, on top of the mutable ``SIGNAL_FILTERS``, so tests and
    env reloads keep working.
    """
    if SIGNAL_FILTERS is None or not SIGNALS_AVAILABLE:
        return SIGNAL_FILTERS
    return signals.Filters.with_chain_overrides(SIGNAL_FILTERS, chain)
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
logger.info(f"Config file: {ENV_FILE_LOADED}")

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
# (chain, token) pairs this process has already pushed, lowercase. The alerts
# table is the durable dedupe state; this is the fallback for the case where its
# upsert failed (logged, swallowed) — without it a single failed write turns
# into "the same token every cycle" for the rest of the run. Bounded by the
# number of distinct alerts the bot can send, so it never needs pruning.
ALERTED_THIS_RUN: set = set()
# Reasons a candidate never reached the user, counted so "the bot is quiet" is
# diagnosable from /health instead of inferred from a log file.
security_unknown_rejects = 0
unsupported_venue_rejects = 0
untradeable_alerts = 0
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
    # Venue: which DEX the pool is on, and whether the configured routers can
    # actually reach it. Alerting on an unroutable pool (Uniswap V4, a V3 fork,
    # Pons/Aerodrome/...) produced buy buttons that could only fail; this is the
    # column that makes that measurable instead of anecdotal.
    "dex_id", "venue_tradeable", "venue_requirement",
    "first_sight_mcap", "first_sight_age_minutes", "first_sight_ts",
    "volume_surge", "vol_accel", "vol_observations", "seed_only",
    "signal_bonus", "signal_penalty", "signal_notes",
    # Per-component score breakdown. The total alone cannot answer "which part of
    # the model withheld the points", which is the question every miss raises.
    # Stored for every evaluation that gets far enough to be scored.
    "score_vol_liq", "score_vol_5m_1h", "score_vol_1h_6h", "score_vol_6h_24h",
    "score_buy_5m", "score_buy_1h", "score_price",
    "score_holder", "score_security", "score_cex",
    "penalties_total",
    # Theoretical ceiling this chain/age could reach, i.e. the number the alert
    # threshold is scaled against. `hand_score / ceiling_score` is the honest
    # "how close was it" ratio; hand_score / alert_threshold is not, because the
    # threshold is itself a fraction of the ceiling.
    "ceiling_score",
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
    "venue_tradeable", "volume_surge", "vol_observations", "seed_only",
    "rejected", "passed_threshold", "alert_sent", "paper_mode", "early_runner",
}
_FEATURE_TEXT_FIELDS = {
    "ts_utc", "chain", "token_address", "pair_address", "symbol", "source",
    "security_source", "signal_notes", "reject_reasons", "holder_source",
    "dex_id", "venue_requirement",
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
                try:
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
                    # One malformed row or one locked commit must not kill the
                    # writer for the rest of the run: the thread never restarts,
                    # the 20k queue then fills, and every later training row is
                    # dropped with nothing but a counter to show for it.
                    self.dropped += 1
                    logger.error(f"Feature write failed (writer continues): {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
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

def _migrate_alerts_dedupe_key(conn: sqlite3.Connection):
    """Make (chain, token_address) a key that actually dedupes.

    Two failure modes produced "the same token alerted me again":

    1. **Case.** The key was compared byte-for-byte while feeds hand over the
       same address checksummed and lowercase. Two spellings, two rows, and the
       second alert behaved as if the first had never happened.
    2. **No unique constraint.** ``db_record_alert`` upserts with
       ``ON CONFLICT(chain, token_address)``. On a table created without that
       UNIQUE clause the statement is rejected, the rejection is caught and
       logged as a warning, and *no dedupe state is ever written* — so the same
       token re-alerted every single cycle while looking like a config problem.

    Lower-case everything, collapse to the newest row per key, then make sure
    the unique index exists. All steps are cheap and idempotent.

    **Order matters.** The collapse must group on ``LOWER(...)`` *before* the
    lower-casing UPDATE: on a table that already has ``UNIQUE(chain,
    token_address)`` — which every schema here has — the UPDATE would collide
    with its own twin ('0xAbC' vs '0xabc' are distinct under BINARY collation),
    abort the statement, and leave both rows exactly as they were. Each step
    therefore runs in its own try: a failure in one must not silently skip the
    others.
    """
    try:
        conn.execute(
            "DELETE FROM alerts WHERE id NOT IN ("
            "SELECT MAX(id) FROM alerts GROUP BY LOWER(chain), LOWER(token_address))"
        )
        conn.execute(
            "UPDATE alerts SET chain = LOWER(chain), token_address = LOWER(token_address)"
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"alerts case-collapse failed: {e}")
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_chain_token "
            "ON alerts(chain, token_address)"
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"alerts dedupe index failed: {e}")
    _migrate_address_case(conn)


def _migrate_address_case(conn: sqlite3.Connection):
    """Lower-case stored token addresses in the tables keyed by them.

    ``evaluate_token`` now lower-cases on write, so without this an existing
    deployment keeps two identities for one contract: duplicate watchlist rows
    (the table's primary key is exact-match, so the same token would be
    re-checked twice and its ``evals``/``best_hand`` split), and feature rows
    that no longer join to each other — which breaks the per-token forward-price
    series ``label_outcomes.py`` builds. The ``alerts`` table is handled by
    ``_migrate_alerts_dedupe_key`` (it needs the collapse first).

    ``features`` has no unique constraint on (chain, token_address), so a plain
    UPDATE cannot collide and cannot drop rows — important, because those rows
    *are* the training series and deleting duplicates would delete history.
    """
    try:
        conn.execute(
            "UPDATE features SET chain = LOWER(chain), token_address = LOWER(token_address)"
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"features address-case migration failed: {e}")
    try:
        # watchlist has a composite primary key, so collapse before lowering
        # (same collision rule as alerts), keeping the most recently written row.
        conn.execute(
            "DELETE FROM watchlist WHERE rowid NOT IN ("
            "SELECT MAX(rowid) FROM watchlist GROUP BY LOWER(chain), LOWER(token_address))"
        )
        conn.execute(
            "UPDATE watchlist SET chain = LOWER(chain), token_address = LOWER(token_address)"
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"watchlist address-case migration failed: {e}")


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
        CREATE TABLE IF NOT EXISTS watchlist (
            chain TEXT NOT NULL,
            token_address TEXT NOT NULL,
            pair_address TEXT,
            symbol TEXT,
            source TEXT,
            first_seen REAL,
            last_seen REAL,
            last_checked REAL,
            evals INTEGER DEFAULT 0,
            best_hand REAL,
            last_hand REAL,
            last_price REAL,
            PRIMARY KEY (chain, token_address)
        );
    """)
    conn.executescript(_feature_schema_sql())
    ensure_feature_columns(conn)  # migrate older feature tables
    conn.executescript(
        "CREATE INDEX IF NOT EXISTS idx_features_token ON features(chain, token_address);"
        "CREATE INDEX IF NOT EXISTS idx_features_ts ON features(ts_epoch);"
    )
    _migrate_alerts_dedupe_key(conn)
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
    # Normalised on both sides: the dedupe key is (chain, token) and an address
    # that arrives checksummed from one feed and lowercase from another is the
    # SAME token — keying on the raw string silently created two rows and the
    # second one alerted as if the first had never happened.
    cur = db_conn.execute(
        "SELECT total_score, alert_time FROM alerts "
        "WHERE chain = ? AND token_address = ? COLLATE NOCASE",
        (str(chain).lower(), str(token_address).lower())
    )
    row = cur.fetchone()
    return {"total_score": row[0], "alert_time": row[1]} if row else None

def db_record_alert(chain, token_address, symbol, score):
    try:
        db_conn.execute(
            "INSERT INTO alerts (chain, token_address, symbol, total_score, alert_time) "
            "VALUES (?,?,?,?,?) ON CONFLICT(chain, token_address) DO UPDATE SET "
            "total_score=excluded.total_score, alert_time=excluded.alert_time, symbol=excluded.symbol",
            (str(chain).lower(), str(token_address).lower(), symbol, score, time.time())
        )
        db_conn.commit()
        return True
    except Exception as e:
        logger.warning(f"DB alert upsert failed: {e}")
        return False


# ── watchlist persistence ────────────────────────────────────────────────────

def db_watchlist_remember(chain, token_address, pair_address, symbol, source,
                          hand_score, price):
    """Remember a pool the scanner has seen, and how good it looked.

    `hand_score` is None for candidates rejected before scoring; those are kept
    (so a later re-check can still be compared) but they will not qualify for
    re-checking on their own unless they turn out to be promoted by some other
    path. The `best_hand` column is a high-water mark.
    """
    if not WATCHLIST_ENABLED:
        return
    try:
        now = time.time()
        db_conn.execute(
            "INSERT INTO watchlist (chain, token_address, pair_address, symbol, source, "
            "first_seen, last_seen, evals, best_hand, last_hand, last_price) "
            "VALUES (?,?,?,?,?,?,?,1,?,?,?) "
            "ON CONFLICT(chain, token_address) DO UPDATE SET "
            "pair_address=excluded.pair_address, symbol=excluded.symbol, "
            "source=excluded.source, last_seen=excluded.last_seen, "
            "evals=watchlist.evals + 1, "
            "best_hand=MAX(COALESCE(watchlist.best_hand, 0), COALESCE(excluded.best_hand, 0)), "
            "last_hand=COALESCE(excluded.last_hand, watchlist.last_hand), "
            "last_price=COALESCE(excluded.last_price, watchlist.last_price)",
            (chain, token_address, pair_address, symbol, source, now, now,
             hand_score, hand_score, price),
        )
        db_conn.commit()
    except Exception as e:
        logger.warning(f"DB watchlist upsert failed: {e}")


def db_watchlist_due(now=None, limit=None):
    """Pools worth re-pricing: alive, not checked recently, and once promising."""
    now = time.time() if now is None else now
    limit = WATCHLIST_MAX if limit is None else limit
    if not WATCHLIST_ENABLED or limit <= 0:
        return []
    cutoff_checked = now - WATCHLIST_RECHECK_MINUTES * 60
    cutoff_seen = now - WATCHLIST_TTL_HOURS * 3600
    try:
        cur = db_conn.execute(
            "SELECT chain, token_address, pair_address, symbol, source, best_hand, last_price "
            "FROM watchlist WHERE last_seen >= ? "
            "AND (last_checked IS NULL OR last_checked <= ?) "
            "AND COALESCE(best_hand, 0) >= ? "
            "ORDER BY COALESCE(best_hand, 0) DESC, last_seen DESC LIMIT ?",
            (cutoff_seen, cutoff_checked, WATCHLIST_MIN_BEST_SCORE, limit),
        )
        return [
            {"chain": r[0], "token_address": r[1], "pair_address": r[2], "symbol": r[3],
             "source": r[4], "best_hand": r[5], "last_price": r[6]}
            for r in cur.fetchall()
        ]
    except Exception as e:
        logger.warning(f"DB watchlist query failed: {e}")
        return []


def db_watchlist_mark_checked(chain, token_address, now=None):
    now = time.time() if now is None else now
    try:
        db_conn.execute(
            "UPDATE watchlist SET last_checked=? WHERE chain=? AND token_address=?",
            (now, chain, token_address),
        )
        db_conn.commit()
    except Exception as e:
        logger.warning(f"DB watchlist mark failed: {e}")


def db_watchlist_prune(now=None):
    """Forget pools that have not been seen for a TTL, so the table cannot grow."""
    if not WATCHLIST_ENABLED:
        return 0
    now = time.time() if now is None else now
    try:
        cur = db_conn.execute("DELETE FROM watchlist WHERE last_seen < ?",
                              (now - WATCHLIST_TTL_HOURS * 3600,))
        db_conn.commit()
        return cur.rowcount
    except Exception as e:
        logger.warning(f"DB watchlist prune failed: {e}")
        return 0


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

# ── First-sight latency instrumentation ──────────────────────────────────────
# "It only pings me after the pump" is a question about *detection* latency, not
# scoring, and the two are invisible to each other in a log full of reject
# reasons. This records the first moment the scanner ever sees a token and logs
# its age and mcap right then, so the log itself answers whether a runner was
# seen at $6k on minute 3 (early, and the gate is the problem) or at $3M on hour
# 5 (discovery is the problem). Deliberately logged *before* the floors so a
# first sighting is recorded even when the token is instantly rejected.
_first_sight: Dict[str, dict] = {}
FIRST_SIGHT_TTL = 6 * 3600


def _first_sight_note(chain, token, symbol, pair, *, liquidity, market_cap, age_minutes):
    """Log a token the first time the scanner sees it, with age and mcap."""
    key = f"{chain}:{str(token).lower()}"
    now = time.time()
    existing = _first_sight.get(key)
    if existing:
        return existing
    v5 = float((pair.get("volume") or {}).get("m5") or 0)
    src = str(pair.get("source") or "?")
    record = {
        "ts": now, "mcap": market_cap, "liq": liquidity,
        "age_minutes": age_minutes, "v5": v5, "source": src,
    }
    _first_sight[key] = record
    age_txt = f"{age_minutes:.1f}m" if age_minutes is not None else "?"
    logger.info(
        f"FIRST SIGHT {symbol}@{chain} age={age_txt} mcap=${market_cap:,.0f} "
        f"liq=${liquidity:,.0f} v5=${v5:,.0f} [{src}]"
    )
    return record


def prune_caches():
    now = time.time()
    # TTLs must match the READ side (SECURITY_TTL, TICKER_TTL, HOLDER_TTL) or
    # the prune keeps entries the lookups already ignore — pure memory waste
    # that a long run only reveals as a slowly growing dict.
    for cache, ttl in [(security_cache, SECURITY_TTL), (coingecko_ticker_cache, TICKER_TTL)]:
        stale = [k for k, (_, ts) in cache.items() if now - ts > ttl]
        for k in stale:
            del cache[k]
    stale = [k for k, (_, ts) in holder_cache.items() if now - ts > HOLDER_TTL]
    for k in stale:
        del holder_cache[k]
    stale = [k for k, rec in _first_sight.items()
             if now - rec.get("ts", 0) > FIRST_SIGHT_TTL]
    for k in stale:
        del _first_sight[k]
    if PAIR_HISTORY is not None:
        PAIR_HISTORY.prune(now)


# ═══════════════════════════════════════════════════════════════════════════════
# IGNITION WATCHLIST — re-price pools that dropped out of the discovery feeds
# ═══════════════════════════════════════════════════════════════════════════════

_watchlist_written_this_cycle: set = set()


def watchlist_cycle_start():
    """Clear the per-cycle write buffer. Called once at the top of each scan."""
    _watchlist_written_this_cycle.clear()


def _watchlist_note(chain, token, pair_address, symbol, source, hand_score, price):
    """Record an evaluated pool, at most once per cycle per token.

    Writing on every evaluation would mean one SQLite commit per candidate per
    cycle for no extra information; one row per cycle is also a cleaner meaning
    for the `evals` counter.
    """
    if not WATCHLIST_ENABLED:
        return
    key = (chain, token)
    if key in _watchlist_written_this_cycle:
        return
    _watchlist_written_this_cycle.add(key)
    db_watchlist_remember(chain, token, pair_address, symbol, source, hand_score, price)


async def get_watchlist_pair(session, chain: str, pair_address: str):
    """Re-price one watched pool via DexScreener's single-pair endpoint.

    Deliberately not GeckoTerminal: that budget is shared with discovery and
    holder lookups (~28 calls/min on the free tier), and spending it on
    re-checks would slow the lane that finds new pools. DexScreener's
    `latest/dex/pairs/{chain}/{pair}` returns the same pair shape
    `evaluate_token` consumes.
    """
    if not pair_address:
        return None
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
    data = await fetch_json(session, url)
    pairs = (data or {}).get("pairs") or []
    for pair in pairs:
        if pair.get("pairAddress", "").lower() == pair_address.lower():
            pair["source"] = "watchlist:dexscreener"
            return pair
    return None


def batches(seq, size):
    """Split a list into chunks of at most ``size`` (DexScreener /tokens/v1 cap 30)."""
    size = max(1, int(size))
    return [seq[i:i + size] for i in range(0, len(seq), size)]


async def collect_watchlist_pairs(session) -> list:
    """Re-price the standing universe, batched, newest signal first.

    This is the lane that makes an abnormal-volume strategy possible at all.
    Measured 2026-09-30: GeckoTerminal's list endpoints are 24h rankings, and
    **0 of 20 pools on any of them** (`top_volume`, `trending`, `trending?duration=5m`,
    `h24_tx_count`, deep pages included) had concentrated 10% of their daily volume
    into the last 5 minutes. There is no short-window ranking to subscribe to —
    `sort=h1_volume_usd_desc` returns HTTP 400 and a network trades feed 404s — so a
    pool mid-pump is simply not on any list. The only way to see the spike is to be
    already watching the pool and comparing it to its own baseline.

    Batching is what makes that affordable: ``/tokens/v1/{chain}/{a,b,c...}`` takes
    30 token addresses per call, so a 300-pool universe costs ~10 calls instead of
    300. Every entry is marked checked before its fetch so a provider outage cannot
    make the same pool retry on every cycle.
    """
    due = db_watchlist_due()
    if not due:
        return []
    logger.info(f"watchlist: {len(due)} pool(s) due for a re-check")
    now = time.time()
    for entry in due:
        db_watchlist_mark_checked(entry["chain"], entry["token_address"], now)

    by_chain: Dict[str, list] = {}
    for entry in due:
        by_chain.setdefault(entry["chain"], []).append(entry)

    async def one_chain(chain: str, entries: list):
        out = []
        for batch in batches(entries, 30):
            addresses = [e["token_address"] for e in batch if e.get("token_address")]
            if not addresses:
                continue
            url = f"https://api.dexscreener.com/tokens/v1/{chain}/{','.join(addresses)}"
            try:
                data = await fetch_json(session, url)
            except Exception as e:  # noqa: BLE001 - one bad batch must not stop the lane
                logger.warning(f"watchlist batch failed for {chain}: {e}")
                continue
            pairs = data if isinstance(data, list) else []
            # One pair per token: the deepest pool, matching dedupe_best_pool, so a
            # token with several pools is judged on the one that is tradeable.
            best: Dict[str, dict] = {}
            for pair in pairs:
                if pair.get("chainId") != chain:
                    continue
                # Key by token so several pools for one token collapse to the
                # deepest. Fall back to the pair address when the feed omits it,
                # rather than silently dropping the row.
                token = str((pair.get("baseToken") or {}).get("address") or "").lower()
                if not token:
                    token = str(pair.get("pairAddress") or "").lower()
                if not token:
                    continue
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                cur = best.get(token)
                if cur is None or liq > float((cur.get("liquidity") or {}).get("usd") or 0):
                    pair = dict(pair)
                    pair["source"] = "watchlist:dexscreener"
                    best[token] = pair
            out.extend(best.values())
        return out

    results = await asyncio.gather(
        *[one_chain(ch, entries) for ch, entries in by_chain.items()],
        return_exceptions=True,
    )
    flat = []
    for res in results:
        if isinstance(res, list):
            flat.extend(res)
    return flat

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
# The discovery client is built with min_interval_s=0.0 (the limiter below is
# what paces it), and the token-info calls for holder concentration go through
# fetch_json directly, so without a *shared* limiter the
# two paths would each believe they owned the whole budget and collectively blow
# it. One process-wide limiter therefore covers both.
GT_MIN_INTERVAL = _env_float("GT_MIN_INTERVAL_S", 2.1)  # ~28 calls/min
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


# Legacy env spellings kept as aliases so existing .env files keep working
# (GT_PAGES_NEW -> new_pools, GT_PAGES_TOP -> top_volume, and the TTL trio).
_GT_PAGES_LEGACY = {
    "new_pools": "GT_PAGES_NEW",
    "trending": "GT_PAGES_TRENDING",
    "top_volume": "GT_PAGES_TOP",
}
_GT_TTL_LEGACY = {
    "new_pools": "GT_LIST_TTL_NEW",
    "trending": "GT_LIST_TTL_TRENDING",
    "top_volume": "GT_LIST_TTL_TOP",
}
_GT_PAGE_DEFAULTS = {"new_pools": 1, "trending": 2, "top_volume": 1}


def _gt_env_value(kind: str, prefix: str, legacy: Dict[str, str], default: float) -> float:
    """First non-blank of ``<PREFIX>_<KIND>`` then the legacy name, else default.

    A blank value counts as unset — otherwise a `GT_PAGES_TOP=` line silently
    turns into a parse of "" and, worse, hides that the default is in force.
    """
    for name in (f"{prefix}_{kind.upper()}", legacy.get(kind, "")):
        if not name:
            continue
        raw = os.getenv(name, "")
        if raw is not None and str(raw).strip():
            return _env_float(name, default)
    return float(default)


def _gt_source_pages() -> Dict[str, int]:
    """Pages per source for EVERY source the feeds define.

    Only three literal names used to be bound (bot.py's old inline dict), so
    the sources the shipped profile actually runs — `trending_5m` and
    `top_txns` — were hard-capped at one page while `GT_PAGES_TRENDING` and
    `GT_PAGES_NEW` sat in `.env` doing nothing for them. Depth is now tunable
    for every kind, and a typo still falls back to the default rather than
    raising mid-cycle.
    """
    return {
        kind: max(1, int(_gt_env_value(
            kind, "GT_PAGES", _GT_PAGES_LEGACY, _GT_PAGE_DEFAULTS.get(kind, 1)
        )))
        for kind in discovery.GT_ENDPOINTS
    }


def _gt_source_ttls() -> Dict[str, float]:
    """List-cache TTL (s) per source; defaults come from discovery.DEFAULT_LIST_TTLS."""
    return {
        kind: max(5.0, _gt_env_value(
            kind, "GT_LIST_TTL", _GT_TTL_LEGACY,
            discovery.DEFAULT_LIST_TTLS.get(kind, 90.0)
        ))
        for kind in discovery.GT_ENDPOINTS
    }


async def get_geckoterminal_pairs(session, network):
    """Candidate discovery via GeckoTerminal (see discovery.py).

    Replaces the DexScreener `latest/dex/pairs/{chain}` call, which 404s and
    silently left the scanner with only paid boost/profile tokens.

    Depth is tunable per deployment and per source: ``GT_PAGES_<KIND>`` (e.g.
    ``GT_PAGES_TRENDING_5M``) and ``GT_LIST_TTL_<KIND>``, with the legacy
    ``GT_PAGES_NEW`` / ``GT_PAGES_TOP`` / ``GT_LIST_TTL_*`` spellings still
    accepted. Page 1 of new_pools is the newest birth cohort — that freshness is
    the whole point of the feed, so depth there buys less than it costs; deeper
    pages of the *rankings* buy older, larger pools instead of earlier ones, so
    raise them only with the mcap ceiling in mind. All of it shares the
    GT_MIN_INTERVAL_S budget with holder lookups.
    """
    global _gt_client
    sources = get_gt_sources(network)
    source_pages = _gt_source_pages()
    list_ttls = _gt_source_ttls()
    fetcher = lambda url: gt_fetch_json(session, url)  # noqa: E731
    if _gt_client is None:
        # min_interval_s=0: gt_fetch_json already applies the shared budget, and
        # stacking a second 2.1s wait would halve throughput for no benefit.
        _gt_client = discovery.GeckoTerminal(
            fetcher, min_interval_s=0.0, list_ttls=list_ttls,
            max_pages=1, source_pages=source_pages,
        )
    else:
        _gt_client.fetch = fetcher
        _gt_client.list_ttls = dict(list_ttls)
        _gt_client.source_pages = dict(source_pages)
    pairs = await _gt_client.candidates(network, kinds=sources)
    pairs.sort(key=lambda x: float((x.get("volume") or {}).get("m5", 0) or 0), reverse=True)
    depth_txt = ",".join(f"{k}:{source_pages.get(k, 1)}" for k in sources)
    logger.info(f"{network}: {len(pairs)} GeckoTerminal candidates ({','.join(sources)}) [{depth_txt}p]")
    return pairs[:300]


async def get_all_pairs(session, network):
    """Candidates for one chain — the UNION of GeckoTerminal and DexScreener.

    Why both, measured 2026-09-30 rather than assumed:

    * GeckoTerminal ``new_pools`` lists a pool **3-5 minutes** old. It is the only
      source that early, and it is correspondingly noisy — on the birth feed most
      pools are Uniswap V4, which the routers cannot reach.
    * DexScreener's boost/profile lists surface tokens that are **median ~24h old**
      (p25 ~112m, min 26m in a 21-token sample). That is much later than birth, but
      the list is tiny (~20 tokens across all four chains per cycle instead of
      hundreds), its tokens are mostly small (median mcap ~$107k, 13/21 inside the
      <200k entry window) and most sit on ``uniswap`` / ``pancakeswap`` — i.e. on
      venues the routers CAN trade.

    So they are complements, not substitutes: GeckoTerminal for latency,
    DexScreener for a low-volume, low-competition, mostly-tradeable shortlist. This
    used to be an either/or switch (``USE_GECKOTERMINAL``), which meant turning on
    the early feed silently turned off the curated one.

    Enable with ``DEXSCREENER_SOURCES=boosts,boosts_top,profiles`` (empty
    disables it; unknown names are logged and skipped).
    """
    pairs: list = []
    seen: set = set()

    async def add_pair(pair):
        addr = pair.get("pairAddress")
        if not addr or addr in seen:
            return
        if pair.get("chainId") != network:
            return
        seen.add(addr)
        pairs.append(pair)

    if USE_GECKOTERMINAL:
        for pair in await get_geckoterminal_pairs(session, network):
            if SEED_ONLY_SOURCES:
                kind = str(pair.get("source") or "").split(":", 1)[-1]
                if kind in SEED_ONLY_SOURCES:
                    pair = dict(pair)
                    pair["seed_only"] = True
            await add_pair(pair)

    if DEXSCREENER_SOURCES:
        for pair in await get_dexscreener_pairs(session, network):
            await add_pair(pair)

    # Neither source enabled. This is the LAST-RESORT path, and the endpoint it
    # calls is the one discovery.py's docstring documents as HTTP 404 — so with
    # both sources off the honest outcome is "no candidates", not "the paid list".
    # Kept only so a deployment that deliberately disabled both sees the attempt
    # in the logs rather than an empty cycle with no explanation.
    if not USE_GECKOTERMINAL and not DEXSCREENER_SOURCES:
        data = await fetch_json(
            session,
            f"https://api.dexscreener.com/latest/dex/pairs/{network}?page=0&pageSize=300",
        )
        if data and "pairs" in data:
            for pair in data["pairs"]:
                await add_pair(pair)

    pairs.sort(key=lambda x: float((x.get("volume") or {}).get("m5", 0) or 0), reverse=True)
    # One row per TOKEN, not per pool. The two sources are deduped separately —
    # GeckoTerminal inside candidates(), DexScreener not at all, since
    # /tokens/v1 answers with every pool the token has — so their union could
    # hand the same contract to evaluate_token twice in one cycle and produce
    # two alerts seconds apart. Deepest pool wins, which is also the tradeable
    # one.
    pairs = discovery.dedupe_best_pool(pairs)
    return pairs[:300]


async def get_dexscreener_pairs(session, network):
    """Boosted/profiled tokens for one chain, resolved to pairs.

    ``/tokens/v1/{chain}/{a,b,c}`` accepts up to 30 addresses, so the whole list
    costs one call instead of one per token — which matters because this runs
    every scan cycle for every chain.
    """
    addresses = []
    seen = set()
    for kind in DEXSCREENER_SOURCES:
        url = DEXSCREENER_ENDPOINTS.get(kind)
        if not url:
            logger.warning(f"Unknown DEXSCREENER_SOURCES entry {kind!r}")
            continue
        payload = await fetch_json(session, url)
        for addr in discovery.dexscreener_token_addresses(payload, network):
            if addr not in seen:
                seen.add(addr)
                addresses.append(addr)

    if not addresses:
        return []

    out = []
    for batch in discovery.chunked(addresses, 30):
        url = f"https://api.dexscreener.com/tokens/v1/{network}/{','.join(batch)}"
        data = await fetch_json(session, url)
        for pair in (data if isinstance(data, list) else []):
            if pair.get("chainId") != network:
                continue
            pair = dict(pair)
            pair["source"] = "dexscreener:boost"
            out.append(pair)
    logger.info(f"{network}: {len(addresses)} boosted/profiled token(s) -> {len(out)} pair(s)")
    return out


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
        if now - ts < TICKER_TTL:
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
    concentration were zero — ``max_possible_score`` compensates by lowering the
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
    * **Age gates** cap the long-window branches: a 1-hour-old pool can only
      reach the 0.3x partial tier of the 1h/6h and 6h/24h branches (see
      ``score_1h_6h`` / ``score_6h_24h``), never the full points.

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
    """Whether CEX-listing scoring can ever fire for this chain.

    Both preconditions, not one: no CoinGecko coverage on Robinhood, and no
    data without an API key — ``get_cex_listings`` returns zeros when the key
    is absent (``.env.example`` ships it empty), so counting the 5 CEX points
    anyway set the scaled bar ~3 points above what a candidate could reach on
    eth/bsc/base.
    """
    platform = CHAIN_TO_COINGECKO_PLATFORM.get(chain)
    return bool(platform and platform != "robinhood" and COINGECKO_API_KEY)


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
    floor = min(base, _env_float("MIN_EFFECTIVE_SCORE", 35.0))
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
    global tokens_evaluated, security_unknown_rejects, unsupported_venue_rejects
    chain = pair.get("chainId")
    base_token = pair.get("baseToken", {}) or {}
    # Lower-cased at the source: every consumer below keys on this address
    # (alerts dedupe, watchlist PK, feature-row joins) and feeds disagree about
    # checksum casing, so the same contract must not become two identities.
    token = str(base_token.get("address") or "").lower()
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
        _watchlist_note(chain, token, pair_id, symbol,
                        str(pair.get("source") or ""), feat.get("hand_score"),
                        feat.get("price_usd"))
        return result

    def reject(reason: str):
        return finish(reason)

    # Per-chain floors so one noisy chain (e.g. Base) can be tightened without
    # changing the others: BASE_MIN_LIQUIDITY_USD, BASE_MIN_VOL_5M_USD, ...
    default_liq = ROBINHOOD_MIN_LIQUIDITY_USD if chain == "robinhood" else MIN_LIQUIDITY_USD
    min_liq = _chain_floor(chain, "MIN_LIQUIDITY_USD", default_liq)
    min_vol_5m = _chain_floor(chain, "MIN_VOL_5M_USD", MIN_VOL_5M_USD)
    min_mcap = _chain_floor(chain, "MIN_MARKET_CAP_USD", MIN_MARKET_CAP_USD)
    # Alert ceiling — the "too late to be worth entering" cut. A floor alone
    # let the scanner alert on $3M and $22M tokens that had already run; an
    # early-entry bot needs the other bound too. 0 (the default) disables it,
    # so behaviour is unchanged unless MAX_MARKET_CAP_USD is set explicitly.
    max_mcap = _chain_floor(
        chain, "MAX_MARKET_CAP_USD", _env_float("MAX_MARKET_CAP_USD", 0.0)
    )

    # ── Raw metrics are read *before* the floors ────────────────────────────
    # The floors below return early, and a return used to leave the row with
    # zeros for every window, ratio and transaction count — because the block
    # that fills them sat after the checks. A token rejected for a thin 5-minute
    # read was then indistinguishable from one that never traded, which poisons
    # any model trained on the table (the losers are the class you most need).
    liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
    price = float(pair.get("priceUsd") or 0)
    market_cap = float(pair.get("marketCap") or 0)
    vol_5m = float(pair.get("volume", {}).get("m5") or 0)
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

    # Which DEX this pool is on, and whether our routers can reach it. Recorded
    # for every evaluation (including rejects) so "how many of the pools we see
    # are actually tradeable" is a query, not a guess.
    dex_id = str(pair.get("dexId") or "").strip().lower()
    venue_tradeable = dex_is_supported(chain, dex_id)
    venue_needs = venue_requirement(chain, dex_id)

    feat.update({
        "liquidity_usd": liquidity,
        "market_cap_usd": market_cap,
        "age_minutes": age_minutes,
        "dex_id": dex_id,
        "venue_tradeable": None if venue_tradeable is None else int(venue_tradeable),
        "venue_requirement": venue_needs,
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
        # Provisional ceiling and bar: `has_top100=False` because the holder
        # lookup has not run yet. Both are overwritten with the authoritative
        # values next to `effective_threshold` when the evaluation gets that far.
        # Recorded here so a token rejected early still shows the bar it faced —
        # otherwise "48 against a bar of 64" is unanswerable for exactly the
        # rows you most want to study.
        "ceiling_score": max_possible_score(
            chain, age_minutes, has_top100=False, has_cex=has_cex_data(chain)
        ),
        "alert_threshold": effective_threshold(
            chain, age_minutes, has_top100=False, has_cex=has_cex_data(chain)
        ),
    })

    # Detection-latency probe: record and log the first time this token is ever
    # seen, before any floor can reject it. See _first_sight_note.
    first_sight = _first_sight_note(chain, token, symbol, pair,
                                    liquidity=liquidity, market_cap=market_cap,
                                    age_minutes=age_minutes)
    feat.update({
        "first_sight_mcap": (first_sight or {}).get("mcap"),
        "first_sight_age_minutes": (first_sight or {}).get("age_minutes"),
        "first_sight_ts": (first_sight or {}).get("ts"),
    })

    # ── Seed-only: record a baseline, never score, never alert ───────────────
    # Placed before the floors so the baseline exists even for a pool that would
    # be rejected today: the whole point is to be watching it before its volume
    # moves, and a pool that fails a floor now may pass it later.
    if pair.get("seed_only"):
        if USE_SIGNALS and PAIR_HISTORY is not None:
            PAIR_HISTORY.observe(pair, security=None, score=0.0)
        feat["seed_only"] = 1
        logger.debug(f"seed-only {symbol}@{chain}: baseline recorded, not scored")
        return finish("seed_only")

    # ── Floors (same order as before: first failure still wins the label) ────
    if liquidity < min_liq: return reject("liquidity")
    if price < MIN_PRICE: return reject("price_too_low")
    if market_cap < min_mcap: return reject("market_cap")
    # The ceiling is applied to BOTH size numbers. `market_cap` is the
    # circulating figure the alert prints; `fdv` is what the charts show and
    # the honest answer to "has this already run". Feeds disagree about which
    # one they bother to publish — GeckoTerminal substitutes fdv only when the
    # circulating cap is missing, DexScreener publishes both — so a token
    # quoting a small circulating cap next to a multi-million FDV used to clear
    # the gate and alert as "in the millions". signals.py already rejects
    # dilution (fdv/mcap > 8 with fdv > 250k); this closes the same gap for the
    # size bound itself. Unknown fdv (0) never triggers it.
    if max_mcap and market_cap > max_mcap: return reject("mcap_too_high")
    fdv_value = float(pair.get("fdv") or 0)
    if max_mcap and fdv_value > max_mcap: return reject("fdv_too_high")
    if vol_5m < min_vol_5m: return reject("vol_5m")
    if chain == "robinhood" and age_minutes is not None and age_minutes < ROBINHOOD_MIN_PAIR_AGE_MIN:
        return reject("robinhood_too_new")

    # ── Venue gate (opt-in) ──────────────────────────────────────────────────
    # An unroutable pool cannot be bought, so scoring it to completion burns the
    # shared GeckoTerminal/GoPlus budget on a guaranteed failure. Default is to
    # keep evaluating and let the alert say so (see send_alert); this gate is for
    # once you have confirmed the venue split on your chains.
    if REQUIRE_TRADEABLE_VENUE and venue_tradeable is False:
        unsupported_venue_rejects += 1
        logger.debug(f"Unsupported venue {symbol}@{chain}: dex={dex_id or '?'}")
        return reject("unsupported_venue")

    score = 0; penalties = 0
    s_vol_liq = score_volume_liquidity(vol_5m, liquidity)
    s_5m_1h = score_5m_1h(vol_5m, vol_1h, age_minutes)
    s_1h_6h = score_1h_6h(vol_1h, vol_6h, age_minutes)
    s_6h_24h = score_6h_24h(vol_6h, vol_24h, age_minutes)
    score += s_vol_liq + s_5m_1h + s_1h_6h + s_6h_24h
    feat.update({
        "score_vol_liq": s_vol_liq, "score_vol_5m_1h": s_5m_1h,
        "score_vol_1h_6h": s_1h_6h, "score_vol_6h_24h": s_6h_24h,
    })

    s_buy_5m = 0.0
    total_5m = buys_5m + sells_5m
    if total_5m > 0:
        buy_ratio_5m = buys_5m / total_5m
        if buy_ratio_5m >= 0.85: s_buy_5m = BUY_PRESSURE_5M_PTS
        elif buy_ratio_5m >= 0.70: s_buy_5m = BUY_PRESSURE_5M_PTS * 0.75
        elif buy_ratio_5m >= 0.55: s_buy_5m = BUY_PRESSURE_5M_PTS * 0.5
        elif buy_ratio_5m >= 0.45: s_buy_5m = BUY_PRESSURE_5M_PTS * 0.25
        score += s_buy_5m
        if buy_ratio_5m < 0.35: penalties += PENALTY_SELL_PRESSURE_5M
        if total_5m < 5: penalties += PENALTY_LOW_TX_5M
    else:
        penalties += PENALTY_LOW_TX_5M

    s_buy_1h = 0.0
    total_1h = buys_1h + sells_1h
    if age_minutes is not None and age_minutes > 60 and total_1h > 0:
        buy_ratio_1h = buys_1h / total_1h
        if buy_ratio_1h >= 0.80: s_buy_1h = BUY_PRESSURE_1H_PTS
        elif buy_ratio_1h >= 0.65: s_buy_1h = BUY_PRESSURE_1H_PTS * 0.75
        elif buy_ratio_1h >= 0.50: s_buy_1h = BUY_PRESSURE_1H_PTS * 0.5
        score += s_buy_1h
        if buy_ratio_1h < 0.35: penalties += PENALTY_SELL_PRESSURE_1H
        if total_1h < 10: penalties += PENALTY_LOW_TX_1H

    s_price = score_price(chg_5m, chg_1h, chg_6h, age_minutes)
    score += s_price
    feat.update({
        "score_buy_5m": s_buy_5m, "score_buy_1h": s_buy_1h, "score_price": s_price,
    })

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
        feat["score_security"] = SECURITY_PTS
    else:
        # GoPlus has no record yet (the common case for a pool minutes old) and
        # ALLOW_SECURITY_FALLBACK did not produce a simulated honeypot.is record.
        # Counted so the GoPlus indexing lag is measurable from /health.
        security_unknown_rejects += 1
        return reject("security_unknown")

    # ── Optional signal engine (signals.py): reject rugs before enrichment ──
    verdict = None
    # Recorded before the veto so a signal-rejected row still shows what the hand
    # score had already deducted (sell pressure, low tx counts, unverified).
    feat["penalties_total"] = penalties
    if USE_SIGNALS:
        # history= is what unlocks the documented momentum bonuses (vol_accel,
        # score_rising, holder_delta, vol_rising). It was omitted, so with
        # SIGNAL_BONUS_WEIGHT=0.5 the weight was multiplied by a bonus that
        # could never be earned — only the surge lane ever saw the history.
        verdict = signals.evaluate(pair, security=security, history=PAIR_HISTORY,
                                   filters=_filters_for_chain(chain))
        PAIR_HISTORY.observe(pair, security=security, score=score - penalties)
        feat["signal_bonus"] = verdict.bonus
        feat["signal_penalty"] = verdict.penalty
        feat["signal_notes"] = ", ".join(verdict.notes)[:500]
        if verdict.rejected:
            # Routine with new_pools discovery (dozens per cycle) — DEBUG, not
            # INFO. Near-misses that reach scoring still log at INFO.
            logger.debug(f"Signal reject {symbol}@{chain}: {','.join(verdict.reject_reasons)}")
            return reject("signal:" + ",".join(verdict.reject_reasons))

    base_score = score - penalties
    feat["base_score"] = base_score
    if base_score < PHASE1_MIN_SCORE:
        logger.debug(f"Phase gate skip {symbol}@{chain}: base_score={base_score:.1f}")
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
    feat["score_holder"] = holder_pts
    # GeckoTerminal has no 51-100 band, so top100 is legitimately None. Format
    # defensively: a ":.1f" on None raises and, inside the scanner, silently
    # drops the token.
    age_display = f"{age_minutes:.0f}" if age_minutes is not None else "?"
    t100 = f"{top100:.1f}%" if top100 is not None else "n/a"

    cex_count, has_perps, tier1 = await get_cex_listings(session, chain, token)
    cex_pts = score_cex(cex_count, has_perps, tier1)
    score += cex_pts
    feat["score_cex"] = cex_pts

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
    has_top100 = top100 is not None
    threshold = effective_threshold(
        chain, age_minutes,
        has_top100=has_top100,
        has_cex=has_cex_data(chain),
    )
    feat["alert_threshold"] = threshold
    # Authoritative ceiling (now that the top-100 band is known to be measurable
    # or not). `hand_score / ceiling_score` is how much of the *reachable* score
    # the token actually earned, which is the number to look at when a runner
    # scored "48 against a bar of 64".
    feat["ceiling_score"] = max_possible_score(
        chain, age_minutes, has_top100=has_top100, has_cex=has_cex_data(chain)
    )

    # Early-runner fast lane: a pool younger than its long volume windows can
    # never reach the threshold, so allow an AND-gated exception. This is what
    # makes USE_GECKOTERMINAL=new_pools useful rather than just noisy.
    early_ok = False
    # USE_SIGNALS is a real prerequisite, not a comment: without it the rug /
    # wash-trade hard rejects never ran, so this lane would promote candidates
    # past gates the docs (and config_warnings) claim are still on.
    if EARLY_RUNNER_MODE and USE_SIGNALS and total_score < threshold:
        early_reasons = signals.early_runner_reasons(pair, security=security,
                                                     filters=_filters_for_chain(chain))
        if not early_reasons:
            early_ok = True
            if VERBOSE_LOGGING:
                logger.info(f"Early-runner lane {symbol}@{chain} age={age_display}m score={total_score:.0f}")
    feat["early_runner"] = 1 if early_ok else 0

    # Abnormal-volume lane. Same AND-gated shape as the early lane, but the
    # qualifying signal is a step change against the pool's own baseline rather
    # than youth, so it can fire on an established pool having an unusual hour.
    surge_ok = False
    if VOLUME_SURGE_MODE and total_score < threshold and not early_ok and USE_SIGNALS:
        surge_filters = _filters_for_chain(chain)
        surge_reasons = signals.volume_surge_reasons(
            pair, history=PAIR_HISTORY, security=security, filters=surge_filters,
        )
        metrics = signals.surge_metrics(pair, history=PAIR_HISTORY)
        feat["vol_accel"] = metrics["vol_accel"]
        feat["vol_observations"] = metrics["observations"]
        if not surge_reasons:
            surge_ok = True
            logger.info(
                f"Volume surge {symbol}@{chain} "
                f"accel=x{metrics['vol_accel']:.1f} vol/liq={metrics['vol_liq']:.3f} "
                f"txns={buys_5m + sells_5m} score={total_score:.0f}"
            )
        elif VERBOSE_LOGGING and metrics["vol_accel"] >= surge_filters.surge_min_accel:
            # Near-miss: the volume moved but something else withheld the pass.
            logger.info(
                f"Volume surge near-miss {symbol}@{chain} "
                f"accel=x{metrics['vol_accel']:.1f}: {','.join(surge_reasons)}"
            )
    feat["volume_surge"] = 1 if surge_ok else 0

    # ── Scored-token logging: near-misses only ─────────────────────────────────
    # With new_pools discovery, ~80 tokens per cycle reach scoring and all but a
    # handful die at below_threshold. Logging every one at INFO is the "bullshit
    # spam" — full score lines are emitted only for tokens that alerted, took the
    # early lane, or came within NEAR_MISS_POINTS of the bar. Everything else is
    # still a queryable row in the features table when LOG_FEATURES=true.
    near_miss_gap = _env_float("NEAR_MISS_POINTS", 15.0)
    is_near_miss = (threshold - total_score) <= near_miss_gap
    if VERBOSE_LOGGING and (early_ok or is_near_miss or total_score >= threshold):
        logger.info(
            f"{symbol}@{chain} score={total_score:.0f} (hand={legacy_total:.0f}/{threshold:.1f}) | "
            f"vol={vol_5m/liquidity:.2f}xliq 5m/1h={vol_5m/vol_1h if vol_1h>0 else 0:.2f} "
            f"1h/6h={vol_1h/vol_6h if vol_6h>0 else 0:.2f} 6h/24h={vol_6h/vol_24h if vol_24h>0 else 0:.2f} | "
            f"buy5m={buys_5m}/{sells_5m} buy1h={buys_1h}/{sells_1h} | "
            f"age={age_display}m | holders top10={top10:.1f}% top50={top50:.1f}% top100={t100} "
            f"({holders.source or 'unavailable'}) | "
            f"cex={cex_count} perps={has_perps} | base={base_score:.1f} penalties={penalties} "
            f"signal=+{signal_bonus:.0f}/-{signal_penalty:.0f}"
            + ("  [EARLY]" if early_ok else "")
        )

    # The gate is `total_score`, which already folds in the signal penalties and
    # the weighted bonus. Gating on `legacy_total` as well made
    # SIGNAL_BONUS_WEIGHT dead as a promotion control: at any weight the bonus
    # was ignored, because `legacy_total` never contains it. Since
    # total_score = legacy_total - signal_penalty + bonus*weight, and the weight
    # defaults to 0.0, gating on total_score alone is *identical* to the old
    # two-gate form at the default while letting the knob actually work above it.
    if total_score < threshold and not early_ok and not surge_ok:
        return reject("below_threshold")

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
        "dex_id": dex_id,
        "venue_tradeable": venue_tradeable,
        "venue_requirement": venue_needs,
        "volume_surge": surge_ok,
        "vol_accel": feat.get("vol_accel"),
        "first_sight_mcap": (first_sight or {}).get("mcap"),
        "first_sight_age_minutes": (first_sight or {}).get("age_minutes"),
        "first_sight_ts": (first_sight or {}).get("ts"),
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


async def v4_quote_min_out(chain: str, token_address: str, amount_native: float,
                           slippage: float, session=None) -> Tuple[Optional[int], Optional[dict]]:
    """Minimum acceptable output for a V4 buy, from the DexScreener native price.

    The on-chain V4Quoter cannot be simulated through public RPCs (its
    unlock+callback+revert pattern returns empty revert data via eth_call, on
    every chain tested), so the quote comes from the same price feed the bot
    already trusts for alerts. Returns (min_out_raw, pool_key) or (None, None).
    hookData is always empty: V2MemeHook-style hooks need none for vanilla swaps.
    """
    key = await v4_pool_key_for_token(chain, token_address)
    if not key:
        return None, None
    try:
        price_native = await get_token_price_native(session, chain, token_address)
    except Exception as e:
        logger.warning(f"V4 quote price lookup failed for {chain} {token_address[:10]}...: {e}")
        return None, None
    if not price_native or price_native <= 0:
        return None, None
    try:
        decimals = await get_token_decimals(chain, token_address)
    except Exception:
        decimals = 18
    expected_tokens = amount_native / price_native
    min_tokens = expected_tokens * max(0.0, 1 - slippage / 100)
    min_out_raw = int(min_tokens * (10 ** decimals))
    if min_out_raw <= 0:
        return None, None
    return min_out_raw, key


async def v4_pool_key_for_token(chain: str, token_address: str) -> Optional[dict]:
    """PoolKey for the token's deepest V4 pool, via DexScreener + PositionManager.

    DexScreener's /tokens/v1/{chain}/{address} response carries each pool's
    address; the deepest-liquidity pool wins (same rule as _deepest_pair). The
    pool id resolves to a PoolKey on-chain. Returns None when no V4 pool exists.
    """
    pair_address = None
    try:
        async with aiohttp.ClientSession() as temp_session:
            data = await fetch_json(
                temp_session,
                f"https://api.dexscreener.com/tokens/v1/{chain}/{token_address}",
            )
        pair = _deepest_pair(data)
        if pair:
            pair_address = pair.get("pairAddress")
    except Exception as e:
        logger.debug(f"V4 pool discovery failed for {chain} {token_address[:10]}...: {e}")
    if not pair_address:
        return None
    return await resolve_v4_pool_key(chain, pair_address)


def build_v4_swap_calldata(pool_key: dict, zero_for_one: bool, amount_in: int,
                           amount_out_min: int, token_out: str,
                           sender: str, chain_id: int) -> str:
    """Universal Router execute() calldata for a V4 exact-input single swap."""
    codec = _V4RouterCodec()
    checksum = Web3.to_checksum_address
    key = codec.encode.v4_pool_key(
        checksum(pool_key["currency0"]), checksum(pool_key["currency1"]),
        int(pool_key["fee"]), int(pool_key["tickSpacing"]),
        checksum(pool_key["hooks"]),
    )
    native_in = checksum(pool_key["currency0"]) == checksum(V4_NATIVE_SENTINEL) if zero_for_one else False
    chain = (codec.encode.chain()
             .v4_swap()
             .swap_exact_in_single(pool_key=key, zero_for_one=zero_for_one,
                                   amount_in=int(amount_in),
                                   amount_out_min=int(amount_out_min))
             .take_all(checksum(token_out), 0)
             .settle_all(checksum(pool_key["currency0"] if zero_for_one else pool_key["currency1"]),
                         int(amount_in))
             .build_v4_swap())
    deadline = int(time.time()) + 300
    return chain.build(deadline)


async def try_v4_swap(w3, chain, token_address: str, amount_in: int,
                      is_eth_input: bool, slippage: float,
                      price_native: float = 0.0) -> Tuple[bool, str, int]:
    """V4 swap via the Universal Router (third fallback after V3, V2).

    Quote-first like the other paths: DexScreener native price sets
    amount_out_min, so an empty/thin pool fails before any transaction. Native
    input settles currency0 == address(0); token input needs a Permit2 approval
    to the Universal Router first (sells).
    """
    if not V4_TRADING or not V4_CODEC_AVAILABLE:
        return False, "V4 trading disabled (V4_TRADING=true to enable)", 0
    ur_addr = get_universal_router(chain)
    if not ur_addr or not Web3.is_address(ur_addr):
        return False, f"No Universal Router configured for {chain}", 0
    if not WALLET_ADDRESS:
        return False, "No wallet loaded", 0

    key = await v4_pool_key_for_token(chain, token_address)
    if not key:
        return False, f"No V4 pool found for {chain} {token_address[:10]}...", 0

    c0 = Web3.to_checksum_address(key["currency0"])
    c1 = Web3.to_checksum_address(key["currency1"])
    token_ck = Web3.to_checksum_address(token_address)
    weth = get_weth_address(chain)
    if is_eth_input:
        # Native in: pool must take native on one side. zeroForOne when the
        # native sentinel is currency0 (the common layout).
        if c0 == Web3.to_checksum_address(V4_NATIVE_SENTINEL):
            zero_for_one, settle_ccy = True, c0
        elif c1 == Web3.to_checksum_address(V4_NATIVE_SENTINEL):
            zero_for_one, settle_ccy = False, c1
        else:
            # WNATIVE-routed pool: fall back to wrapped path via currency match.
            if weth and (c0.lower() == weth.lower() or c1.lower() == weth.lower()):
                zero_for_one = (c0.lower() == weth.lower())
                settle_ccy = c0 if zero_for_one else c1
            else:
                return False, "V4 pool takes neither native nor WNATIVE input", 0
    else:
        if token_ck.lower() == c0.lower():
            zero_for_one, settle_ccy = True, c1
        elif token_ck.lower() == c1.lower():
            zero_for_one, settle_ccy = False, c0
        else:
            return False, "V4 pool does not contain the sell token", 0

    amount_native = float(w3.from_wei(amount_in, 'ether')) if is_eth_input else 0.0
    if is_eth_input:
        min_out, _ = await v4_quote_min_out(
            chain, token_address, amount_native, slippage)
    else:
        # Sell side: price the output (native) from the token price.
        try:
            px = price_native or await get_token_price_native(None, chain, token_address)
            decimals = await get_token_decimals(chain, token_address)
            tokens_in = amount_in / (10 ** decimals)
            min_out = int(tokens_in * (px or 0) * max(0.0, 1 - slippage / 100) * 1e18)
            if min_out <= 0:
                return False, "V4 sell quote has no price", 0
        except Exception as e:
            return False, f"V4 sell quote failed: {e}", 0
    if not min_out or min_out <= 0:
        return False, "V4 quote has no price (DexScreener)", 0

    try:
        token_out = token_address if is_eth_input else (
            get_weth_address(chain) or V4_NATIVE_SENTINEL)
        calldata = await asyncio.to_thread(
            build_v4_swap_calldata, key, zero_for_one, int(amount_in),
            int(min_out), token_out, WALLET_ADDRESS, w3.eth.chain_id,
        )
    except Exception as e:
        logger.error(f"V4 calldata build failed: {e}")
        return False, f"V4 calldata build failed: {e}", 0

    # Sells move ERC20s through the router: Permit2 approval first.
    if not is_eth_input:
        try:
            ok = await ensure_permit2_approval(chain, token_address, ur_addr, int(amount_in))
            if not ok:
                return False, "Permit2 approval for Universal Router failed", 0
        except Exception as e:
            return False, f"Permit2 approval failed: {e}", 0

    try:
        gas_fields = await build_gas_fields(w3, chain, multiplier=1.2)
        async with tx_lock(chain):
            nonce = await asyncio.to_thread(w3.eth.get_transaction_count, WALLET_ADDRESS, 'pending')
            tx_dict = {
                'from': WALLET_ADDRESS,
                'to': Web3.to_checksum_address(ur_addr),
                'data': calldata,
                'nonce': nonce,
                'gas': 600000,
                **gas_fields,
            }
            if is_eth_input:
                tx_dict['value'] = int(amount_in)
            try:
                estimated = await asyncio.to_thread(
                    w3.eth.estimate_gas, {k: v for k, v in tx_dict.items() if k != 'gas'})
                tx_dict['gas'] = int(estimated * 1.3)
            except Exception as gas_err:
                logger.warning(f"V4 gas estimation failed for {chain}, using fallback 600k: {gas_err}")
            tx_hash = await asyncio.to_thread(
                w3.eth.send_raw_transaction,
                getattr(w3.eth.account.sign_transaction(tx_dict, PRIVATE_KEY),
                        'raw_transaction', None)
                or w3.eth.account.sign_transaction(tx_dict, PRIVATE_KEY).rawTransaction,
            )
        logger.info(f"V4 swap tx sent: {tx_hash.hex()} | minOut={min_out} | gas={tx_dict['gas']}")
        receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
        if receipt.status != 1:
            return False, f"V4 tx reverted: {tx_hash.hex()}", 0
        # Return the quoted floor as tokens received (receipt has no decoding
        # without the pool events; monitor_positions re-prices live anyway).
        return True, tx_hash.hex(), int(min_out)
    except Exception as e:
        logger.error(f"V4 swap exception: {e}")
        return False, str(e), 0


async def ensure_permit2_approval(chain: str, token_address: str,
                                  spender: str, amount: int) -> bool:
    """Approve the Universal Router via Permit2's approve (one-time per token).

    Standard ERC20 approve(token -> Permit2) then Permit2.approve(token ->
    router). Returns True when allowance already covers amount.
    """
    w3 = w3_instances.get(chain)
    if not w3 or not WALLET_ADDRESS:
        return False
    permit2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
    try:
        allowance = await asyncio.to_thread(
            token.functions.allowance(WALLET_ADDRESS, permit2).call)
        if int(allowance) < int(amount):
            gas_fields = await build_gas_fields(w3, chain, multiplier=1.2)
            async with tx_lock(chain):
                nonce = await asyncio.to_thread(
                    w3.eth.get_transaction_count, WALLET_ADDRESS, 'pending')
                tx = token.functions.approve(
                    Web3.to_checksum_address(permit2), 2**256 - 1
                ).build_transaction({'from': WALLET_ADDRESS, 'nonce': nonce, **gas_fields})
                signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                raw = getattr(signed, 'raw_transaction', getattr(signed, 'rawTransaction', None))
                tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, raw)
                await asyncio.to_thread(
                    w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
    except Exception as e:
        logger.warning(f"ERC20->Permit2 approve failed: {e}")
        return False
    permit2_abi = [{"inputs": [{"internalType": "address", "name": "token", "type": "address"},
                               {"internalType": "address", "name": "spender", "type": "address"},
                               {"internalType": "uint160", "name": "amount", "type": "uint160"},
                               {"internalType": "uint48", "name": "expiration", "type": "uint48"}],
                    "name": "approve", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
                   {"inputs": [{"internalType": "address", "name": "owner", "type": "address"},
                               {"internalType": "address", "name": "token", "type": "address"},
                               {"internalType": "address", "name": "spender", "type": "address"}],
                    "name": "allowance",
                    "outputs": [{"internalType": "uint160", "name": "amount", "type": "uint160"},
                                {"internalType": "uint48", "name": "expiration", "type": "uint48"},
                                {"internalType": "uint48", "name": "nonce", "type": "uint48"}],
                    "stateMutability": "view", "type": "function"}]
    try:
        p2 = w3.eth.contract(address=Web3.to_checksum_address(permit2), abi=permit2_abi)
        allowed, _, _ = await asyncio.to_thread(
            p2.functions.allowance(WALLET_ADDRESS, token_address, spender).call)
        if int(allowed) >= int(amount):
            return True
        gas_fields = await build_gas_fields(w3, chain, multiplier=1.2)
        async with tx_lock(chain):
            nonce = await asyncio.to_thread(
                w3.eth.get_transaction_count, WALLET_ADDRESS, 'pending')
            tx = p2.functions.approve(
                Web3.to_checksum_address(token_address),
                Web3.to_checksum_address(spender),
                2**160 - 1, 2**48 - 1,
            ).build_transaction({'from': WALLET_ADDRESS, 'nonce': nonce, **gas_fields})
            signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            raw = getattr(signed, 'raw_transaction', getattr(signed, 'rawTransaction', None))
            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, raw)
            receipt = await asyncio.to_thread(
                w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
            return receipt.status == 1
    except Exception as e:
        logger.warning(f"Permit2->router approve failed: {e}")
        return False

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
        if V4_TRADING:
            # V4 pools are invisible to V2/V3 quoters by construction (singleton
            # PoolManager, not standalone pool contracts) — most new Robinhood
            # launches live there now.
            logger.info(f"V2 failed, trying V4 fallback for {chain} {token_address[:10]}...")
            success4, tx_hash4, tokens_received4 = await try_v4_swap(
                w3, chain, token_address, amount_in_wei,
                is_eth_input=True, slippage=slippage, price_native=price_native or 0.0,
            )
            if success4:
                human_tokens = tokens_received4 / (10 ** decimals) if tokens_received4 > 0 else 0
                logger.info(f"V4 Buy confirmed: {human_tokens} tokens for {amount_native} {NATIVE_SYMBOL[chain]}")
                return True, tx_hash4, human_tokens
            return False, f"V3: {tx_hash} | V2: {tx_hash2} | V4: {tx_hash4}", 0.0
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
        if V4_TRADING:
            logger.info(f"V2 sell failed, trying V4 fallback for {chain} {token_address[:10]}...")
            success4, tx_hash4, weth_received4 = await try_v4_swap(
                w3, chain, token_address, sell_amount,
                is_eth_input=False, slippage=slippage,
            )
            if success4:
                native_received = w3.from_wei(weth_received4, 'ether') if weth_received4 > 0 else 0
                logger.info(f"V4 Sell confirmed: {percentage}% sold, received {native_received} W{NATIVE_SYMBOL[chain]}")
                return True, tx_hash4, float(native_received)
            return False, f"V3: {tx_hash} | V2: {tx_hash2} | V4: {tx_hash4}", 0.0
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
    if pos_id is None:
        # The buy DID execute — only the bookkeeping failed (typically a WAL
        # lock held by the feature-writer's own connection). Announcing
        # "Position Opened" here would leave an untracked position with no
        # trailing stop, no take-profit and no monitoring.
        await tg_send(
            f"⚠️ <b>BUY EXECUTED but NOT TRACKED</b>\n"
            f"{esc(symbol)} on {chain.upper()}\n"
            f"Spent: {amount_native} {NATIVE_SYMBOL[chain]}\n"
            f"Tx: <code>{esc(tx_hash)}</code>\n"
            f"<i>The positions table rejected the insert, so no trailing stop "
            f"or take-profit will run. Sell manually or restart and re-add.</i>",
            parse_mode=ParseMode.HTML,
        )
        return None
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

def build_no_route_keyboard(chain, token_address):
    """Buttons for an alert whose pool the configured routers cannot reach.

    Deliberately no buy buttons: every one of them would fail at quote time.
    The point of the alert is still to inform, not to offer an action that
    cannot work.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 DexScreener",
                              url=f"https://dexscreener.com/{chain}/{token_address}")],
        [InlineKeyboardButton("🚫 Skip", callback_data="noop")],
    ])


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

    elif cmd == "/export":
        await handle_export_command(message, args)

    elif cmd == "/late":
        await handle_late_command(args)

    elif cmd == "/health":
        await handle_health_command()

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
# DATA EXPORT (/export, /late, /health) — data out without shell access
# ═══════════════════════════════════════════════════════════════════════════════
#
# Free hosts give no shell and wipe local state, so the features table is only
# reachable through Telegram. These commands dump it as documents plus answer
# the two diagnostic questions every operator asks: "how late am I being
# alerted?" (/late) and "what is this process actually running?" (/health).


def _export_db_path() -> str:
    """The SQLite file the feature logger is actually writing to."""
    return FEATURE_DB_PATH or DB_PATH


def build_features_csv(limit: int = 20000, alerts_only: bool = False,
                       db_path: Optional[str] = None) -> Tuple[bytes, int, int]:
    """Dump the features table to CSV bytes (newest rows first).

    Returns (csv_bytes, rows_included, total_rows). Rows are capped at ``limit``
    so a long-running bot cannot produce a document Telegram refuses to send.
    Pure function over the DB file: safe to unit-test without a bot instance.
    """
    path = db_path or _export_db_path()
    limit = max(1, min(int(limit), EXPORT_MAX_ROWS))
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "features" not in tables:
            return b"", 0, 0
        total = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        where = "WHERE alert_sent = 1" if alerts_only else ""
        cur = conn.execute(
            f"SELECT * FROM features {where} ORDER BY id DESC LIMIT ?", (limit,)
        )
        columns = [d[0] for d in cur.description]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        n = 0
        for row in cur:
            writer.writerow(row)
            n += 1
    finally:
        conn.close()
    return buf.getvalue().encode("utf-8"), n, total


def build_late_report(limit: int = 20, db_path: Optional[str] = None) -> str:
    """How long after first sight each alerted token fired, worst offenders first.

    Uses the first_sight_* columns the bot already records (see _first_sight_note):
    delay + mcap multiple at alert time separates "discovery was late" from
    "the gate held a good call back". Pure function: testable without Telegram.
    """
    path = db_path or _export_db_path()
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "features" not in tables:
            return "No features table (is LOG_FEATURES=true?)."
        rows = conn.execute(
            """SELECT symbol, chain, ts_epoch, first_sight_ts, first_sight_mcap,
                      market_cap_usd, hand_score
               FROM features WHERE alert_sent = 1 AND first_sight_ts IS NOT NULL
               ORDER BY (ts_epoch - first_sight_ts) DESC LIMIT ?""",
            (max(1, min(int(limit), 50)),),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM features WHERE alert_sent = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    if not rows:
        return f"Alerted rows: {total}, but none carry first-sight data."
    lines = [f"🐢 <b>Lateness report</b> ({len(rows)} latest shown, {total} alerts total)"]
    for sym, chain, ts, fs_ts, fs_mcap, mcap, score in rows:
        delay_m = (ts - fs_ts) / 60.0 if ts and fs_ts else 0
        mult = (mcap / fs_mcap) if fs_mcap else 0
        lines.append(
            f"• {esc(sym or '?')}@{esc(chain or '?')}: "
            f"alerted {delay_m:.0f}m after first sight, "
            f"${(fs_mcap or 0):,.0f} → ${(mcap or 0):,.0f} ({mult:.1f}x), score {score}"
        )
    return "\n".join(lines)


async def handle_export_command(message, args):
    """Send the features table as one or more CSV documents.

    Usage: /export [N] [alerts] — N newest rows (default 20000, capped by
    EXPORT_MAX_ROWS), or only alert_sent=1 rows with the `alerts` flag.
    Runs the SQLite read in a thread so a large dump cannot stall the scan loop.
    """
    n = EXPORT_MAX_ROWS
    alerts_only = False
    for a in args:
        if a.lower() in ("alerts", "alerted"):
            alerts_only = True
        else:
            try:
                n = int(a)
            except ValueError:
                pass
    n = max(1, min(n, EXPORT_MAX_ROWS))
    if not LOG_FEATURES:
        await bot.send_message(
            chat_id=CHAT_ID,
            text="⚠️ LOG_FEATURES is off, so the features table may be thin or empty. "
                 "Set LOG_FEATURES=true and restart to start collecting rows.",
            parse_mode=ParseMode.HTML,
        )
    await bot.send_message(chat_id=CHAT_ID, text="⏳ Building CSV export…")
    try:
        csv_bytes, included, total = await asyncio.to_thread(
            build_features_csv, n, alerts_only
        )
    except Exception as e:
        logger.error(f"/export failed: {e}", exc_info=True)
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Export failed: {esc(str(e))}",
                               parse_mode=ParseMode.HTML)
        return
    if included == 0:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"Empty export (table holds {total} rows). "
                 "If 0, the logger isn't writing — check LOG_FEATURES=true in Render env.",
        )
        return
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    kind = "alerts" if alerts_only else "features"
    # Telegram caps a single document at 50 MB; chunk so a big table still lands.
    chunks = max(1, (included + EXPORT_CHUNK_ROWS - 1) // EXPORT_CHUNK_ROWS)
    lines = list(csv_bytes.decode("utf-8").splitlines())
    header, body = lines[0], lines[1:]
    for i in range(chunks):
        part = body[i * EXPORT_CHUNK_ROWS:(i + 1) * EXPORT_CHUNK_ROWS]
        data = ("\n".join([header] + part) + "\n").encode("utf-8")
        fname = f"{kind}_{date}_p{i + 1}of{chunks}.csv" if chunks > 1 else f"{kind}_{date}.csv"
        try:
            await bot.send_document(
                chat_id=CHAT_ID,
                document=InputFile(data, filename=fname),
                caption=(f"📦 <b>{kind}.csv</b>: rows {i * EXPORT_CHUNK_ROWS + 1}–"
                         f"{i * EXPORT_CHUNK_ROWS + len(part)} of {included} "
                         f"(table: {total})" if chunks > 1 else
                         f"📦 <b>{kind}.csv</b>: {included} rows (table holds {total})"),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.error(f"/export send failed: {e}")
            await bot.send_message(chat_id=CHAT_ID, text=f"❌ Send failed: {esc(str(e))}",
                                   parse_mode=ParseMode.HTML)
            return


async def handle_late_command(args):
    """Answer 'how late am I being alerted?' from first-sight columns."""
    n = 20
    for a in args:
        try:
            n = int(a)
        except ValueError:
            pass
    try:
        report = await asyncio.to_thread(build_late_report, n)
    except Exception as e:
        logger.error(f"/late failed: {e}", exc_info=True)
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ /late failed: {esc(str(e))}",
                               parse_mode=ParseMode.HTML)
        return
    await bot.send_message(chat_id=CHAT_ID, text=report, parse_mode=ParseMode.HTML)


async def handle_health_command():
    """In-chat version of /health: what this process is actually running."""
    stats = FEATURE_LOGGER.stats()
    warns = config_warnings()
    lines = [
        "💓 <b>Bot health</b>",
        f"Scanned pairs: {total_pairs_scanned} | evaluated: {tokens_evaluated} | alerts: {alerts_sent}",
        f"Feature logging: {'✅ ON' if LOG_FEATURES else '❌ OFF'} "
        f"(logged {stats['logged']}, written {stats['written_batches']}, dropped {stats['dropped']})",
        f"Feature DB: <code>{esc(_export_db_path())}</code>",
        f"Config: <code>{esc(ENV_FILE_LOADED)}</code>",
        f"Ceilings: {esc(str(effective_mcap_ceilings()))} | re_alerts={RE_ALERTS_ENABLED}",
        f"Quiet because: security_unknown={security_unknown_rejects} "
        f"unsupported_venue={unsupported_venue_rejects} untradeable={untradeable_alerts}",
    ]
    if warns:
        lines.append("⚠️ <b>Config warnings:</b>\n" + "\n".join(f"• {esc(w)}" for w in warns))
    await bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode=ParseMode.HTML)

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
        f"/features — Feature-logging stats\n"
        f"/export [N] [alerts] — DM the features table as CSV (N newest rows)\n"
        f"/late [N] — Worst alert delays vs first sight\n"
        f"/health — What this process is running, why it's quiet"
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

def _fmt_pct(value) -> str:
    """Render a holder percentage that may legitimately be unmeasured.

    GeckoTerminal publishes no 51-100 band, so ``top100`` is ``None`` rather than
    a guess. Formatting that with ``:.1f`` raised
    "unsupported format string passed to NoneType.__format__", which — because
    the alert text is built outside the try that guards ``tg_send`` — killed the
    whole scan cycle on every alert.
    """
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


async def send_alert(alert):
    global alerts_sent, untradeable_alerts
    sec = alert.get("security") or {}
    pct10, pct50, pct100 = alert.get("holder_pct", (0, 0, 0))
    buy_pct_5m = (alert['buys_5m'] / (alert['buys_5m'] + alert['sells_5m']) * 100) if (alert['buys_5m'] + alert['sells_5m']) > 0 else 0
    buy_pct_1h = (alert['buys_1h'] / (alert['buys_1h'] + alert['sells_1h']) * 100) if (alert['buys_1h'] + alert['sells_1h']) > 0 else 0

    if sec.get("source") == "etherscan":
        # Robinhood's real security source. Labelling this "GoPlus" hid the
        # verification result, which is the one thing Etherscan actually tells us.
        if sec.get("verification_known"):
            verified = "✅ Yes" if sec.get("is_verified") else "⚠️ No"
        else:
            verified = "❓ Unknown"
        sec_text = (
            f"<b>Security (Etherscan):</b>\n"
            f"  Verified source: {verified}\n"
            f"  Proxy: {'❌ YES' if sec.get('is_proxy') else '✅ No'}\n"
        )
    elif sec.get("source") == "blockscout":
        sec_text = (
            f"<b>Security (Blockscout):</b>\n"
            f"  Verified: {'✅' if sec.get('is_verified') else '⚠️ No'}\n"
            f"  Proxy: {'❌ YES' if sec.get('is_proxy') else '✅ No'}\n"
        )
    else:
        buy_tax = sec.get('buy_tax') or 0
        sell_tax = sec.get('sell_tax') or 0
        sec_text = (
            f"<b>Security (GoPlus):</b> {'✅' if sec else '❓'}\n"
            f"  Honeypot: {'✅ No' if not sec or not sec.get('is_honeypot') else '❌ YES'}\n"
            f"  Buy Tax: {buy_tax:.1f}%\n"
            f"  Sell Tax: {sell_tax:.1f}%\n"
        )

    age_display = f"{alert['age_minutes']:.0f}" if alert.get("age_minutes") is not None else "?"

    # ── Route honesty ────────────────────────────────────────────────────────
    # A pool on a DEX we hold no router for (Uniswap V4, a V3 fork, Pons,
    # Aerodrome, ...) cannot be bought. Saying so, and withholding the buy
    # buttons, is strictly better than offering buttons that fail at quote time.
    venue_ok = alert.get("venue_tradeable")
    dex_display = esc(alert.get("dex_id") or "unknown")
    if venue_ok is False:
        untradeable_alerts += 1
        needs = esc(alert.get("venue_requirement") or "unknown")
        route_text = (
            f"⚠️ <b>No route — not tradeable by this bot.</b>\n"
            f"  DEX: <code>{dex_display}</code>\n"
            f"  Needs: {needs}\n"
            f"  <i>A router only routes its own factory's pools, so a V2/V3 router\n"
            f"  on this chain does not reach another DEX. Buttons withheld.</i>\n\n"
        )
    else:
        suffix = "" if venue_ok else " (tradeability unknown)"
        route_text = f"🏦 Venue: <code>{dex_display}</code>{suffix}\n\n"

    # How early were we? Either discovery saw this small and the gate held it
    # back, or discovery never saw it small — opposite fixes, and previously only
    # distinguishable by grepping FIRST SIGHT out of the log.
    surge_text = ""
    if alert.get("volume_surge"):
        accel = alert.get("vol_accel") or 0.0
        surge_text = (f"⚡ <b>Abnormal volume:</b> {accel:.1f}x its own 5m baseline\n\n")

    fs_mcap = alert.get("first_sight_mcap")
    fs_ts = alert.get("first_sight_ts")
    first_text = ""
    if fs_mcap:
        mins = (time.time() - fs_ts) / 60.0 if fs_ts else None
        mult = (alert["market_cap"] / fs_mcap) if fs_mcap else 0
        ago = f"{mins:.0f}m ago" if mins is not None else "earlier"
        first_text = (
            f"👀 <b>First seen:</b> {ago} at ${fs_mcap:,.0f} mcap "
            f"-> now ${alert['market_cap']:,.0f} ({mult:.1f}x)\n\n"
        )
    text = (
        f"🚨 <b>ONCHAIN PUMP — Score {alert['total_score']:.0f}/100</b>\n\n"
        f"<b>{esc(alert['name'])}</b> ({esc(alert['symbol'])})\n"
        f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n"
        f"🕒 Age: <b>{age_display} min</b>\n\n"
        f"{surge_text}"
        f"{first_text}"
        f"{route_text}"
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
        f"  Top 10: {_fmt_pct(pct10)}\n"
        f"  Top 50: {_fmt_pct(pct50)}\n"
        f"  Top 100: {_fmt_pct(pct100)}\n\n"
        f"<b>CEX Listings:</b> {alert['cex_count']} (perps: {'✅' if alert['has_perps'] else '❌'})\n\n"
        f"<b>Signal Engine:</b> +{alert.get('signal_bonus', 0):.0f} / -{alert.get('signal_penalty', 0):.0f}\n"
        + (f"<i>{esc(', '.join(alert.get('signal_notes', [])))}</i>\n" if alert.get('signal_notes') else "")
        + f"📝 <b>Contract:</b> <code>{esc(alert['token_address'])}</code>"
    )

    if venue_ok is False:
        keyboard = build_no_route_keyboard(alert['chain'], alert['token_address'])
    else:
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
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        # False, not "nothing": the dispatcher must not write dedupe state for a
        # message the user never received, or a transient outage would mark the
        # token as reported and (RE_ALERTS=false) silence it permanently.
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════════════════════

async def dispatch_alerts(results, batch: Optional[set] = None) -> int:
    """Send alerts for evaluation results that cleared the gate.

    Extracted from the scan loop so the watchlist lane shares exactly the same
    de-duplication and failure handling as the discovery lane: an alert that
    fails to build or send must never take down the cycle — that failure mode
    looked exactly like "the bot found nothing" while it was in fact finding
    candidates and dying on every send.

    De-duplication is layered because the failure modes are layered. One scan
    can bring the same token in twice (GeckoTerminal lists pool A, DexScreener's
    ``/tokens/v1`` answers with pool B of the same contract), the two lanes run
    as separate dispatch calls in the same cycle, the alerts table can lose a
    write, and — with ``RE_ALERTS=true`` — an old alert can be due for a repeat.
    So:

    * ``batch`` — at most one message per (chain, token) for the whole cycle,
      always, regardless of how far apart the two scores are. The scan loop
      passes one shared set to both lanes; without it a ≥12-point gap between
      the discovery and watchlist readings would send twice seconds apart.
    * the ``alerts`` table — durable, across batches and restarts;
    * ``ALERTED_THIS_RUN`` — in-memory fallback when the table has no record
      for a token we already pushed (a failed upsert is only a warning).

    Dedupe state is written **only after a delivered message**: a failed send
    is still added to ``batch`` (so one cycle never double-attempts it) but
    leaves no record, so the next cycle tries again instead of treating a
    Telegram outage as "the user was told".
    """
    sent = 0
    if batch is None:
        batch = set()
    for result in results:
        if not result:
            continue
        if isinstance(result, Exception):
            # Previously dropped in silence, which made a systematic failure
            # (a bad env value parsed per candidate, say) look like "no
            # candidates" — the exact diagnosis this repo keeps warning about.
            logger.warning(f"Evaluator raised; candidate skipped: {result!r}")
            continue
        try:
            key = (str(result["chain"]).lower(), str(result["token_address"]).lower())
            if key in batch:
                logger.debug(
                    f"Duplicate in batch for {result['symbol']}@{result['chain']} — suppressed"
                )
                continue
            last_alert = db_get_last_alert(result["chain"], result["token_address"])
            if last_alert:
                if not RE_ALERTS_ENABLED:
                    should_alert = False
                else:
                    hours_since = (time.time() - last_alert["alert_time"]) / 3600
                    improved = (
                        result["total_score"] - last_alert["total_score"]
                    ) >= SCORE_IMPROVEMENT_THRESHOLD
                    should_alert = hours_since >= RE_ALERT_COOLDOWN_HOURS or improved
            elif key in ALERTED_THIS_RUN:
                # No durable record although we sent this run: the upsert failed.
                should_alert = False
            else:
                should_alert = True
            if not should_alert:
                logger.debug(
                    f"Re-alert suppressed for {result['symbol']}@{result['chain']} "
                    f"(RE_ALERTS={RE_ALERTS_ENABLED})"
                )
                continue
            delivered = bool(await send_alert(result))
            batch.add(key)
            if not delivered:
                # Nothing recorded: the next cycle may legitimately retry.
                continue
            db_record_alert(result["chain"], result["token_address"], result["symbol"], result["total_score"])
            ALERTED_THIS_RUN.add(key)
            sent += 1
            await asyncio.sleep(1)
        except Exception:
            logger.error(
                f"Alert pipeline failed for {result.get('symbol')}@{result.get('chain')}",
                exc_info=True,
            )
    return sent


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
                f"Mcap ceiling: {_fmt_ceilings(effective_mcap_ceilings())}\n"
                f"Feature logging: {'ON' if LOG_FEATURES else 'off'}\n"
                f"Config: <code>{esc(ENV_FILE_LOADED)}</code>\n"
                + (
                    "\n⚠️ <b>CONFIG WARNINGS</b>\n"
                    + "\n".join(f"• {esc(w)}" for w in config_warnings())
                    + "\n"
                    if config_warnings() else ""
                )
                + (
                    "\nℹ️ <b>NOTES</b>\n"
                    + "\n".join(f"• {esc(n)}" for n in config_notes())
                    + "\n"
                    if config_notes() else ""
                )
                + "\n<b>Paste any CA to buy instantly!</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

        while not shutdown_flag:
            try:
                cycle_start = time.time()
                cycle_alerts = 0
                # One dedupe set for the whole cycle: discovery (per network)
                # and the watchlist lane both dispatch through the same set, so
                # one token can produce at most one message per cycle even when
                # RE_ALERTS=true lets a score jump beat the cooldown.
                cycle_batch: set = set()
                prune_caches()
                watchlist_cycle_start()
                if WATCHLIST_ENABLED and int(time.time()) % 600 < SCAN_INTERVAL:
                    db_watchlist_prune()

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

                # ── Phase A: discovery for EVERY chain, before any evaluation ──
                # This used to interleave discover→evaluate per chain, which
                # made detection latency depend on chain order: the holder
                # lookups for BSC/ETH/Base all draw on the same GeckoTerminal
                # budget as discovery, so Robinhood — last in NETWORKS, and
                # where the early runners are — did not even get *listed* until
                # minutes into the cycle. Discovery is cheap (listing calls
                # only); doing all of it first means every chain sees the
                # current feed, then enrichment can spend the budget.
                discovered: Dict[str, list] = {}
                for network in NETWORKS:
                    if shutdown_flag:
                        break
                    # Per-chain isolation: an exception here used to skip
                    # discovery for the remaining chains AND every evaluation
                    # that cycle, then surface as a "bot cycle crashed" retry a
                    # minute later — one flaky feed looked like the whole bot
                    # was down while three healthy chains went unscanned.
                    try:
                        pairs = await get_all_pairs(session, network)
                    except Exception as e:
                        logger.error(f"Discovery failed for {network}: {e}")
                        pairs = []
                    discovered[network] = pairs
                    total_pairs_scanned += len(pairs)
                    logger.info(f"{network}: {len(pairs)} pairs discovered")

                # ── Phase B: evaluate the discovered candidates ───────────────
                for network in NETWORKS:
                    if shutdown_flag:
                        break
                    pairs = discovered.get(network) or []
                    if not pairs:
                        continue
                    tasks = [evaluate_token(session, p) for p in pairs]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    cycle_alerts += await dispatch_alerts(results, cycle_batch)

                # ── Ignition watchlist ──────────────────────────────────────
                # Pools that dropped out of the feeds get re-priced here. See
                # WATCHLIST_ENABLED: discovery is event-based, so without this
                # a pool that is born quiet and runs hours later is never
                # looked at again (the boar case).
                if WATCHLIST_ENABLED and not shutdown_flag:
                    watch_pairs = await collect_watchlist_pairs(session)
                    total_pairs_scanned += len(watch_pairs)
                    if watch_pairs:
                        logger.info(f"watchlist: {len(watch_pairs)} pair(s) to re-evaluate")
                        watch_results = await asyncio.gather(
                            *[evaluate_token(session, p) for p in watch_pairs],
                            return_exceptions=True,
                        )
                        cycle_alerts += await dispatch_alerts(watch_results, cycle_batch)

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
        # `threshold` is the raw bar; `threshold_by_chain` is what evaluate_token
        # actually starts from before scaling (the gate itself is age-scaled on
        # top — see effective_threshold). Reporting both stops the "why did this
        # alert at 47?" question from needing a log dive.
        "threshold": ALERT_THRESHOLD,
        "threshold_by_chain": {
            chain: _chain_floor(chain, "MIN_SCORE", float(ALERT_THRESHOLD))
            for chain in NETWORKS
        },
        "paper_trading": PAPER_TRADING,
        # Which config file this process actually loaded, and the ceiling it is
        # actually enforcing. Both answer "I set it in .env, why is the bot
        # behaving differently?" without shell access to the host.
        "env_file": ENV_FILE_LOADED,
        "mcap_ceiling_usd": effective_mcap_ceilings(),
        "re_alerts": RE_ALERTS_ENABLED,
        # Why the bot was quiet, as numbers rather than a log grep:
        # security_unknown = GoPlus had no record yet (common minutes after a
        # pool is born); unsupported_venue = no router for that DEX (V4 etc.);
        # untradeable_alerts = alerts sent with buy buttons withheld.
        "security_unknown_rejects": security_unknown_rejects,
        "unsupported_venue_rejects": unsupported_venue_rejects,
        "untradeable_alerts": untradeable_alerts,
        # A deployment running code defaults is a different bot; report that
        # rather than letting it be inferred from the alerts it produces.
        "config_warnings": config_warnings(),
        "features": FEATURE_LOGGER.stats(),
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting Pump Bot v5.4 on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
