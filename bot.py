import os
import re
import requests
import time
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import telebot

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
SCANNER_API_KEY = os.getenv('SCANNER_API_KEY')

bot = telebot.TeleBot(TELEGRAM_TOKEN)

CHAINS = ["bsc", "ethereum", "base"]
CHAIN_IDS = {"bsc": 56, "ethereum": 1, "base": 8453}

SPIKE_MULTIPLIER = 5.0
MIN_5MIN_VOL = 150000
MIN_LIQUIDITY_USD = 30000
MAX_WHALE_PCT = 75
ALERT_COOLDOWN_MIN = 25
CHECK_INTERVAL_SEC = 40
HEARTBEAT_INTERVAL_SEC = 3600

seen_pairs = {}

def escape_markdown(text):
    if not isinstance(text, str):
        return str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def fetch_pairs():
    queries = ["WBNB", "USDT", "ETH"]
    all_pairs = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for q in queries:
        try:
            url = f"https://api.dexscreener.com/latest/dex/search?q={q}"
            resp = requests.get(url, timeout=15, headers=headers)
            resp.raise_for_status()
            pairs = resp.json().get("pairs", [])
            all_pairs.extend(pairs)
            time.sleep(0.8)
        except Exception as e:
            logger.error(f"DexScreener query '{q}' failed: {e}")

    # Deduplicate
    unique = {}
    for p in all_pairs:
        addr = p.get("pairAddress")
        if addr and addr not in unique:
            unique[addr] = p
    logger.info(f"✅ Fetched {len(all_pairs)} pairs, {len(unique)} unique")
    return list(unique.values())


def volume_spike_detected(pair):
    vol = pair.get("volume", {})
    v5 = float(vol.get("m5") or 0)
    v1h = float(vol.get("h1") or 0)

    if v5 < MIN_5MIN_VOL:
        return False
    if v1h <= 0:
        return v5 >= MIN_5MIN_VOL * 1.8
    expected = v1h / 12.0
    return v5 > SPIKE_MULTIPLIER * expected


def check_whale_concentration(token_address, chain):
    if not SCANNER_API_KEY or not token_address:
        return True, "No API key / invalid address"

    chain_id = CHAIN_IDS.get(chain)
    if not chain_id:
        return True, "Unknown chain"

    try:
        params = {
            "chainid": chain_id,
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": token_address,
            "page": 1,
            "offset": 10,
            "apikey": SCANNER_API_KEY
        }
        resp = requests.get(SCANNER_V2_URL, params=params, timeout=12)
        holders = resp.json().get("result", [])

        top10_raw = sum(int(h.get("TokenHolderQuantity", 0)) for h in holders)

        ts_params = {**params, "action": "totalsupply"}
        ts_resp = requests.get(SCANNER_V2_URL, params=ts_params, timeout=10)
        total_raw = int(ts_resp.json().get("result", 1))

        decimals = 18
        try:
            info_params = {**params, "action": "tokeninfo"}
            info_resp = requests.get(SCANNER_V2_URL, params=info_params, timeout=10)
            result = info_resp.json().get("result")
            if isinstance(result, list) and result:
                decimals = int(result[0].get("decimal", 18))
        except:
            pass

        top10 = top10_raw / (10 ** decimals)
        total = total_raw / (10 ** decimals)
        concentration = (top10 / total * 100) if total > 0 else 0

        is_low = concentration <= MAX_WHALE_PCT
        return is_low, f"{concentration:.1f}% in top 10"

    except Exception as e:
        logger.error(f"Whale check failed: {e}")
        return True, "Check failed"


def send_telegram_alert(pair, whale_info):
    chain = pair.get("chainId")
    base = pair.get("baseToken", {})
    name = escape_markdown(base.get("name", "Unknown"))
    symbol = escape_markdown(base.get("symbol", "???"))
    v5 = pair.get("volume", {}).get("m5", 0)
    liq = pair.get("liquidity", {}).get("usd", 0)
    pair_addr = pair.get("pairAddress", "")

    msg = f"""🚨 *EARLY VOLUME PICKUP!*

🔥 {name} ({symbol})
🌐 Chain: {chain.upper()}
💰 5min Vol: ${v5:,.0f}
📊 Liquidity: ${liq:,.0f}
🐳 {whale_info}

🔗 [DexScreener](https://dexscreener.com/{chain}/{pair_addr})
"""
    try:
        bot.send_message(CHAT_ID, msg, parse_mode='Markdown', disable_web_page_preview=True)
        logger.info(f"✅ ALERT SENT → {symbol}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")


# ===================== MAIN LOOP =====================
logger.info("🚀 Sniper Bot Started – Early Volume + Low Whale Concentration")

# Startup notification
try:
    bot.send_message(CHAT_ID, f"✅ Bot started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
except:
    pass

last_heartbeat = datetime.now(timezone.utc)

while True:
    pairs = fetch_pairs()
    now = datetime.now(timezone.utc)

    seen_pairs = {k: v for k, v in seen_pairs.items() if (now - v) < timedelta(hours=2)}

    alerts_sent = 0

    for pair in pairs:
        chain = pair.get("chainId")
        if chain not in CHAINS:
            continue

        liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
        if liquidity < MIN_LIQUIDITY_USD:
            continue
        if not volume_spike_detected(pair):
            continue

        pair_addr = pair.get("pairAddress")
        if not pair_addr or (pair_addr in seen_pairs and (now - seen_pairs[pair_addr]) < timedelta(minutes=ALERT_COOLDOWN_MIN)):
            continue

        token_address = pair.get("baseToken", {}).get("address")
        if not token_address or "0x" not in token_address.lower():
            continue

        is_low, whale_info = check_whale_concentration(token_address, chain)

        if is_low:
            send_telegram_alert(pair, whale_info)
            seen_pairs[pair_addr] = now
            alerts_sent += 1

    if (now - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL_SEC:
        try:
            bot.send_message(CHAT_ID, f"🫀 Bot is alive • {len(pairs)} pairs scanned")
            last_heartbeat = now
        except:
            pass

    logger.info(f"Cycle done – {len(pairs)} pairs checked, {alerts_sent} alerts sent")
    time.sleep(CHECK_INTERVAL_SEC)
