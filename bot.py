import asyncio
import aiohttp
import time
import os
import logging
from contextlib import asynccontextmanager
from collections import deque
from typing import Dict, List, Optional
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from fastapi import FastAPI
import uvicorn

load_dotenv()

# ===================== CONFIG =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")

NETWORKS = ["bsc", "ethereum", "base"]
SCAN_INTERVAL = 60
HEARTBEAT_INTERVAL = 3600

TOP10_THRESHOLD = 75.0
MIN_LIQUIDITY_USD = 18000.0
MIN_VOLUME_SPIKE_RATIO = 0.8
MIN_VOLUME_RATIO_1H_24H = 0.15

# ===================== GLOBALS =====================
seen_pairs = set()
holder_cache = {}
total_pairs_scanned = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False
hourly_scan_records = deque(maxlen=100)

api_rate_limiter = asyncio.Semaphore(5)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("pump_bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)

# ===================== HELPER FUNCTIONS =====================
async def fetch_json(session: aiohttp.ClientSession, url: str, headers=None, timeout=15):
    async with api_rate_limiter:
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    logger.warning(f"Rate limit hit for {url}, sleeping 2s")
                    await asyncio.sleep(2)
                    return await fetch_json(session, url, headers, timeout)
        except Exception as e:
            logger.debug(f"Fetch error {url}: {e}")
        return None

async def get_all_pairs(session: aiohttp.ClientSession, network: str) -> List[Dict]:
    """Returns up to 300 actively trading pairs using DexScreener endpoints."""
    pairs = []
    seen = set()

    # 1. Trending pairs (highest 24h volume)
    url = f"https://api.dexscreener.com/latest/dex/trending?chain={network}"
    data = await fetch_json(session, url)
    if data and "pairs" in data:
        for p in data["pairs"][:150]:
            addr = p.get("pairAddress")
            if addr and addr not in seen:
                seen.add(addr)
                pairs.append(p)

    # 2. New pairs (recently created)
    url = f"https://api.dexscreener.com/latest/dex/new?chain={network}"
    data = await fetch_json(session, url)
    if data and "pairs" in data:
        for p in data["pairs"][:100]:
            addr = p.get("pairAddress")
            if addr and addr not in seen:
                seen.add(addr)
                pairs.append(p)

    # 3. Fallback: global top volume (filtered by chain) – only if we need more
    if len(pairs) < 200:
        url = "https://api.dexscreener.com/latest/dex/search?q="
        data = await fetch_json(session, url)
        if data and "pairs" in data:
            for p in data["pairs"]:
                if p.get("chainId") == network:
                    addr = p.get("pairAddress")
                    if addr and addr not in seen and len(pairs) < 300:
                        seen.add(addr)
                        pairs.append(p)

    pairs.sort(key=lambda x: float(x.get("volume", {}).get("m5", 0)), reverse=True)
    logger.info(f"Found {len(pairs)} pairs on {network}")
    return pairs[:300]

async def get_top10_percent(session: aiohttp.ClientSession, chain: str, token: str) -> float:
    if not MORALIS_API_KEY:
        return 0.0

    moralis_chain = {"bsc": "bsc", "ethereum": "eth", "base": "base"}.get(chain)
    if not moralis_chain:
        return 0.0

    cache_key = f"{moralis_chain}:{token}"
    now = time.time()

    if cache_key in holder_cache:
        pct, timestamp = holder_cache[cache_key]
        if now - timestamp < 600:  # 10 minutes
            logger.debug(f"Cache hit for {token} on {chain}: {pct:.2f}%")
            return pct

    url = f"https://deep-index.moralis.io/api/v2.2/erc20/{token}/owners?chain={moralis_chain}&order=DESC&limit=10"
    headers = {"X-API-Key": MORALIS_API_KEY}
    data = await fetch_json(session, url, headers=headers)

    if not data or "result" not in data:
        holder_cache[cache_key] = (0.0, now)
        return 0.0

    holders = data.get("result", [])
    total_supply = float(data.get("total_supply", 0))
    if total_supply == 0:
        holder_cache[cache_key] = (0.0, now)
        return 0.0

    top10_balance = sum(float(h.get("balance", 0)) for h in holders[:10])
    pct = (top10_balance / total_supply) * 100
    holder_cache[cache_key] = (pct, now)

    if len(holder_cache) > 1000:
        for k in list(holder_cache.keys()):
            if now - holder_cache[k][1] > 3600:
                del holder_cache[k]

    return pct

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

async def send_alert(alert: Dict):
    global alerts_sent
    alerts_sent += 1
    text = (f"🚨 <b>EARLY PUMP SIGNAL</b> 🚨\n\n"
            f"<b>{alert['token_name']}</b> ({alert['symbol']})\n"
            f"🔗 Chain: <b>{alert['chain']}</b>\n"
            f"💰 Liquidity: <b>${alert['liquidity']:,.0f}</b>\n"
            f"⚡ Spike: <b>{alert['spike_type']}</b>\n"
            f"👥 Top10: <b>{alert['top10_pct']}%</b>\n\n"
            f"🔗 <a href='{alert['dex_url']}'>DexScreener</a>")
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        logger.info(f"Alert sent → {alert['symbol']} (top10: {alert['top10_pct']}%)")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

async def send_heartbeat():
    uptime_hours = (time.time() - start_time) / 3600 if start_time > 0 else 0
    now = time.time()
    pairs_last_hour = sum(cnt for ts, cnt in hourly_scan_records if now - ts <= 3600)

    text = (f"🫀 <b>Bot Heartbeat</b> 🫀\n"
            f"Uptime: <b>{uptime_hours:.1f} hours</b>\n"
            f"Alerts sent: <b>{alerts_sent}</b>\n"
            f"Total pairs scanned: <b>{total_pairs_scanned:,}</b>\n"
            f"Pairs scanned (last hour): <b>{pairs_last_hour:,}</b>\n"
            f"Holder cache size: <b>{len(holder_cache)}</b>")
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        logger.info("Heartbeat sent")
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")

# ===================== BOT TASK =====================
async def bot_task():
    global start_time, last_heartbeat_time, total_pairs_scanned
    start_time = time.time()
    last_heartbeat_time = start_time
    logger.info("🚀 Bot task started")

    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=(f"✅ <b>Pump Bot</b> has started successfully.\n"
                  f"Scan interval: {SCAN_INTERVAL}s | Min liq: ${MIN_LIQUIDITY_USD:,.0f}\n"
                  f"Top10 threshold: >{TOP10_THRESHOLD}% | Cache TTL: 10 min"),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Startup message failed: {e}")

    async with aiohttp.ClientSession() as session:
        while not shutdown_flag:
            cycle_start = time.time()
            cycle_pairs = 0

            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                await send_heartbeat()
                last_heartbeat_time = time.time()

            for network in NETWORKS:
                if shutdown_flag:
                    break
                logger.info(f"Scanning {network}...")
                pairs = await get_all_pairs(session, network)
                for pair in pairs:
                    if shutdown_flag:
                        break
                    total_pairs_scanned += 1
                    cycle_pairs += 1
                    alert = await evaluate_token(session, pair)
                    if alert:
                        await send_alert(alert)
                        await asyncio.sleep(1)

            hourly_scan_records.append((cycle_start, cycle_pairs))
            await asyncio.sleep(SCAN_INTERVAL)

# ===================== FASTAPI LIFESPAN =====================
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
        logger.info("Bot task cancelled")
    logger.info("Bot shutdown complete")

app = FastAPI(lifespan=lifespan)

@app.get("/")
@app.get("/health")
async def health_check():
    uptime = (time.time() - start_time) / 3600 if start_time > 0 else 0
    return {
        "status": "alive",
        "uptime_hours": round(uptime, 2),
        "pairs_scanned": total_pairs_scanned,
        "alerts_sent": alerts_sent,
        "cache_size": len(holder_cache)
    }

# ===================== ENTRY POINT =====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting Pump Bot on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
