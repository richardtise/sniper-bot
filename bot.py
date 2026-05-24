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
    handlers=[logging.FileHandler("crime_pump.log"), logging.StreamHandler()]
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

# ---------- DETECTION SETTINGS (Old Token Resurrection) ----------
NETWORKS = ["bsc", "ethereum", "base"]
SCAN_INTERVAL = 20                       # scan every 20 seconds
SEEN_PAIRS_CLEANUP = 2000

# Old token resurrection (no age limit – any pair older than 1 hour)
RESURRECTION_MIN_AGE_HOURS = 1           # ignore pairs younger than 1 hour
RESURRECTION_MAX_LIQUIDITY_USD = 250000  # still avoid huge liquidity
RESURRECTION_MIN_5MIN_VOLUME = 8000      # minimum 5m volume to consider
RESURRECTION_VOLUME_SPIKE_24H = 3.0      # 3x spike vs 24h average (was 8x)
RESURRECTION_BUY_TX_SPIKE = 2.5          # 2.5x increase in buy tx count vs 24h avg

# Insider concentration (main filter)
MAX_TOP10_SUPPLY_PERCENT = 75            # top10 >75% = insider controlled
MIN_HOLDER_COUNT = 25                    # ignore tokens with very few holders

# Heartbeat
HEARTBEAT_INTERVAL = 3600

# ---------- GLOBALS ----------
seen_pairs = set()
api_rate_limiter = asyncio.Semaphore(3)
shutdown_flag = False
start_time = 0.0
last_heartbeat_time = 0.0

# ---------- FETCH & DEX ----------
async def fetch_json(session: aiohttp.ClientSession, url: str, timeout=15):
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception:
        return None

async def get_all_pairs(session: aiohttp.ClientSession, network: str) -> List[Dict]:
    """Fetch all trading pairs for a network (not just new)."""
    url = f"https://api.dexscreener.com/latest/dex/search?q=/{network}/"
    data = await fetch_json(session, url)
    if not data or "pairs" not in data:
        return []
    return data["pairs"]

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
        return 0.0, 0
    async with api_rate_limiter:
        base_urls = {"ethereum": "https://api.etherscan.io/api", "bsc": "https://api.bscscan.com/api", "base": "https://api.basescan.org/api"}
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
        top10_balance = sum(float(h.get("balance", 0)) for h in holders[:10])
        top10_percent = (top10_balance / total_supply) * 100 if total_supply else 0
        return round(top10_percent, 2), len(holders)

# ---------- VOLUME & TX SPIKE DETECTION ----------
def get_volume_and_tx_spikes(pair: Dict) -> Tuple[float, float]:
    """
    Returns (volume_spike_vs_24h, buy_tx_spike_vs_24h)
    Volume spike = 5m volume / (24h volume / 288)
    Buy tx spike = 5m buys / (24h buys / 288)
    """
    vol_5m = float(pair.get("volume", {}).get("m5", 0))
    vol_24h = float(pair.get("volume", {}).get("h24", 0))
    tx_5m = pair.get("txns", {}).get("m5", {})
    buys_5m = float(tx_5m.get("buys", 0))
    tx_24h = pair.get("txns", {}).get("h24", {})
    buys_24h = float(tx_24h.get("buys", 0))

    if vol_24h <= 0:
        vol_spike = 999 if vol_5m > 0 else 0
    else:
        expected_5m_vol = vol_24h / 288.0
        vol_spike = vol_5m / expected_5m_vol if expected_5m_vol > 0 else 0

    if buys_24h <= 0:
        buy_spike = 999 if buys_5m > 0 else 0
    else:
        expected_5m_buys = buys_24h / 288.0
        buy_spike = buys_5m / expected_5m_buys if expected_5m_buys > 0 else 0

    return vol_spike, buy_spike

# ---------- RISK ASSESSMENT (Old Tokens Only) ----------
async def assess_old_token_risk(session: aiohttp.ClientSession, pair: Dict) -> Optional[Dict]:
    try:
        chain = pair.get("chainId", "")
        token_addr = pair.get("baseToken", {}).get("address", "")
        if not chain or not token_addr:
            return None

        liquidity = float(pair.get("liquidity", {}).get("usd", 0))
        if liquidity > RESURRECTION_MAX_LIQUIDITY_USD or liquidity < 1000:
            return None

        vol_5m = float(pair.get("volume", {}).get("m5", 0))
        if vol_5m < RESURRECTION_MIN_5MIN_VOLUME:
            return None

        # Age check – only old tokens (pair created > 1 hour ago)
        created_at = pair.get("pairCreatedAt") or pair.get("createdAt", 0)
        if created_at:
            age_min = (time.time() * 1000 - created_at) / 60000
            if age_min < (RESURRECTION_MIN_AGE_HOURS * 60):
                return None
        else:
            # No creation time, assume it's old enough
            pass

        # Holder concentration (insider control)
        top10_pct, holder_count = await get_token_supply_and_holders(session, chain, token_addr)
        if top10_pct <= MAX_TOP10_SUPPLY_PERCENT or holder_count < MIN_HOLDER_COUNT:
            logger.debug(f"SKIP old token {token_addr}: top10={top10_pct}% (>{MAX_TOP10_SUPPLY_PERCENT} needed), holders={holder_count}")
            return None

        # Volume and buy transaction spikes
        vol_spike, buy_spike = get_volume_and_tx_spikes(pair)
        if vol_spike < RESURRECTION_VOLUME_SPIKE_24H and buy_spike < RESURRECTION_BUY_TX_SPIKE:
            # Neither volume nor buy activity is unusually high
            return None

        # Trigger alert – no price check
        return {
            "type": "resurrection",
            "chain": chain,
            "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
            "token_symbol": pair.get("baseToken", {}).get("symbol", "???"),
            "token_address": token_addr,
            "liquidity_usd": liquidity,
            "vol_5m": vol_5m,
            "vol_spike": vol_spike,
            "buy_spike": buy_spike,
            "top10_pct": top10_pct,
            "holder_count": holder_count,
            "age_hours": age_min / 60 if created_at else 0,
            "dex_url": f"https://dexscreener.com/{chain}/{pair.get('pairAddress', token_addr)}",
        }
    except Exception as e:
        logger.error(f"Assessment error: {e}")
        return None

# ---------- TELEGRAM ALERT ----------
async def send_alert(alert: Dict):
    text = (
        f"⚠️ <b>OLD TOKEN RESURRECTION PUMP (Early Signal)</b> ⚠️\n\n"
        f"<b>{alert['token_name']}</b> ({alert['token_symbol']})\n"
        f"🔗 Chain: <b>{alert['chain'].upper()}</b>\n"
        f"💧 Liquidity: <b>${alert['liquidity_usd']:,.0f}</b>\n"
        f"📊 5m Volume: <b>${alert['vol_5m']:,.0f}</b>\n"
        f"📈 Volume Spike (5m vs 24h avg): <b>{alert['vol_spike']:.1f}x</b>\n"
        f"🛒 Buy TX Spike (5m vs 24h avg): <b>{alert['buy_spike']:.1f}x</b>\n"
        f"👥 Top10 Holders: <b>{alert['top10_pct']:.1f}%</b>\n"
        f"📋 Holders: <b>{alert['holder_count']}</b>\n"
        f"⏱ Pair Age: <b>{alert['age_hours']:.1f} hours</b>\n\n"
        f"⚠️ Dead token with high insider concentration is showing abnormal activity.\n"
        f"🚫 Likely coordinated pump – do NOT buy.\n\n"
        f"<a href='{alert['dex_url']}'>DexScreener</a>"
    )
    for attempt in range(3):
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            logger.info(f"Alert sent: {alert['token_symbol']}")
            return
        except TelegramError as e:
            logger.warning(f"Attempt {attempt+1} failed: {e}")
            await asyncio.sleep(2 ** attempt)

# ---------- SCAN LOOP (All pairs, not just new) ----------
async def scan_loop():
    global last_heartbeat_time
    async with aiohttp.ClientSession() as session:
        while not shutdown_flag:
            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                await send_heartbeat()
                last_heartbeat_time = time.time()
            for network in NETWORKS:
                if shutdown_flag:
                    break
                logger.info(f"Scanning {network} for old token pumps...")
                pairs = await get_all_pairs(session, network)
                # We need to scan many pairs – but to avoid rate limits, only check top pairs by volume? 
                # Better: sort by 5m volume descending and check top 200.
                pairs.sort(key=lambda x: float(x.get("volume", {}).get("m5", 0)), reverse=True)
                for pair in pairs[:150]:   # check 150 highest 5m volume pairs
                    if shutdown_flag:
                        break
                    pair_id = pair.get("pairAddress")
                    if pair_id in seen_pairs:
                        continue
                    seen_pairs.add(pair_id)
                    if len(seen_pairs) > SEEN_PAIRS_CLEANUP:
                        seen_pairs.clear()
                    alert = await assess_old_token_risk(session, pair)
                    if alert:
                        await send_alert(alert)
            await asyncio.sleep(SCAN_INTERVAL)

# ---------- HEARTBEAT, STARTUP, SHUTDOWN ----------
async def send_heartbeat():
    uptime = (time.time() - start_time) / 3600
    text = f"💚 <b>Bot Heartbeat</b>\n\nUptime: {uptime:.1f}h\nPairs scanned: {len(seen_pairs)}\nStatus: ACTIVE"
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")

async def send_startup_message():
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"✅ <b>Old Token Pump Detector</b> started\n\nScanning for resurrection pumps on old coins.\nTrigger: volume spike >{RESURRECTION_VOLUME_SPIKE_24H}x OR buy spike >{RESURRECTION_BUY_TX_SPIKE}x, top10 holders >{MAX_TOP10_SUPPLY_PERCENT}%.\nNo price delay – alerts on first abnormal activity.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Startup failed: {e}")

async def shutdown(session):
    global shutdown_flag
    logger.info("Shutting down...")
    shutdown_flag = True
    await session.close()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

def handle_signals(session):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(session)))

async def main():
    global start_time, last_heartbeat_time
    start_time = time.time()
    last_heartbeat_time = start_time
    logger.info("Starting Old Token Resurrection Pump Detector")
    await send_startup_message()
    async with aiohttp.ClientSession() as session:
        handle_signals(session)
        await scan_loop()

if __name__ == "__main__":
    asyncio.run(main())
