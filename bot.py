import asyncio
import aiohttp
import time
import os
import logging
import signal
from collections import deque
from typing import Dict, List, Optional
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from telegram.constants import ParseMode
from fastapi import FastAPI
import uvicorn

load_dotenv()

# ===================== CONFIG =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCANNER_API_KEY = os.getenv("SCANNER_API_KEY")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")
if not SCANNER_API_KEY:
    raise ValueError("Missing SCANNER_API_KEY in .env")

NETWORKS = ["bsc", "ethereum", "base"]
SCAN_INTERVAL = 30
HEARTBEAT_INTERVAL = 3600          # 1 hour

TOP10_THRESHOLD = 80.0
MIN_LIQUIDITY_USD = 30000.0        # $30k as you originally wanted
MIN_VOLUME_SPIKE_RATIO = 1.0       # 5m > 1h
MIN_VOLUME_RATIO_1H_24H = 0.20     # 1h > 20% of 24h

# ===================== GLOBALS =====================
seen_pairs = set()
holder_cache = {}                  # token -> last top10%
total_pairs_scanned = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False

# Rate limiter for explorer APIs
api_rate_limiter = asyncio.Semaphore(5)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("pump_bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)

# ===================== FASTAPI LIFESPAN =====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: launch bot as a background task
    logger.info("🚀 Lifespan startup: starting bot task...")
    bot_task = asyncio.create_task(main_bot())
    yield
    # Shutdown: cancel bot task gracefully
    logger.info("Shutdown signal received, cancelling bot task...")
    bot_task.cancel()
    await bot_task

app = FastAPI(lifespan=lifespan)

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
async def fetch_json(session: aiohttp.ClientSession, url: str, timeout=15):
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        return None

async def get_all_pairs(session: aiohttp.ClientSession, network: str) -> List[Dict]:
    url = f"https://api.dexscreener.com/latest/dex/search?q=/{network}/"
    data = await fetch_json(session, url)
    if not data or "pairs" not in data:
        return []
    pairs = data["pairs"]
    pairs.sort(key=lambda x: float(x.get("volume", {}).get("m5", 0)), reverse=True)
    return pairs[:300]

async def get_top10_percent(session: aiohttp.ClientSession, chain: str, token: str) -> float:
    base = {
        "bsc": "https://api.bscscan.com/api",
        "ethereum": "https://api.etherscan.io/api",
        "base": "https://api.basescan.org/api"
    }.get(chain)
    if not base:
        return 0.0

    async with api_rate_limiter:
        supply_url = f"{base}?module=stats&action=tokensupply&contractaddress={token}&apikey={SCANNER_API_KEY}"
        holder_url = f"{base}?module=token&action=tokenholderlist&contractaddress={token}&page=1&offset=10&apikey={SCANNER_API_KEY}"

        supply_data = await fetch_json(session, supply_url)
        if not supply_data or supply_data.get("status") != "1":
            return 0.0
        total_supply = float(supply_data.get("result", 0))
        if total_supply == 0:
            return 0.0

        holder_data = await fetch_json(session, holder_url)
        if not holder_data or holder_data.get("status") != "1":
            return 0.0

        holders = holder_data.get("result", [])
        top10_balance = sum(float(h.get("balance", 0)) for h in holders[:10])
        return (top10_balance / total_supply) * 100

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

    # Volume spike (OR condition)
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

    current_pct = await get_top10_percent(session, chain, token)
    if current_pct < TOP10_THRESHOLD:
        return None

    # Calculate accumulation (extra info)
    prev_pct = holder_cache.get(token, current_pct)
    accumulating = current_pct > prev_pct
    holder_cache[token] = current_pct

    return {
        "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
        "chain": chain.upper(),
        "liquidity": liquidity,
        "vol_5m": vol5,
        "vol_1h": vol1h,
        "vol_24h": vol24h,
        "spike_type": trigger,
        "top10_pct": round(current_pct, 2),
        "prev_top10_pct": round(prev_pct, 2) if prev_pct != current_pct else None,
        "accumulating": accumulating,
        "dex_url": f"https://dexscreener.com/{chain}/{pair_id}"
    }

# ===================== ALERT =====================
async def send_alert(alert: Dict):
    global alerts_sent
    alerts_sent += 1

    acc_text = " (accumulating)" if alert["accumulating"] else " (not accumulating)"
    if alert.get("prev_top10_pct"):
        acc_text += f" – prev: {alert['prev_top10_pct']}%"
    else:
        acc_text += " – first time seen"

    text = (
        f"🚨 <b>EARLY PUMP SIGNAL</b> 🚨\n\n"
        f"<b>{alert['token_name']}</b> ({alert['symbol']})\n"
        f"🔗 Chain: <b>{alert['chain']}</b>\n"
        f"💰 Liquidity: <b>${alert['liquidity']:,.0f}</b>\n"
        f"📊 5m Vol: <b>${alert['vol_5m']:,.0f}</b>  |  1h Vol: ${alert['vol_1h']:,.0f}  |  24h Vol: ${alert['vol_24h']:,.0f}\n"
        f"⚡ Spike: <b>{alert['spike_type']}</b>\n"
        f"👥 Top10 holders: <b>{alert['top10_pct']}%</b>{acc_text}\n\n"
        f"🔗 <a href='{alert['dex_url']}'>DexScreener</a>\n\n"
        f"<i>⚠️ Extremely high risk – verify you can sell before buying.</i>"
    )
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        logger.info(f"Alert sent → {alert['symbol']} (top10={alert['top10_pct']}%, accum={alert['accumulating']})")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ===================== HEARTBEAT =====================
async def send_heartbeat():
    uptime = (time.time() - start_time) / 3600
    text = (
        f"🫀 <b>Bot Heartbeat</b> 🫀\n"
        f"Uptime: <b>{uptime:.1f} hours</b>\n"
        f"Pairs scanned (total): <b>{total_pairs_scanned:,}</b>\n"
        f"Alerts sent: <b>{alerts_sent}</b>"
    )
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        logger.info("Heartbeat sent")
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")

# ===================== SCAN LOOP =====================
async def scan_loop():
    global total_pairs_scanned, last_heartbeat_time
    async with aiohttp.ClientSession() as session:
        while not shutdown_flag:
            cycle_start = time.time()

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
                    alert = await evaluate_token(session, pair)
                    if alert:
                        await send_alert(alert)
                        await asyncio.sleep(1)
                logger.info(f"Scanned {len(pairs)} pairs on {network}")

            elapsed = time.time() - cycle_start
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)

# ===================== BOT MAIN =====================
async def main_bot():
    global start_time, last_heartbeat_time
    start_time = time.time()
    last_heartbeat_time = start_time
    logger.info("🚀 Crime Pump Bot started (lifespan managed)")
    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "✅ <b>Crime Pump Bot</b> has started successfully.\n\n"
            f"<b>Filters:</b>\n"
            f"• Volume spike: 5m>1h OR 1h>20%24h\n"
            f"• Top10 holders > {TOP10_THRESHOLD}% (accumulation status extra)\n"
            f"• Min liquidity: ${MIN_LIQUIDITY_USD:,.0f}\n\n"
            f"Scanning ALL pairs on {', '.join(NETWORKS)}.\n"
            f"Heartbeat every hour."
        ),
        parse_mode=ParseMode.HTML
    )
    await scan_loop()

# ===================== ENTRY POINT =====================
if __name__ == "__main__":
    # Signal handlers for graceful exit
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal, exiting...")
        os._exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    port = int(os.getenv("PORT", 10000))
    logger.info(f"FastAPI server running on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
