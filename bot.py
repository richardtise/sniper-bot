import asyncio
import aiohttp
import time
import re
from typing import Dict, List, Optional, Tuple

# ---------- CONFIGURATION ----------
TELEGRAM_BOT_TOKEN = "TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID = "CHAT_ID"

# Blockchain explorer API keys (get free from etherscan.io, bscscan.com, basescan.org)
SCANNER_API_KEY = "SCANNER_API_KEY"
BSCSCAN_API_KEY = "YOUR_BSCSCAN_API_KEY"
BASESCAN_API_KEY = "YOUR_BASESCAN_API_KEY"

NETWORKS = ["bsc", "ethereum", "base"]

SCAN_INTERVAL = 20                    # seconds between full scans
SEEN_PAIRS_CLEANUP = 1000             # reset seen set after this many entries

# ---------- FRESH PAIR DETECTION (RaveDAO style) ----------
FRESH_MAX_AGE_MINUTES = 35
FRESH_MIN_LIQUIDITY_USD = 5000
FRESH_MAX_LIQUIDITY_USD = 250000
FRESH_MIN_5MIN_VOLUME = 18000
FRESH_SPIKE_MULTIPLIER = 2.5          # 5m vol / 1h vol

# ---------- RESURRECTION DETECTION (dead token pumps) ----------
RESURRECTION_ENABLED = True
RESURRECTION_MIN_AGE_HOURS = 1        # token must be at least 1 hour old
RESURRECTION_MAX_LIQUIDITY_USD = 250000
RESURRECTION_MIN_5MIN_VOLUME = 15000
RESURRECTION_VOLUME_SPIKE_24H = 10.0  # 5m vol vs 24h avg > 10x
RESURRECTION_MIN_PRICE_GAIN_PCT = 100 # price up >100% in last hour

# ---------- HOLDER CONCENTRATION (common to both) ----------
MAX_TOP10_SUPPLY_PERCENT = 82         # alert if top10 hold more than this
MIN_HOLDER_COUNT = 40                 # ignore tokens with too few holders

# ---------- GLOBALS ----------
seen_pairs = set()
api_rate_limiter = asyncio.Semaphore(3)   # max 3 concurrent explorer API calls

# ---------- HELPER FUNCTIONS ----------
def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

async def fetch_json(session: aiohttp.ClientSession, url: str, timeout=15):
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception:
        return None

# ---------- DEX PAIR FETCHING ----------
async def get_new_pairs(session: aiohttp.ClientSession, network: str) -> List[Dict]:
    """
    Fetch most recent pairs from DexScreener.
    Uses chain-specific search to get a broad set of new pairs.
    """
    # Better than hardcoded queries: search by chain prefix
    url = f"https://api.dexscreener.com/latest/dex/search?q=/{network}/"
    data = await fetch_json(session, url)
    if not data or "pairs" not in data:
        return []
    pairs = data["pairs"]
    # Sort by creation time (newest first) – fallback to 'createdAt' if needed
    pairs.sort(key=lambda x: x.get("pairCreatedAt") or x.get("createdAt", 0), reverse=True)
    return pairs[:20]   # limit to 20 newest to avoid spam

# ---------- HOLDER CONCENTRATION FROM EXPLORER APIS ----------
async def get_token_supply_and_holders(session: aiohttp.ClientSession, chain: str, token_address: str) -> Tuple[float, int]:
    """
    Returns (top10_percent_of_supply, total_holders_count)
    Uses BscScan/Etherscan/BaseScan APIs.
    """
    async with api_rate_limiter:
        # Construct API URLs for each chain
        if chain == "bsc":
            holder_url = f"https://api.bscscan.com/api?module=token&action=tokenholderlist&contractaddress={token_address}&page=1&offset=20&apikey={BSCSCAN_API_KEY}"
            supply_url = f"https://api.bscscan.com/api?module=stats&action=tokensupply&contractaddress={token_address}&apikey={BSCSCAN_API_KEY}"
        elif chain == "ethereum":
            holder_url = f"https://api.etherscan.io/api?module=token&action=tokenholderlist&contractaddress={token_address}&page=1&offset=20&apikey={ETHERSCAN_API_KEY}"
            supply_url = f"https://api.etherscan.io/api?module=stats&action=tokensupply&contractaddress={token_address}&apikey={ETHERSCAN_API_KEY}"
        elif chain == "base":
            holder_url = f"https://api.basescan.org/api?module=token&action=tokenholderlist&contractaddress={token_address}&page=1&offset=20&apikey={BASESCAN_API_KEY}"
            supply_url = f"https://api.basescan.org/api?module=stats&action=tokensupply&contractaddress={token_address}&apikey={BASESCAN_API_KEY}"
        else:
            return 0.0, 0

        # Small delay to be nice to free APIs
        await asyncio.sleep(0.35)

        # Get total supply
        supply_data = await fetch_json(session, supply_url)
        total_supply = float(supply_data.get("result", 0)) if supply_data and supply_data.get("status") == "1" else 0

        if total_supply == 0:
            return 0.0, 0

        # Get top holders
        holders_data = await fetch_json(session, holder_url)
        if not holders_data or holders_data.get("status") != "1":
            return 0.0, 0

        holders = holders_data.get("result", [])
        if not holders:
            return 0.0, 0

        top10_balance = 0.0
        for holder in holders[:10]:
            top10_balance += float(holder.get("balance", 0))

        top10_percent = (top10_balance / total_supply) * 100
        total_holders = len(holders)
        return round(top10_percent, 2), total_holders

# ---------- RISK ASSESSMENT (Both Modes) ----------
async def assess_crime_pump_risk(session: aiohttp.ClientSession, pair: Dict) -> Optional[Dict]:
    """
    Evaluates a single pair. Returns alert dict if either fresh or resurrection criteria are met.
    """
    try:
        chain = pair.get("chainId", "")
        token_addr = pair.get("baseToken", {}).get("address", "")
        if not chain or not token_addr:
            return None

        # Basic metrics
        liquidity = float(pair.get("liquidity", {}).get("usd", 0))
        vol_5m = float(pair.get("volume", {}).get("m5", 0))
        vol_1h = float(pair.get("volume", {}).get("h1", 0))
        vol_24h = float(pair.get("volume", {}).get("h24", 0))
        price_change_h1 = float(pair.get("priceChange", {}).get("h1", 0))

        # Pair age (fallback to 'createdAt' if 'pairCreatedAt' missing)
        created_at = pair.get("pairCreatedAt") or pair.get("createdAt", 0)
        age_min = (time.time() * 1000 - created_at) / 60000 if created_at else 999

        # ----- Common requirement: high holder concentration -----
        top10_pct, holder_count = await get_token_supply_and_holders(session, chain, token_addr)
        if top10_pct == 0 or holder_count < MIN_HOLDER_COUNT:
            return None
        if top10_pct <= MAX_TOP10_SUPPLY_PERCENT:
            return None   # not concentrated enough

        # ----- MODE A: Fresh pump (young pair) -----
        if age_min <= FRESH_MAX_AGE_MINUTES:
            # Volume and liquidity filters
            if not (FRESH_MIN_LIQUIDITY_USD <= liquidity <= FRESH_MAX_LIQUIDITY_USD):
                return None
            if vol_5m < FRESH_MIN_5MIN_VOLUME:
                return None
            if vol_1h > 0 and (vol_5m / vol_1h) < FRESH_SPIKE_MULTIPLIER:
                return None

            # Fresh pump detected
            return {
                "pump_type": "🔥 FRESH CRIME PUMP (New Pair)",
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
                "dex_url": f"https://dexscreener.com/{chain}/{pair.get('pairAddress')}",
            }

        # ----- MODE B: Resurrection pump (old, dead token) -----
        if RESURRECTION_ENABLED and age_min >= (RESURRECTION_MIN_AGE_HOURS * 60):
            # Must have 24h volume to calculate baseline
            if vol_24h <= 0:
                return None

            # Expected 5-minute volume if volume was evenly distributed over 24h
            # 24h = 288 five-minute intervals
            expected_5m_avg = vol_24h / 288.0
            spike_vs_24h = vol_5m / expected_5m_avg if expected_5m_avg > 0 else 0

            # Check resurrection conditions
            if liquidity > RESURRECTION_MAX_LIQUIDITY_USD:
                return None
            if vol_5m < RESURRECTION_MIN_5MIN_VOLUME:
                return None
            if spike_vs_24h < RESURRECTION_VOLUME_SPIKE_24H:
                return None
            if price_change_h1 < RESURRECTION_MIN_PRICE_GAIN_PCT:
                return None

            return {
                "pump_type": "💀 RESURRECTION PUMP (Dead Token Waking Up)",
                "chain": chain,
                "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
                "token_symbol": pair.get("baseToken", {}).get("symbol", "???"),
                "token_address": token_addr,
                "pair_address": pair.get("pairAddress", ""),
                "liquidity_usd": liquidity,
                "vol_5m": vol_5m,
                "vol_spike": spike_vs_24h,          # vs 24h avg
                "top10_pct": top10_pct,
                "holder_count": holder_count,
                "age_min": age_min,
                "price_change_h1": price_change_h1,
                "dex_url": f"https://dexscreener.com/{chain}/{pair.get('pairAddress')}",
            }

        return None

    except Exception as e:
        print(f"Assessment error: {e}")
        return None

# ---------- TELEGRAM ALERT ----------
async def send_alert(session: aiohttp.ClientSession, alert: Dict):
    # Build message depending on pump type
    if alert["pump_type"] == "🔥 FRESH CRIME PUMP (New Pair)":
        msg = f"""🚨 **{alert['pump_type']}** 🚨

**{escape_md(alert['token_name'])}** ({escape_md(alert['token_symbol'])})
🔗 Chain: **{alert['chain'].upper()}**
💧 Liquidity: **${alert['liquidity_usd']:,.0f}**
📊 5m Volume: **${alert['vol_5m']:,.0f}**
📈 Volume Spike (5m/1h): **{alert['vol_spike']:.1f}x**
👥 Top 10 Holders: **{alert['top10_pct']:.1f}%** of supply
📋 Holders (approx): **{alert['holder_count']}**
⏱ Age: **{alert['age_min']:.1f} min**

⚠️ Extreme insider control + early volume spike → classic coordinated pump (RAVE/LABS style).
🚫 Do NOT buy – high probability of rug.

🔗 [DexScreener]({alert['dex_url']})
🔗 [View Holders](https://{alert['chain'] if alert['chain'] != 'ethereum' else 'etherscan'}.io/token/{alert['token_address']}#balances)"""
    else:  # Resurrection pump
        msg = f"""⚠️ **{alert['pump_type']}** ⚠️

**{escape_md(alert['token_name'])}** ({escape_md(alert['token_symbol'])})
🔗 Chain: **{alert['chain'].upper()}**
💧 Liquidity: **${alert['liquidity_usd']:,.0f}**
📊 5m Volume: **${alert['vol_5m']:,.0f}**
📈 Volume Spike (5m vs 24h avg): **{alert['vol_spike']:.1f}x**
📊 Price Change (1h): **+{alert['price_change_h1']:.0f}%**
👥 Top 10 Holders: **{alert['top10_pct']:.1f}%** of supply
📋 Holders: **{alert['holder_count']}**
⏱ Pair Age: **{alert['age_min']:.0f} minutes** ({alert['age_min']/60:.1f} hours)

💀 This token was dead but is suddenly pumping with extreme volume.
Insider control remains high → likely a coordinated exit scam.

🚫 Do NOT buy – risk of immediate dump.

🔗 [DexScreener]({alert['dex_url']})
🔗 [View Holders](https://{alert['chain'] if alert['chain'] != 'ethereum' else 'etherscan'}.io/token/{alert['token_address']}#balances)"""

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        await session.post(url, json=payload)
        print(f"Alert sent: {alert['token_symbol']} ({alert['pump_type']})")
    except Exception as e:
        print(f"Telegram send error: {e}")

# ---------- MAIN SCAN LOOP ----------
async def main():
    global seen_pairs
    print("🚀 Supply Concentration Crime Pump Detector Started (Fresh + Resurrection modes)")
    print(f"   Fresh pair age limit: {FRESH_MAX_AGE_MINUTES} min")
    print(f"   Resurrection detection: {'ON' if RESURRECTION_ENABLED else 'OFF'}")
    async with aiohttp.ClientSession() as session:
        while True:
            for network in NETWORKS:
                print(f"Scanning {network}...")
                pairs = await get_new_pairs(session, network)
                # Check at most 15 newest pairs per network to stay within rate limits
                for pair in pairs[:15]:
                    pair_id = pair.get("pairAddress")
                    if pair_id in seen_pairs:
                        continue
                    seen_pairs.add(pair_id)
                    if len(seen_pairs) > SEEN_PAIRS_CLEANUP:
                        seen_pairs.clear()

                    alert = await assess_crime_pump_risk(session, pair)
                    if alert:
                        await send_alert(session, alert)

            await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())

