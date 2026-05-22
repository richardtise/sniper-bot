import requests
import time
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import os
import telebot

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
SCANNER_API_KEY = os.getenv('SCANNER_API_KEY')

bot = telebot.TeleBot(TELEGRAM_TOKEN)

CHAINS = ["bsc", "ethereum", "base"]
SPIKE_MULTIPLIER = 8.0
MIN_5MIN_VOLUME = 10000
MIN_LIQUIDITY = 30000
ALERT_COOLDOWN_MIN = 45

seen_pairs = {}

def fetch_hot_pairs():
    try:
        resp = requests.get("https://api.dexscreener.com/latest/dex/search?q=", timeout=15)
        resp.raise_for_status()
        return resp.json().get("pairs", [])
    except Exception as e:
        logger.warning(f"Fetch error: {e}")
        return []

def volume_spike_detected(pair):
    vol = pair.get("volume", {})
    v5 = float(vol.get("m5") or 0)
    v1h = float(vol.get("h1") or 0)
    
    if v5 < MIN_5MIN_VOLUME:
        return False
    if v1h <= 0:
        return v5 > 20000
    return v5 > SPIKE_MULTIPLIER * (v1h / 12.0)

def send_telegram_alert(pair):
    chain = pair["chainId"]
    base = pair.get("baseToken", {})
    msg = f"""🚨 *POTENTIAL RUNNER DETECTED!*

🔥 {base.get('name')} ({base.get('symbol')})
🌐 Chain: {chain.upper()}
💰 5min Volume: ${pair.get('volume', {}).get('m5', 0):,.0f}
📊 Liquidity: ${pair.get('liquidity', {}).get('usd', 0):,.0f}

🔗 https://dexscreener.com/{chain}/{pair.get('pairAddress', '')}
"""
    try:
        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
        logger.info(f"Alert sent for {base.get('symbol')}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ===================== MAIN LOOP =====================
logger.info("🚀 Sniper Bot Started (Strict Mode - Only Potential Runners)")

while True:
    pairs = fetch_hot_pairs()
    now = datetime.now(timezone.utc)

    # Cleanup
    for k in list(seen_pairs.keys()):
        if (now - seen_pairs[k]) >= timedelta(hours=3):
            del seen_pairs[k]

    for pair in pairs:
        if pair.get("chainId") not in CHAINS:
            continue
        if pair.get("liquidity", {}).get("usd", 0) < MIN_LIQUIDITY:
            continue
        if not volume_spike_detected(pair):
            continue

        pair_addr = pair.get("pairAddress")
        if not pair_addr:
            continue
        if pair_addr in seen_pairs and (now - seen_pairs[pair_addr]) < timedelta(minutes=ALERT_COOLDOWN_MIN):
            continue

        send_telegram_alert(pair)
        seen_pairs[pair_addr] = now
        logger.info(f"🚨 Strong signal on {pair.get('baseToken', {}).get('symbol')}")

    logger.info(f"Scanned {len(pairs)} pairs")
    time.sleep(60)



