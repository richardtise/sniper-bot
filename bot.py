import asyncio
import aiohttp
import time
import os
import logging
import signal
from collections import deque
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from telegram.constants import ParseMode
from fastapi import FastAPI
import uvicorn
import threading

load_dotenv()

# ---------- CONFIGURATION ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")

SCANNER_API_KEY = os.getenv("SCANNER_API_KEY")
if not SCANNER_API_KEY:
    raise ValueError("Missing SCANNER_API_KEY in .env")

NETWORKS = ["ethereum", "bsc", "base"]   # order: ethereum first for API key compatibility
SCAN_INTERVAL = 30                       # seconds between full scans
HEARTBEAT_INTERVAL = 3600                # 1 hour
TOP10_THRESHOLD = 80.0                   # require top10 > 80%
MIN_VOLUME_SPIKE_RATIO = 1.0             # 5m volume > 1h volume
MIN_LIQUIDITY_USD = 30000.0              # minimum $30k liquidity
SEEN_PAIRS_CLEANUP = 10000               # max unique pairs to remember

# Internal state
seen_pairs = set()
holder_cache = {}                        # token_address -> last_top10_percent
total_pairs_scanned = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False
hourly_pair_counts = deque()             # (timestamp, count)
last_scan_stats = {"total": 0, "per_network": {}}

# Rate limiter for explorer APIs
api_rate_limiter = asyncio.Semaphore(5)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("pump_bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)

# ---------- FASTAPI FOR RENDER KEEP-ALIVE ----------
app = FastAPI()

@app.get("/")
@app.get("/health")
async def health_check():
    uptime_sec = time.time() - start_time
    uptime_str = f"{int(uptime_sec//3600)}h {int((uptime_sec%3600)//60)}m"
    return {
        "status": "alive",
        "uptime": uptime_str,
        "total_pairs_scanned": total_pairs_scanned,
        "alerts_sent": alerts_sent,
        "time": datetime.now(timezone.utc).isoformat()
    }

# ---------- HTTP HELPER ----------
async def fetch_json(session: aiohttp.ClientSession, url: str, timeout=15):
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        logger.debug(f"Fetch error {url}: {e}")
    return None

# ---------- GET ALL PAIRS (NO AGE FILTER) ----------
async def get_all_pairs(session: aiohttp.ClientSession, network: str) -> list:
    """
    Fetch ALL pairs from DexScreener for a given network.
    Returns up to ~500 pairs (DexScreener limit). Sorted by 5m volume desc.
    """
    url = f"https://api.dexscreener.com/latest/dex/search?q=/{network}/"
    data = await fetch_json(session, url)
    if not data or "pairs" not in data:
        return []
    pairs = data["pairs"]
    # Sort by 5m volume (highest first) to catch active pumps early
    pairs.sort(key=lambda x: float(x.get("volume", {}).get("m5", 0)), reverse=True)
    return pairs

# ---------- VOLUME SPIKE CHECK ----------
def check_volume_spike(pair: Dict) -> Tuple[bool, float]:
    vol_m5 = float(pair.get("volume", {}).get("m5", 0))
    vol_h1 = float(pair.get("volume", {}).get("h1", 0))
    if vol_h1 == 0:
        return vol_m5 > 0, 999.0 if vol_m5 > 0 else 0.0
    ratio = vol_m5 / vol_h1
    return ratio > MIN_VOLUME_SPIKE_RATIO, ratio

# ---------- HOLDER CONCENTRATION (Single API Key) ----------
def get_explorer_base_url(chain: str) -> str:
    return {
        "ethereum": "https://api.etherscan.io/api",
        "bsc": "https://api.bscscan.com/api",
        "base": "https://api.basescan.org/api"
    }.get(chain)

async def get_top10_and_accumulation(session: aiohttp.ClientSession, chain: str, token: str) -> Tuple[float, bool]:
    """
    Returns (top10_percent, is_accumulating) where accumulating means
    top10% increased since last check.
    """
    base_url = get_explorer_base_url(chain)
    if not base_url:
        return 0.0, False

    async with api_rate_limiter:
        # Total supply
        supply_url = f"{base_url}?module=stats&action=tokensupply&contractaddress={token}&apikey={SCANNER_API_KEY}"
        supply_data = await fetch_json(session, supply_url)
        if not supply_data or supply_data.get("status") != "1":
            return 0.0, False
        total_supply = float(supply_data.get("result", 0))
        if total_supply == 0:
            return 0.0, False

        # Top 10 holders
        holder_url = f"{base_url}?module=token&action=tokenholderlist&contractaddress={token}&page=1&offset=10&apikey={SCANNER_API_KEY}"
        holder_data = await fetch_json(session, holder_url)
        if not holder_data or holder_data.get("status") != "1":
            return 0.0, False

        holders = holder_data.get("result", [])
        top10_balance = sum(float(h.get("balance", 0)) for h in holders)
        current_pct = (top10_balance / total_supply) * 100

        # Check accumulation
        prev_pct = holder_cache.get(token, current_pct)
        accumulating = current_pct > prev_pct
        holder_cache[token] = current_pct

        return current_pct, accumulating

# ---------- MAIN PUMP EVALUATION ----------
async def evaluate_token(session: aiohttp.ClientSession, pair: Dict) -> Optional[Dict]:
    chain = pair.get("chainId")
    token = pair.get("baseToken", {}).get("address")
    pair_id = pair.get("pairAddress")
    if not chain or not token or not pair_id:
        return None

    # Liquidity filter
    liquidity = float(pair.get("liquidity", {}).get("usd", 0))
    if liquidity < MIN_LIQUIDITY_USD:
        return None

    # Deduplicate
    if pair_id in seen_pairs:
        return None
    seen_pairs.add(pair_id)
    if len(seen_pairs) > SEEN_PAIRS_CLEANUP:
        seen_pairs.clear()

    # Signal 1: Volume spike (5m > 1h)
    spike, ratio = check_volume_spike(pair)
    if not spike:
        return None

    # Signal 2: Top10 > 80% and accumulating
    top10_pct, accumulating = await get_top10_and_accumulation(session, chain, token)
    if top10_pct <= TOP10_THRESHOLD:
        return None

    # All conditions met
    return {
        "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
        "address": token,
        "chain": chain.upper(),
        "liquidity_usd": liquidity,
        "volume_5m": float(pair.get("volume", {}).get("m5", 0)),
        "volume_1h": float(pair.get("volume", {}).get("h1", 0)),
        "spike_ratio": round(ratio, 2),
        "top10_pct": round(top10_pct, 2),
        "accumulating": accumulating,
        "dex_url": f"https://dexscreener.com/{chain}/{pair_id}"
    }

# ---------- TELEGRAM ALERT ----------
async def send_alert(alert: Dict):
    global alerts_sent
    alerts_sent += 1
    text = (
        f"🚨 <b>EARLY PUMP SIGNAL</b> 🚨\n\n"
        f"<b>{alert['token_name']}</b> ({alert['symbol']})\n"
        f"🔗 <b>{alert['chain']}</b>\n"
        f"💰 Liquidity: <b>${alert['liquidity_usd']:,.0f}</b>\n"
        f"📊 5m Vol: ${alert['volume_5m']:,.0f}  |  1h Vol: ${alert['volume_1h']:,.0f}\n"
        f"⚡ Volume spike: <b>{alert['spike_ratio']}x</b> (5m > 1h)\n\n"
        f"👥 Top10 holders: <b>{alert['top10_pct']}%</b>\n"
        f"📈 Accumulating: {'✅ YES' if alert['accumulating'] else '❌ NO'}\n\n"
        f"🔗 <a href='{alert['dex_url']}'>DexScreener</a>\n\n"
        f"<i>⚠️ Extremely high risk – verify you can sell before buying.</i>"
    )
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        logger.info(f"Alert sent for {alert['symbol']} on {alert['chain']}")
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

# ---------- HEARTBEAT ----------
async def send_heartbeat():
    global last_scan_stats, total_pairs_scanned, alerts_sent, hourly_pair_counts
    uptime_sec = time.time() - start_time
    uptime_hours = uptime_sec / 3600
    uptime_str = f"{int(uptime_sec//3600)}h {int((uptime_sec%3600)//60)}m"

    # Pairs scanned in last hour
    now = time.time()
    while hourly_pair_counts and hourly_pair_counts[0][0] < now - 3600:
        hourly_pair_counts.popleft()
    pairs_last_hour = sum(cnt for _, cnt in hourly_pair_counts)

    text = (
        f"💓 <b>Bot Heartbeat</b>\n\n"
        f"⏱ Uptime: {uptime_str} ({uptime_hours:.1f}h)\n"
        f"🔍 Total pairs scanned: <b>{total_pairs_scanned:,}</b>\n"
        f"📊 Pairs scanned (last hour): <b>{pairs_last_hour:,}</b>\n"
        f"🚨 Alerts sent: <b>{alerts_sent}</b>\n"
        f"🆕 Unique pairs in cache: <b>{len(seen_pairs):,}</b>\n"
        f"⚙️ Last scan: {last_scan_stats['total']} pairs\n"
        f"   └─ {', '.join([f'{net}: {last_scan_stats["per_network"].get(net,0)}' for net in NETWORKS])}\n"
        f"🔁 Scan interval: {SCAN_INTERVAL}s | Min liq: ${MIN_LIQUIDITY_USD:,.0f}"
    )
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        logger.info("Heartbeat sent")
    except TelegramError as e:
        logger.error(f"Heartbeat failed: {e}")

# ---------- SCAN LOOP (NO AGE FILTER) ----------
async def scan_loop():
    global total_pairs_scanned, last_scan_stats, hourly_pair_counts, last_heartbeat_time
    async with aiohttp.ClientSession() as session:
        while not shutdown_flag:
            scan_start = time.time()
            total_this_cycle = 0
            cycle_counts = {net: 0 for net in NETWORKS}

            for network in NETWORKS:
                try:
                    logger.info(f"Scanning {network} (all pairs)...")
                    pairs = await get_all_pairs(session, network)
                    # Process all pairs (no hard limit beyond DexScreener's ~500)
                    for pair in pairs:
                        if shutdown_flag:
                            break
                        cycle_counts[network] += 1
                        total_pairs_scanned += 1
                        total_this_cycle += 1
                        alert = await evaluate_token(session, pair)
                        if alert:
                            await send_alert(alert)
                            # Small delay to avoid Telegram flood
                            await asyncio.sleep(1)
                    logger.info(f"Scanned {cycle_counts[network]} pairs on {network}")
                except Exception as e:
                    logger.error(f"Error scanning {network}: {e}")
                    cycle_counts[network] = 0

            # Update stats
            last_scan_stats = {"total": total_this_cycle, "per_network": cycle_counts}
            hourly_pair_counts.append((time.time(), total_this_cycle))

            # Heartbeat
            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                await send_heartbeat()
                last_heartbeat_time = time.time()

            # Wait until next scan
            elapsed = time.time() - scan_start
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)

# ---------- BOT STARTUP ----------
async def startup_message():
    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"✅ <b>Crime Pump Bot Active</b>\n\n"
             f"<b>Filters:</b>\n"
             f"• 5m volume > 1h volume spike\n"
             f"• Top10 holders > {TOP10_THRESHOLD}% and accumulating\n"
             f"• Min liquidity: ${MIN_LIQUIDITY_USD:,.0f}\n\n"
             f"<b>Scanning ALL pairs</b> (no age limit) on Ethereum, BSC, Base.\n"
             f"Heartbeat every {HEARTBEAT_INTERVAL//60} minutes.",
        parse_mode=ParseMode.HTML
    )

# ---------- MAIN ASYNC BOT ----------
async def main_bot():
    global start_time, last_heartbeat_time
    start_time = time.time()
    last_heartbeat_time = start_time
    logger.info("🚀 Pump bot starting (full scan mode)")
    await startup_message()
    await scan_loop()

# ---------- RUN BOT IN BACKGROUND THREAD ----------
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(main_bot())
    loop.run_forever()

# ---------- SHUTDOWN HANDLER ----------
def shutdown_handler(sig, frame):
    global shutdown_flag
    logger.info("Shutdown signal received, stopping bot...")
    shutdown_flag = True
    # Give bot time to finish current scan
    time.sleep(5)
    os._exit(0)

# ---------- FASTAPI ENTRYPOINT ----------
if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Run FastAPI server (for Render)
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting FastAPI health server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
