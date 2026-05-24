import asyncio
import aiohttp
import time
import os
import logging
from typing import Dict, Optional, Tuple
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from telegram.constants import ParseMode

load_dotenv()

# ---------- CONFIGURATION ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing TELEGRAM_TOKEN or CHAT_ID in .env")

ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY")
BSCSCAN_KEY = os.getenv("BSCSCAN_API_KEY")
BASESCAN_KEY = os.getenv("BASESCAN_API_KEY")
SCANNER_KEY = os.getenv("SCANNER_API_KEY")

NETWORKS = ["ethereum", "bsc", "base"]
SCAN_INTERVAL = 20
TOP10_THRESHOLD = 80.0
MIN_VOLUME_SPIKE_RATIO = 1.0
MIN_LIQUIDITY_USD = 30000.0          # <-- NEW

seen_pairs = set()
holder_cache = {}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)

# ---------- HTTP HELPER ----------
async def fetch_json(session: aiohttp.ClientSession, url: str, timeout=10):
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        logger.debug(f"Fetch error: {e}")
    return None

# ---------- GET ALL PAIRS ----------
async def get_all_pairs(session: aiohttp.ClientSession, network: str):
    url = f"https://api.dexscreener.com/latest/dex/search?q=/{network}/"
    data = await fetch_json(session, url)
    if not data or "pairs" not in data:
        return []
    pairs = data["pairs"]
    pairs.sort(key=lambda x: float(x.get("volume", {}).get("m5", 0)), reverse=True)
    return pairs

# ---------- VOLUME SPIKE ----------
def check_volume_spike(pair: Dict) -> Tuple[bool, float]:
    vol_m5 = float(pair.get("volume", {}).get("m5", 0))
    vol_h1 = float(pair.get("volume", {}).get("h1", 0))
    if vol_h1 == 0:
        return vol_m5 > 0, 999.0 if vol_m5 > 0 else 0.0
    ratio = vol_m5 / vol_h1
    return ratio > MIN_VOLUME_SPIKE_RATIO, ratio

# ---------- TOP 10 HOLDERS ----------
def get_api_key(chain: str) -> str:
    if chain == "ethereum":
        return ETHERSCAN_KEY or SCANNER_KEY
    elif chain == "bsc":
        return BSCSCAN_KEY or SCANNER_KEY
    elif chain == "base":
        return BASESCAN_KEY or SCANNER_KEY
    return SCANNER_KEY

async def get_top10_and_accumulation(session: aiohttp.ClientSession, chain: str, token: str) -> Tuple[float, bool]:
    api_key = get_api_key(chain)
    if not api_key:
        return 0.0, False

    base_urls = {
        "ethereum": "https://api.etherscan.io/api",
        "bsc": "https://api.bscscan.com/api",
        "base": "https://api.basescan.org/api"
    }
    base = base_urls.get(chain)
    if not base:
        return 0.0, False

    supply_url = f"{base}?module=stats&action=tokensupply&contractaddress={token}&apikey={api_key}"
    supply_data = await fetch_json(session, supply_url)
    if not supply_data or supply_data.get("status") != "1":
        return 0.0, False
    total_supply = float(supply_data.get("result", 0))
    if total_supply == 0:
        return 0.0, False

    holder_url = f"{base}?module=token&action=tokenholderlist&contractaddress={token}&page=1&offset=10&apikey={api_key}"
    holder_data = await fetch_json(session, holder_url)
    if not holder_data or holder_data.get("status") != "1":
        return 0.0, False

    holders = holder_data.get("result", [])
    top10_balance = sum(float(h.get("balance", 0)) for h in holders)
    current_pct = (top10_balance / total_supply) * 100

    prev_pct = holder_cache.get(token, current_pct)
    accumulating = current_pct > prev_pct
    holder_cache[token] = current_pct

    return current_pct, accumulating

# ---------- MAIN EVALUATION (WITH LIQUIDITY FILTER) ----------
async def evaluate_pair(session: aiohttp.ClientSession, pair: Dict) -> Optional[Dict]:
    chain = pair.get("chainId")
    token = pair.get("baseToken", {}).get("address")
    pair_id = pair.get("pairAddress")
    if not chain or not token or not pair_id:
        return None

    # Liquidity filter
    liquidity = float(pair.get("liquidity", {}).get("usd", 0))
    if liquidity < MIN_LIQUIDITY_USD:
        return None

    if pair_id in seen_pairs:
        return None
    seen_pairs.add(pair_id)
    if len(seen_pairs) > 5000:
        seen_pairs.clear()

    spike, ratio = check_volume_spike(pair)
    if not spike:
        return None

    top10_pct, accumulating = await get_top10_and_accumulation(session, chain, token)
    if top10_pct <= TOP10_THRESHOLD:
        return None

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
    text = (
        f"🚨 <b>EARLY PUMP SIGNAL</b> 🚨\n\n"
        f"<b>{alert['token_name']}</b> ({alert['symbol']})\n"
        f"🔗 <b>{alert['chain']}</b>\n"
        f"💰 Liquidity: <b>${alert['liquidity_usd']:,.0f}</b> (min $30k)\n"
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

# ---------- SCAN LOOP ----------
async def scan_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            for network in NETWORKS:
                try:
                    logger.info(f"Scanning {network} (liquidity ≥ ${MIN_LIQUIDITY_USD:,.0f})...")
                    pairs = await get_all_pairs(session, network)
                    for pair in pairs[:300]:
                        alert = await evaluate_pair(session, pair)
                        if alert:
                            await send_alert(alert)
                            await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error on {network}: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

# ---------- STARTUP ----------
async def startup():
    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"✅ <b>Pump Bot Active</b>\n\n"
             f"Filters:\n"
             f"• 5m volume > 1h volume spike\n"
             f"• Top10 holders > 80% and accumulating\n"
             f"• Minimum liquidity: ${MIN_LIQUIDITY_USD:,.0f}\n\n"
             f"Scanning all pairs on Ethereum, BSC, Base.",
        parse_mode=ParseMode.HTML
    )

# ---------- MAIN ----------
async def main():
    logger.info("Starting two‑signal pump bot with $30k liquidity filter")
    await startup()
    await scan_loop()

if __name__ == "__main__":
    asyncio.run(main())
