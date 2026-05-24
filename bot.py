import asyncio
import aiohttp
import time
import os
import logging
import signal
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from telegram.constants import ParseMode

load_dotenv()

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("crime_pump.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------- TELEGRAM ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ---------- API KEYS ----------
SCANNER_API_KEY = os.getenv("SCANNER_API_KEY")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY")
BASESCAN_API_KEY = os.getenv("BASESCAN_API_KEY")

# ---------- DETECTION SETTINGS ----------
NETWORKS = ["bsc", "ethereum", "base"]
SCAN_INTERVAL = 30
SEEN_PAIRS_CLEANUP = 2000               # <-- added missing constant

# Fresh pair detection
FRESH_MAX_AGE_MINUTES = 40
FRESH_MIN_LIQUIDITY_USD = 5000
FRESH_MIN_LIQUIDITY_USD_ULTRA_FRESH = 2000
FRESH_MAX_LIQUIDITY_USD = 250000
FRESH_MIN_5MIN_VOLUME = 15000
FRESH_MIN_5MIN_VOLUME_ULTRA_FRESH = 5000
FRESH_SPIKE_MULTIPLIER = 2.5

# Resurrection detection
RESURRECTION_ENABLED = True
RESURRECTION_MIN_AGE_HOURS = 1
RESURRECTION_MAX_LIQUIDITY_USD = 250000
RESURRECTION_MIN_5MIN_VOLUME = 12000
RESURRECTION_VOLUME_SPIKE_24H = 8.0
RESURRECTION_MIN_PRICE_GAIN_PCT = 80

# Holder concentration
MAX_TOP10_SUPPLY_PERCENT = 80
MIN_HOLDER_COUNT = 35

# Heartbeat
HEARTBEAT_INTERVAL = 3600               # 1 hour

# ---------- GLOBALS ----------
seen_pairs = set()
api_rate_limiter = asyncio.Semaphore(3)
shutdown_flag = False
start_time = 0.0
last_heartbeat_time = 0.0

# ---------- HELPER: FETCH JSON ----------
async def fetch_json(session: aiohttp.ClientSession, url: str, timeout=15):
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception as e:
        logger.debug(f"Fetch error {url}: {e}")
        return None

# ---------- DEX PAIRS ----------
async def get_new_pairs(session: aiohttp.ClientSession, network: str) -> List[Dict]:
    url = f"https://api.dexscreener.com/latest/dex/search?q=/{network}/"
    data = await fetch_json(session, url)
    if not data or "pairs" not in data:
        return []
    pairs = data["pairs"]
    pairs.sort(key=lambda x: x.get("pairCreatedAt") or x.get("createdAt", 0), reverse=True)
    return pairs[:25]

# ---------- HOLDER CONCENTRATION ----------
def get_api_key(chain: str) -> str:
    if chain == "bsc":
        return BSCSCAN_API_KEY or SCANNER_API_KEY
    elif chain == "ethereum":
        return ETHERSCAN_API_KEY or SCANNER_API_KEY
    elif chain == "base":
        return BASESCAN_API_KEY or SCANNER_API_KEY
    return SCANNER_API_KEY

async def get_token_supply_and_holders(session: aiohttp.ClientSession, chain: str, token_address: str) -> Tuple[float, int]:
    api_key = get_api_key(chain)
    if not api_key:
        logger.warning(f"No API key for {chain}")
        return 0.0, 0

    async with api_rate_limiter:
        base_urls = {
            "ethereum": "https://api.etherscan.io/api",
            "bsc": "https://api.bscscan.com/api",
            "base": "https://api.basescan.org/api"
        }
        base = base_urls.get(chain)
        if not base:
            return 0.0, 0

        holder_url = f"{base}?module=token&action=tokenholderlist&contractaddress={token_address}&page=1&offset=20&apikey={api_key}"
        supply_url = f"{base}?module=stats&action=tokensupply&contractaddress={token_address}&apikey={api_key}"

        await asyncio.sleep(0.35)

        supply_data = await fetch_json(session, supply_url)
        total_supply = float(supply_data.get("result", 0)) if supply_data and supply_data.get("status") == "1" else 0
        if total_supply == 0:
            return 0.0, 0

        holders_data = await fetch_json(session, holder_url)
        if not holders_data or holders_data.get("status") != "1":
            return 0.0, 0

        holders = holders_data.get("result", [])
        if not holders:
            return 0.0, 0

        top10_balance = sum(float(h.get("balance", 0)) for h in holders[:10])
        top10_percent = (top10_balance / total_supply) * 100
        return round(top10_percent, 2), len(holders)

# ---------- RISK ASSESSMENT ----------
async def assess_crime_pump_risk(session: aiohttp.ClientSession, pair: Dict) -> Optional[Dict]:
    try:
        chain = pair.get("chainId", "")
        token_addr = pair.get("baseToken", {}).get("address", "")
        if not chain or not token_addr:
            return None

        liquidity = float(pair.get("liquidity", {}).get("usd", 0))
        vol_5m = float(pair.get("volume", {}).get("m5", 0))
        vol_1h = float(pair.get("volume", {}).get("h1", 0))
        vol_24h = float(pair.get("volume", {}).get("h24", 0))
        price_change_h1 = float(pair.get("priceChange", {}).get("h1", 0))

        created_at = pair.get("pairCreatedAt") or pair.get("createdAt", 0)
        age_min = max(0, (time.time() * 1000 - created_at) / 60000) if created_at else 999

        # Holder concentration
        top10_pct, holder_count = await get_token_supply_and_holders(session, chain, token_addr)
        if top10_pct <= MAX_TOP10_SUPPLY_PERCENT or holder_count < MIN_HOLDER_COUNT:
            logger.debug(f"SKIP {token_addr}: top10={top10_pct}% (need >{MAX_TOP10_SUPPLY_PERCENT}), holders={holder_count} (need >{MIN_HOLDER_COUNT})")
            return None

        # ----- FRESH PUMP (with ultra-fresh overrides) -----
        if age_min <= FRESH_MAX_AGE_MINUTES:
            # Ultra-fresh (<5 min) uses lower thresholds
            if age_min < 5:
                min_liq = FRESH_MIN_LIQUIDITY_USD_ULTRA_FRESH
                min_vol = FRESH_MIN_5MIN_VOLUME_ULTRA_FRESH
            else:
                min_liq = FRESH_MIN_LIQUIDITY_USD
                min_vol = FRESH_MIN_5MIN_VOLUME

            # Handle brand new pair (vol_1h == 0)
            if vol_1h == 0:
                spike_ok = True
            else:
                spike_ok = (vol_5m / vol_1h) >= FRESH_SPIKE_MULTIPLIER

            if (min_liq <= liquidity <= FRESH_MAX_LIQUIDITY_USD and
                vol_5m >= min_vol and spike_ok):
                return {
                    "type": "fresh",
                    "chain": chain,
                    "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
                    "token_symbol": pair.get("baseToken", {}).get("symbol", "???"),
                    "token_address": token_addr,
                    "pair_address": pair.get("pairAddress", ""),
                    "liquidity_usd": liquidity,
                    "vol_5m": vol_5m,
                    "vol_spike": vol_5m / vol_1h if vol_1h else 999,
                    "top10_pct": top10_pct,
                    "holder_count": holder_count,
                    "age_min": age_min,
                    "dex_url": f"https://dexscreener.com/{chain}/{pair.get('pairAddress', token_addr)}",
                }

        # ----- RESURRECTION PUMP -----
        if RESURRECTION_ENABLED and age_min >= (RESURRECTION_MIN_AGE_HOURS * 60):
            if vol_24h <= 0:
                return None
            expected_5m_avg = vol_24h / 288.0
            spike_vs_24h = vol_5m / expected_5m_avg if expected_5m_avg > 0 else 0
            if (liquidity <= RESURRECTION_MAX_LIQUIDITY_USD and
                vol_5m >= RESURRECTION_MIN_5MIN_VOLUME and
                spike_vs_24h >= RESURRECTION_VOLUME_SPIKE_24H and
                price_change_h1 >= RESURRECTION_MIN_PRICE_GAIN_PCT):
                return {
                    "type": "resurrection",
                    "chain": chain,
                    "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
                    "token_symbol": pair.get("baseToken", {}).get("symbol", "???"),
                    "token_address": token_addr,
                    "pair_address": pair.get("pairAddress", ""),
                    "liquidity_usd": liquidity,
                    "vol_5m": vol_5m,
                    "vol_spike": spike_vs_24h,
                    "top10_pct": top10_pct,
                    "holder_count": holder_count,
                    "age_min": age_min,
                    "price_change_h1": price_change_h1,
                    "dex_url": f"https://dexscreener.com/{chain}/{pair.get('pairAddress', token_addr)}",
                }
        return None
    except Exception as e:
        logger.error(f"Assessment error: {e}", exc_info=True)
        return None

# ---------- TELEGRAM ALERT (HTML) ----------
async def send_alert(alert: Dict):
    if alert["type"] == "fresh":
        text = (
            f"🚨 <b>FRESH CRIME PUMP (New Pair)</b> 🚨\n\n"
            f"<b>{alert['token_name']}</b> ({alert['token_symbol']})\n"
            f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n"
            f"💧 Liquidity: <b>${alert['liquidity_usd']:,.0f}</b>\n"
            f"📊 5m Volume: <b>${alert['vol_5m']:,.0f}</b>\n"
            f"📈 Spike (5m/1h): <b>{alert['vol_spike']:.1f}x</b>\n"
            f"👥 Top10 Holders: <b>{alert['top10_pct']:.1f}%</b>\n"
            f"📋 Holders: <b>{alert['holder_count']}</b>\n"
            f"⏱ Age: <b>{alert['age_min']:.1f} min</b>\n\n"
            f"⚠️ Extreme insider control + early volume spike.\n"
            f"🚫 Do NOT buy – classic coordinated pump.\n\n"
            f"<a href='{alert['dex_url']}'>DexScreener</a>"
        )
    else:  # resurrection
        text = (
            f"⚠️ <b>RESURRECTION PUMP (Dead Token Waking Up)</b> ⚠️\n\n"
            f"<b>{alert['token_name']}</b> ({alert['token_symbol']})\n"
            f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n"
            f"💧 Liquidity: <b>${alert['liquidity_usd']:,.0f}</b>\n"
            f"📊 5m Volume: <b>${alert['vol_5m']:,.0f}</b>\n"
            f"📈 Spike (5m vs 24h avg): <b>{alert['vol_spike']:.1f}x</b>\n"
            f"📊 Price Change (1h): <b>+{alert['price_change_h1']:.0f}%</b>\n"
            f"👥 Top10 Holders: <b>{alert['top10_pct']:.1f}%</b>\n"
            f"📋 Holders: <b>{alert['holder_count']}</b>\n"
            f"⏱ Pair Age: <b>{alert['age_min']:.0f} min</b> ({alert['age_min']/60:.1f} hours)\n\n"
            f"💀 This token was dead but is suddenly pumping.\n"
            f"Insider control remains high → likely exit scam.\n\n"
            f"<a href='{alert['dex_url']}'>DexScreener</a>"
        )

    for attempt in range(3):
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            logger.info(f"Alert sent: {alert['token_symbol']} ({alert['type']})")
            return
        except TelegramError as e:
            logger.warning(f"Telegram attempt {attempt+1} failed: {e}")
            if attempt == 2:
                logger.error(f"Failed to send alert after 3 attempts: {alert['token_symbol']}")
            await asyncio.sleep(2 ** attempt)

# ---------- HEARTBEAT ----------
async def send_heartbeat():
    uptime_hours = (time.time() - start_time) / 3600
    text = (
        f"💚 <b>Bot Heartbeat</b> 💚\n\n"
        f"⏱ Uptime: <b>{uptime_hours:.1f} hours</b>\n"
        f"📊 Pairs scanned this session: <b>{len(seen_pairs)}</b>\n"
        f"🔄 Scan interval: <b>{SCAN_INTERVAL}s</b>\n"
        f"🤖 Status: <b>ACTIVE</b>"
    )
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        logger.info("Heartbeat sent")
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")

# ---------- STARTUP MESSAGE ----------
async def send_startup_message():
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "✅ <b>Crime Pump Detector</b> started\n\n"
                f"📊 Chains: {', '.join(NETWORKS)}\n"
                f"🔄 Scan every {SCAN_INTERVAL}s\n"
                f"💚 Heartbeat every {HEARTBEAT_INTERVAL//3600}h\n"
                f"🔍 Fresh pump: top10 > {MAX_TOP10_SUPPLY_PERCENT}%, spike > {FRESH_SPIKE_MULTIPLIER}x\n"
                f"⚡ Ultra-fresh (<5 min): liq ≥${FRESH_MIN_LIQUIDITY_USD_ULTRA_FRESH}, vol ≥${FRESH_MIN_5MIN_VOLUME_ULTRA_FRESH}\n"
                f"⚰️ Resurrection: {'ON' if RESURRECTION_ENABLED else 'OFF'}"
            ),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Startup message failed: {e}")

# ---------- SCAN LOOP ----------
async def scan_loop():
    global last_heartbeat_time
    async with aiohttp.ClientSession() as session:
        while not shutdown_flag:
            # Heartbeat
            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                await send_heartbeat()
                last_heartbeat_time = time.time()

            for network in NETWORKS:
                if shutdown_flag:
                    break
                logger.info(f"Scanning {network}...")
                pairs = await get_new_pairs(session, network)
                for pair in pairs[:20]:
                    if shutdown_flag:
                        break
                    pair_id = pair.get("pairAddress")
                    if pair_id in seen_pairs:
                        continue
                    seen_pairs.add(pair_id)
                    if len(seen_pairs) > SEEN_PAIRS_CLEANUP:
                        seen_pairs.clear()
                    alert = await assess_crime_pump_risk(session, pair)
                    if alert:
                        await send_alert(alert)
            await asyncio.sleep(SCAN_INTERVAL)

# ---------- GRACEFUL SHUTDOWN ----------
async def shutdown(session):
    global shutdown_flag
    logger.info("Shutdown signal received, cleaning up...")
    shutdown_flag = True
    await session.close()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Shutdown complete.")

def handle_signals(session):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(session)))

# ---------- MAIN ----------
async def main():
    global start_time, last_heartbeat_time
    start_time = time.time()
    last_heartbeat_time = start_time

    logger.info("🚀 Crime Pump Detector starting...")
    await send_startup_message()
    async with aiohttp.ClientSession() as session:
        handle_signals(session)
        await scan_loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, exiting.")
