import asyncio
import aiohttp
import time
import os
import logging
import signal
from collections import deque
from typing import Dict, List, Optional
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from telegram.constants import ParseMode
from fastapi import FastAPI
import uvicorn
import threading

load_dotenv()

# ===================== CONFIG =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")

NETWORKS = ["bsc", "ethereum", "base"]
SCAN_INTERVAL = 25
HEARTBEAT_INTERVAL = 3600

TOP10_THRESHOLD = 75.0
MIN_LIQUIDITY_USD = 18000.0
MIN_VOLUME_SPIKE_RATIO = 0.8
MIN_VOLUME_RATIO_1H_24H = 0.15

# ===================== GLOBALS =====================
seen_pairs = set()
holder_cache = {}          # cache_key -> (top10_pct, timestamp)
total_pairs_scanned = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False

api_rate_limiter = asyncio.Semaphore(5)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("pump_bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)

# ===================== FASTAPI =====================
app = FastAPI()

@app.get("/")
@app.get("/health")
async def health_check():
    uptime = (time.time() - start_time) / 3600
    return {
        "status": "alive",
        "uptime_hours": round(uptime, 2),
        "pairs_scanned": total_pairs_scanned,
        "alerts_sent": alerts_sent
    }

# ===================== HELPERS =====================
async def fetch_json(session: aiohttp.ClientSession, url: str, headers=None, timeout=15):
    async with api_rate_limiter:
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    logger.warning(f"Rate limit hit for {url}, sleeping 2s")
                    await asyncio.sleep(2)
                    # Retry once
                    return await fetch_json(session, url, headers, timeout)
        except Exception as e:
            logger.debug(f"Fetch error: {e}")
        return None

async def get_all_pairs(session: aiohttp.ClientSession, network: str) -> List[Dict]:
    pairs = []
    seen = set()

    # Trending pairs
    data = await fetch_json(session, f"https://api.dexscreener.com/latest/dex/trending?chain={network}")
    if data and "pairs" in data:
        for p in data["pairs"][:150]:
            addr = p.get("pairAddress")
            if addr and addr not in seen:
                seen.add(addr)
                pairs.append(p)

    # New pairs
    data = await fetch_json(session, f"https://api.dexscreener.com/latest/dex/new?chain={network}")
    if data and "pairs" in data:
        for p in data["pairs"][:100]:
            addr = p.get("pairAddress")
            if addr and addr not in seen:
                seen.add(addr)
                pairs.append(p)

    # Global search fallback
    data = await fetch_json(session, "https://api.dexscreener.com/latest/dex/search?q=")
    if data and "pairs" in data:
        for p in data["pairs"]:
            if p.get("chainId") == network:
                addr = p.get("pairAddress")
                if addr and addr not in seen and len(pairs) < 300:
                    seen.add(addr)
                    pairs.append(p)

    pairs.sort(key=lambda x: float(x.get("volume", {}).get("m5", 0)), reverse=True)
    return pairs[:300]

# ===================== MORALIS HOLDER DATA WITH CACHE =====================
async def get_top10_percent(session: aiohttp.ClientSession, chain: str, token: str) -> float:
    if not MORALIS_API_KEY:
        return 0.0

    moralis_chain = {"bsc": "bsc", "ethereum": "eth", "base": "base"}.get(chain)
    if not moralis_chain:
        return 0.0

    cache_key = f"{moralis_chain}:{token}"
    now = time.time()
    # Check cache (valid for 10 minutes)
    if cache_key in holder_cache:
        pct, timestamp = holder_cache[cache_key]
        if now - timestamp < 600:  # 10 minutes
            logger.debug(f"Cache hit for {token} on {chain}: {pct:.2f}%")
            return pct

    url = f"https://deep-index.moralis.io/api/v2.2/erc20/{token}/owners?chain={moralis_chain}&order=DESC&limit=10"
    headers = {"X-API-Key": MORALIS_API_KEY}
    data = await fetch_json(session, url, headers=headers)

    if not data or "result" not in data:
        # Cache failure as 0.0 for a shorter time (2 minutes) to avoid repeated fails
        holder_cache[cache_key] = (0.0, now)
        return 0.0

    holders = data.get("result", [])
    total_supply = float(data.get("total_supply", 0))
    if total_supply == 0:
        holder_cache[cache_key] = (0.0, now)
        return 0.0

    top10 = sum(float(h.get("balance", 0)) for h in holders[:10])
    pct = (top10 / total_supply) * 100
    holder_cache[cache_key] = (pct, now)

    # Clean cache occasionally to prevent memory bloat
    if len(holder_cache) > 1000:
        for k in list(holder_cache.keys()):
            if now - holder_cache[k][1] > 3600:  # remove entries older than 1 hour
                del holder_cache[k]

    return pct

# ===================== EVALUATION =====================
async def evaluate_token(session: aiohttp.ClientSession, pair: Dict) -> Optional[Dict]:
    chain = pair.get("chainId")
    token = pair.get("baseToken", {}).get("address")
    pair_id = pair.get("pairAddress")
    if not all([chain, token, pair_id]):
        return None

    liquidity = float(pair.get("liquidity", {}).get("usd", 0))
    if liquidity < MIN_LIQUIDITY_USD:
        return None

    if pair_id in seen_pairs:
        return None
    seen_pairs.add(pair_id)
    if len(seen_pairs) > 10000:
        seen_pairs.clear()

    vol5 = float(pair.get("volume", {}).get("m5", 0))
    vol1h = float(pair.get("volume", {}).get("h1", 0))
    vol24h = float(pair.get("volume", {}).get("h24", 0))

    spike = False
    trigger = ""
    if vol1h > 0 and vol5 / vol1h >= MIN_VOLUME_SPIKE_RATIO:
        spike = True
        trigger = "5m>1h"
    elif vol24h > 0 and vol1h / vol24h >= MIN_VOLUME_RATIO_1H_24H:
        spike = True
        trigger = "1h>20%24h"

    if not spike:
        return None

    top10_pct = await get_top10_percent(session, chain, token)
    if top10_pct < TOP10_THRESHOLD:
        return None

    return {
        "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
        "chain": chain.upper(),
        "liquidity": liquidity,
        "vol_5m": vol5,
        "vol_1h": vol1h,
        "spike_type": trigger,
        "top10_pct": round(top10_pct, 2),
        "dex_url": f"https://dexscreener.com/{chain}/{pair_id}"
    }

# ===================== ALERT & HEARTBEAT =====================
async def send_alert(alert: Dict):
    global alerts_sent
    alerts_sent += 1
    text = f"🚨 <b>EARLY PUMP SIGNAL</b> 🚨\n\n<b>{alert['token_name']}</b> ({alert['symbol']})\n🔗 Chain: <b>{alert['chain']}</b>\n💰 Liquidity: <b>${alert['liquidity']:,.0f}</b>\n⚡ Spike: <b>{alert['spike_type']}</b>\n👥 Top10: <b>{alert['top10_pct']}%</b>\n\n🔗 <a href='{alert['dex_url']}'>DexScreener</a>"
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        logger.info(f"Alert sent → {alert['symbol']}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

async def send_heartbeat():
    uptime = (time.time() - start_time) / 3600
    text = f"🫀 <b>Bot Heartbeat</b> 🫀\nUptime: <b>{uptime:.1f} hours</b>\nAlerts: <b>{alerts_sent}</b>"
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ===================== SCAN LOOP =====================
async def scan_loop():
    global total_pairs_scanned, last_heartbeat_time
    async with aiohttp.ClientSession() as session:
        while not shutdown_flag:
            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                await send_heartbeat()
                last_heartbeat_time = time.time()

            for network in NETWORKS:
                if shutdown_flag:
                    break
                logger.info(f"Scanning {network}...")
                pairs = await get_all_pairs(session, network)
                logger.info(f"Found {len(pairs)} pairs on {network}")
                for pair in pairs:
                    if shutdown_flag:
                        break
                    total_pairs_scanned += 1
                    alert = await evaluate_token(session, pair)
                    if alert:
                        await send_alert(alert)
                        await asyncio.sleep(1)
            await asyncio.sleep(SCAN_INTERVAL)

# ===================== STARTUP =====================
async def main_bot():
    global start_time, last_heartbeat_time
    start_time = time.time()
    last_heartbeat_time = start_time
    logger.info("🚀 Pump Bot Started (with caching)")
    await bot.send_message(chat_id=CHAT_ID, text="✅ <b>Pump Bot</b> has started successfully.\nCache enabled for Moralis (10 min TTL).", parse_mode=ParseMode.HTML)
    await scan_loop()

# ===================== ENTRY POINT =====================
if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda s, f: os._exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: os._exit(0))

    threading.Thread(target=lambda: asyncio.run(main_bot()), daemon=True).start()

    port = int(os.getenv("PORT", 10000))
    logger.info(f"FastAPI server running on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
