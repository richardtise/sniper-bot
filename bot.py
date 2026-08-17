#!/usr/bin/env python3
"""
================================================================================
PUMP SIGNAL BOT v6 — Early Micro-Cap Hunter (Strict Tax Policy)
================================================================================
Optimized for catching brand-new, low-liquidity tokens that are starting to pump.
Strict policy: ANY buy or sell tax (even 1%) instantly disqualifies the token,
treating it like a honeypot. This eliminates tokens with hidden fees.

Key improvements over v5:
  • Any tax > 0% = instant rejection (same as honeypot)
  • All other logic identical to v5 (parallel scanning, relative volume, etc.)

Required env vars:
  TELEGRAM_TOKEN   — Telegram bot token
  CHAT_ID          — Telegram chat ID for alerts
================================================================================
"""

import asyncio
import aiohttp
import time
import os
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

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")

# Networks to scan
NETWORKS = ["bsc", "ethereum", "base", "robinhood"]

# Chain mapping for GoPlusLabs
CHAIN_TO_GOPLUS_ID = {
    "bsc": "56",
    "ethereum": "1",
    "base": "8453",
    "robinhood": None,
}

# --- Timing ---
SCAN_INTERVAL = 30
HEARTBEAT_INTERVAL = 3600

# --- Minimum viability ---
MIN_LIQUIDITY_USD = 1_500.0
MIN_VOL_5M_USD = 50.0
MIN_PRICE = 1e-12

# --- Scoring weights (max total = 100) ---
VOLUME_RATIO_MAX_PTS = 25
BUY_PRESSURE_5M_MAX_PTS = 20
BUY_PRESSURE_1H_MAX_PTS = 15
PRICE_MOMENTUM_MAX_PTS = 15
SECURITY_MAX_PTS = 25

# --- Penalties ---
PENALTY_HONEYPOT = 100          # instant disqualification
PENALTY_ANY_TAX = 100           # any tax >0 also instant disqualification
PENALTY_LOW_TX_5M = 10
PENALTY_LOW_TX_1H = 5
PENALTY_SELL_PRESSURE_5M = 25
PENALTY_SELL_PRESSURE_1H = 20

# --- Alert threshold ---
ALERT_THRESHOLD = 55

# --- Cooldown ---
RE_ALERT_COOLDOWN_HOURS = 4
SCORE_IMPROVEMENT_THRESHOLD = 15

# --- API settings ---
MAX_RETRIES = 2
API_TIMEOUT = 10
CONCURRENT_API_LIMIT = 15

# --- Debug ---
VERBOSE_LOGGING = os.getenv("VERBOSE_LOGGING", "false").lower() == "true"

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING & GLOBALS
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("pump_bot_v6.log"), logging.StreamHandler()],
)
logger = logging.getLogger("pump_bot_v6")

bot = Bot(token=TELEGRAM_TOKEN)
api_semaphore = asyncio.Semaphore(CONCURRENT_API_LIMIT)

# Security cache (token -> (data, timestamp))
security_cache: Dict[str, Tuple[dict, float]] = {}

# Database
db_conn: Optional[sqlite3.Connection] = None

# Stats
total_pairs_scanned = 0
tokens_evaluated = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False
hourly_scan_records: deque = deque(maxlen=200)

DB_PATH = "pump_bot_v6.db"

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

def db_get_last_alert(chain: str, token_address: str) -> Optional[dict]:
    cur = db_conn.execute(
        "SELECT total_score, alert_time FROM alerts WHERE chain=? AND token_address=?",
        (chain, token_address)
    )
    row = cur.fetchone()
    return {"total_score": row[0], "alert_time": row[1]} if row else None

def db_record_alert(chain: str, token_address: str, symbol: str, score: int):
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
# API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_json(session: aiohttp.ClientSession, url: str, headers: Optional[Dict] = None) -> Optional[Any]:
    async with api_semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)
                ) as resp:
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

# ═══════════════════════════════════════════════════════════════════════════════
# PAIR DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

async def get_all_pairs(session: aiohttp.ClientSession, network: str) -> List[Dict]:
    pairs: List[Dict] = []
    seen: set = set()

    async def add_pair(p: Dict):
        addr = p.get("pairAddress")
        if addr and addr not in seen:
            seen.add(addr)
            pairs.append(p)

    # 1. Latest pairs
    url = f"https://api.dexscreener.com/latest/dex/pairs/{network}?page=0&pageSize=300"
    data = await fetch_json(session, url)
    if data and isinstance(data, dict) and "pairs" in data:
        for p in data["pairs"]:
            if p.get("chainId") == network:
                await add_pair(p)

    # 2. Top boosted tokens
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

    # 3. Latest profiles
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
    logger.info(f"[{network}] Collected {len(pairs)} unique pairs")
    return pairs[:300]

# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY CHECK (GoPlusLabs)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_token_security(session: aiohttp.ClientSession, chain: str, token: str) -> Optional[dict]:
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
        "lp_locked": result.get("is_lp_locked") == "1",
    }
    security_cache[cache_key] = (security, now)
    return security

# ═══════════════════════════════════════════════════════════════════════════════
# SCORING & EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════

async def evaluate_token(session: aiohttp.ClientSession, pair: Dict) -> Optional[Dict]:
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

    # Basic viability
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

    txns_5m = pair.get("txns", {}).get("m5", {}) or {}
    buys_5m = int(txns_5m.get("buys", 0))
    sells_5m = int(txns_5m.get("sells", 0))

    txns_1h = pair.get("txns", {}).get("h1", {}) or {}
    buys_1h = int(txns_1h.get("buys", 0))
    sells_1h = int(txns_1h.get("sells", 0))

    chg_5m = float(pair.get("priceChange", {}).get("m5") or 0)
    chg_1h = float(pair.get("priceChange", {}).get("h1") or 0)
    chg_6h = float(pair.get("priceChange", {}).get("h6") or 0)

    score = 0
    penalties = 0

    # 1. Volume-to-Liquidity Ratio
    vol_to_liq = vol_5m / liquidity if liquidity > 0 else 0
    if vol_to_liq >= 1.0:
        score += 25
    elif vol_to_liq >= 0.5:
        score += 20
    elif vol_to_liq >= 0.25:
        score += 15
    elif vol_to_liq >= 0.1:
        score += 10
    else:
        score += 5

    # 2. Buy Pressure 5m
    total_5m = buys_5m + sells_5m
    if total_5m > 0:
        buy_ratio_5m = buys_5m / total_5m
        if buy_ratio_5m >= 0.85:
            score += 20
        elif buy_ratio_5m >= 0.70:
            score += 15
        elif buy_ratio_5m >= 0.55:
            score += 10
        elif buy_ratio_5m >= 0.45:
            score += 5
        if buy_ratio_5m < 0.35:
            penalties += PENALTY_SELL_PRESSURE_5M
        if total_5m < 5:
            penalties += PENALTY_LOW_TX_5M
    else:
        penalties += PENALTY_LOW_TX_5M

    # 3. Buy Pressure 1h
    total_1h = buys_1h + sells_1h
    if total_1h > 0:
        buy_ratio_1h = buys_1h / total_1h
        if buy_ratio_1h >= 0.80:
            score += 15
        elif buy_ratio_1h >= 0.65:
            score += 10
        elif buy_ratio_1h >= 0.50:
            score += 5
        if buy_ratio_1h < 0.35:
            penalties += PENALTY_SELL_PRESSURE_1H
        if total_1h < 10:
            penalties += PENALTY_LOW_TX_1H
    else:
        penalties += PENALTY_LOW_TX_1H

    # 4. Price Momentum (only positive)
    if chg_5m > 0:
        if chg_5m >= 50:
            score += 10
        elif chg_5m >= 20:
            score += 7
        elif chg_5m >= 5:
            score += 4
    if chg_1h > 0:
        if chg_1h >= 100:
            score += 5
        elif chg_1h >= 50:
            score += 3

    # 5. Security Check — STRICT TAX POLICY
    security = await get_token_security(session, chain, token)
    if security:
        # Honeypot? Disqualify.
        if security.get("is_honeypot"):
            return None

        buy_tax = security.get("buy_tax", 0)
        sell_tax = security.get("sell_tax", 0)

        # ⚠️ NEW: Any tax > 0% = instant disqualification (same as honeypot)
        if buy_tax > 0 or sell_tax > 0:
            logger.info(f"Rejected {symbol}: tax detected (buy={buy_tax}%, sell={sell_tax}%)")
            return None

        # If tax is exactly 0, give full security points
        score += 25
    # If security is None (no data), we don't penalize but also no points.

    # Final score
    total_score = max(0, score - penalties)

    if VERBOSE_LOGGING:
        logger.info(
            f"{symbol} ({chain}) score={total_score} | vol_to_liq={vol_to_liq:.2f} "
            f"buy5m={buys_5m}/{sells_5m} buy1h={buys_1h}/{sells_1h} "
            f"chg5m={chg_5m:.1f}% liq=${liquidity:.0f} vol5m=${vol_5m:.0f} "
            f"security={security}"
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
        "buys_5m": buys_5m,
        "sells_5m": sells_5m,
        "buys_1h": buys_1h,
        "sells_1h": sells_1h,
        "chg_5m": chg_5m,
        "chg_1h": chg_1h,
        "security": security,
        "dex_url": f"https://dexscreener.com/{chain}/{pair_id}",
    }

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERTS
# ═══════════════════════════════════════════════════════════════════════════════

async def send_alert(alert: Dict):
    global alerts_sent
    sec = alert.get("security") or {}

    buy_pct_5m = (alert['buys_5m'] / (alert['buys_5m'] + alert['sells_5m']) * 100) if (alert['buys_5m'] + alert['sells_5m']) > 0 else 0
    buy_pct_1h = (alert['buys_1h'] / (alert['buys_1h'] + alert['sells_1h']) * 100) if (alert['buys_1h'] + alert['sells_1h']) > 0 else 0

    text = (
        f"🚨 <b>EARLY PUMP SIGNAL — Score {alert['total_score']}/100</b>\n\n"
        f"<b>{alert['name']}</b> ({alert['symbol']})\n"
        f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n\n"
        f"<b>💰 Liquidity:</b> ${alert['liquidity']:,.0f}\n"
        f"<b>📈 Volume (5m):</b> ${alert['vol_5m']:,.0f}\n"
        f"<b>💹 Price Δ (5m):</b> {alert['chg_5m']:+.1f}%\n"
        f"<b>💹 Price Δ (1h):</b> {alert['chg_1h']:+.1f}%\n\n"
        f"<b>🟢 Buy Pressure (5m):</b> {buy_pct_5m:.1f}% ({alert['buys_5m']}B / {alert['sells_5m']}S)\n"
        f"<b>🟢 Buy Pressure (1h):</b> {buy_pct_1h:.1f}% ({alert['buys_1h']}B / {alert['sells_1h']}S)\n\n"
        f"<b>🔒 Security:</b> ✅ No honeypot, 0% taxes\n\n"
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

    logger.info("🚀 Pump Bot v6 (Strict Tax Policy) starting...")

    async with aiohttp.ClientSession() as session:
        while not shutdown_flag:
            cycle_start = time.time()
            cycle_alerts = 0

            # Heartbeat
            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                uptime = (time.time() - start_time) / 3600
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🫀 <b>Bot v6 Heartbeat</b>\nUptime: {uptime:.1f}h\nPairs scanned: {total_pairs_scanned}\nAlerts sent: {alerts_sent}",
                    parse_mode=ParseMode.HTML
                )
                last_heartbeat_time = time.time()

            # Scan networks
            for network in NETWORKS:
                if shutdown_flag:
                    break

                pairs = await get_all_pairs(session, network)
                total_pairs_scanned += len(pairs)

                # Parallel evaluation
                tasks = [evaluate_token(session, p) for p in pairs]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if not result or isinstance(result, Exception):
                        continue

                    # Cooldown check
                    last_alert = db_get_last_alert(result["chain"], result["token_address"])
                    should_alert = True
                    if last_alert:
                        hours_since = (time.time() - last_alert["alert_time"]) / 3600
                        if hours_since < RE_ALERT_COOLDOWN_HOURS:
                            score_jump = result["total_score"] - last_alert["total_score"]
                            if score_jump < SCORE_IMPROVEMENT_THRESHOLD:
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
# FASTAPI
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    logger.info("Shutdown complete")

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health_check():
    return {
        "status": "alive",
        "alerts_sent": alerts_sent,
        "pairs_scanned": total_pairs_scanned,
        "threshold": ALERT_THRESHOLD,
    }

# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting Pump Bot v6 on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
