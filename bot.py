import asyncio
import aiohttp
import time
import os
import logging
from contextlib import asynccontextmanager
from collections import deque
from typing import Dict, List, Optional, Tuple
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
SCAN_INTERVAL = 60          # seconds between scan cycles
HEARTBEAT_INTERVAL = 3600   # 1 hour

# --- Filtering thresholds ---
# Whale concentration: only alert if top 10 holders own >X% of supply.
# High concentration (>75%) is intentional here as a pump signal — tightly
# held tokens are more susceptible to coordinated pumps. Set lower (e.g. 40%)
# if you want broader coverage.
TOP10_THRESHOLD = 75.0

MIN_LIQUIDITY_USD = 18_000.0

# 5m volume is ≥80% of the 1h volume → nearly ALL trading happened in the last
# 5 minutes, which is a genuine burst signal (5m is only ~8.3% of 1h normally).
MIN_VOLUME_SPIKE_RATIO_5M_1H = 0.80

# 1h volume is ≥15% of 24h volume → last hour is running 3.6× the 24h average.
MIN_VOLUME_RATIO_1H_24H = 0.15

# Buy pressure: buys/(buys+sells) in the 5m window must exceed this ratio.
# Filters out volume spikes that are actually sell-offs.
MIN_BUY_PRESSURE_5M = 0.55

# Minimum absolute 5m volume in USD to ignore dust-level pairs.
MIN_VOL_5M_USD = 1_000.0

# Max retries for rate-limited requests before giving up.
MAX_RETRIES = 3

# ===================== GLOBALS =====================
# LRU-style seen_pairs: fixed-size deque tracks insertion order;
# a parallel set gives O(1) lookup. Oldest entries evicted when full.
_seen_pairs_order: deque = deque(maxlen=10_000)
_seen_pairs_set: set = set()

holder_cache: Dict[str, Tuple[float, float]] = {}  # key → (pct, timestamp)
total_pairs_scanned = 0
alerts_sent = 0
start_time = 0.0
last_heartbeat_time = 0.0
shutdown_flag = False
hourly_scan_records: deque = deque(maxlen=100)

# Semaphore limits concurrent outbound API calls to avoid hammering endpoints.
api_rate_limiter = asyncio.Semaphore(5)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("pump_bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)


# ===================== STARTUP VALIDATION =====================
def validate_env():
    """Warn loudly about missing optional but critical config at startup."""
    if not MORALIS_API_KEY:
        logger.warning(
            "⚠️  MORALIS_API_KEY is not set. "
            "get_top10_percent() will always return 0.0, "
            "meaning NO token will ever pass the TOP10_THRESHOLD filter "
            "and the bot will send ZERO alerts. Set this in your .env file."
        )


# ===================== SEEN PAIRS LRU =====================
def _pair_seen(pair_id: str) -> bool:
    return pair_id in _seen_pairs_set


def _mark_pair_seen(pair_id: str):
    """Add pair_id to the LRU set. When the deque overflows its maxlen,
    the oldest entry is automatically evicted; we mirror that in the set."""
    if len(_seen_pairs_order) == _seen_pairs_order.maxlen:
        evicted = _seen_pairs_order[0]  # leftmost = oldest
        _seen_pairs_set.discard(evicted)
    _seen_pairs_order.append(pair_id)
    _seen_pairs_set.add(pair_id)


# ===================== HELPER FUNCTIONS =====================
async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    headers: Optional[Dict] = None,
    timeout: int = 15,
    retries: int = MAX_RETRIES,
) -> Optional[Dict]:
    """
    Fetch JSON from *url* with bounded exponential-backoff retry on 429.
    Logs non-200 status codes explicitly so API key expiry is never silent.
    """
    async with api_rate_limiter:
        for attempt in range(1, retries + 1):
            try:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        wait = 2 ** attempt  # 2s, 4s, 8s
                        logger.warning(
                            f"Rate-limited (429) on attempt {attempt}/{retries}: "
                            f"{url} — sleeping {wait}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    else:
                        logger.warning(
                            f"Non-200 response ({resp.status}) from {url}"
                        )
                        return None
            except asyncio.TimeoutError:
                logger.debug(f"Timeout on attempt {attempt}/{retries}: {url}")
            except Exception as e:
                logger.debug(f"Fetch error on attempt {attempt}/{retries} {url}: {e}")
        return None


# ===================== PAIR COLLECTION =====================
async def get_all_pairs(
    session: aiohttp.ClientSession, network: str
) -> List[Dict]:
    """
    Collect up to ~300 pairs for *network* using real DexScreener v1 endpoints:

      1. /token-boosts/top/v1   — most-boosted tokens (high attention / paid promo)
      2. /token-profiles/latest/v1 — newest token listings
      3. /latest/dex/search?q=<network> — broad search filtered by chain

    For each token address discovered via (1) and (2), we hydrate full pair data
    via /tokens/v1/{chain}/{address} which returns the Pair schema with volume,
    liquidity, txns etc.  The search endpoint (3) returns Pair objects directly.
    """
    pairs: List[Dict] = []
    seen: set = set()

    async def add_pair(p: Dict):
        addr = p.get("pairAddress")
        if addr and addr not in seen:
            seen.add(addr)
            pairs.append(p)

    async def hydrate_token(token_address: str) -> List[Dict]:
        """Fetch full pair data for a token address."""
        url = f"https://api.dexscreener.com/tokens/v1/{network}/{token_address}"
        data = await fetch_json(session, url)
        if not data:
            return []
        # Response is a list of Pair objects
        if isinstance(data, list):
            return [p for p in data if p.get("chainId") == network]
        if isinstance(data, dict) and "pairs" in data:
            return [p for p in data["pairs"] if p.get("chainId") == network]
        return []

    # --- Source 1: Top boosted tokens ---
    boost_url = "https://api.dexscreener.com/token-boosts/top/v1"
    boost_data = await fetch_json(session, boost_url)
    if boost_data and isinstance(boost_data, list):
        # Filter to this network, take top 50
        network_boosts = [b for b in boost_data if b.get("chainId") == network][:50]
        hydrate_tasks = [
            hydrate_token(b["tokenAddress"])
            for b in network_boosts
            if b.get("tokenAddress")
        ]
        results = await asyncio.gather(*hydrate_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, list):
                for p in result:
                    await add_pair(p)

    # --- Source 2: Latest token profiles ---
    profiles_url = "https://api.dexscreener.com/token-profiles/latest/v1"
    profiles_data = await fetch_json(session, profiles_url)
    if profiles_data and isinstance(profiles_data, list):
        network_profiles = [
            p for p in profiles_data if p.get("chainId") == network
        ][:50]
        hydrate_tasks = [
            hydrate_token(p["tokenAddress"])
            for p in network_profiles
            if p.get("tokenAddress")
        ]
        results = await asyncio.gather(*hydrate_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, list):
                for p in result:
                    await add_pair(p)

    # --- Source 3: Search by chain name (returns Pair objects directly) ---
    if len(pairs) < 200:
        search_url = (
            f"https://api.dexscreener.com/latest/dex/search?q={network}"
        )
        search_data = await fetch_json(session, search_url)
        if search_data and "pairs" in search_data:
            for p in search_data["pairs"]:
                if p.get("chainId") == network:
                    await add_pair(p)

    # Sort by 5m volume descending — highest-momentum pairs first.
    pairs.sort(
        key=lambda x: float(x.get("volume", {}).get("m5", 0)), reverse=True
    )
    logger.info(f"[{network}] Collected {len(pairs)} pairs")
    return pairs[:300]


# ===================== MORALIS HOLDER CHECK =====================
async def get_top10_percent(
    session: aiohttp.ClientSession, chain: str, token: str
) -> float:
    """
    Returns the percentage of total supply held by the top 10 wallet addresses.
    Results are cached for 10 minutes to minimise Moralis API usage.
    Cache is evicted lazily (entries older than 1 hour removed on each write).
    """
    if not MORALIS_API_KEY:
        return 0.0

    moralis_chain = {"bsc": "bsc", "ethereum": "eth", "base": "base"}.get(chain)
    if not moralis_chain:
        return 0.0

    cache_key = f"{moralis_chain}:{token}"
    now = time.time()

    if cache_key in holder_cache:
        pct, timestamp = holder_cache[cache_key]
        if now - timestamp < 600:  # 10-minute TTL
            logger.debug(f"Cache hit {token} on {chain}: {pct:.2f}%")
            return pct

    url = (
        f"https://deep-index.moralis.io/api/v2.2/erc20/{token}/owners"
        f"?chain={moralis_chain}&order=DESC&limit=10"
    )
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

    # Lazy eviction: remove entries older than 1 hour on each cache write.
    stale = [k for k, (_, ts) in holder_cache.items() if now - ts > 3600]
    for k in stale:
        del holder_cache[k]

    return pct


# ===================== TOKEN EVALUATION =====================
async def evaluate_token(
    session: aiohttp.ClientSession, pair: Dict
) -> Optional[Dict]:
    """
    Returns an alert dict if the pair passes ALL filters, otherwise None.

    Filter pipeline (cheap → expensive):
      1. Required fields present
      2. Minimum liquidity
      3. Not already seen (LRU dedup)
      4. Minimum absolute 5m volume
      5. Volume spike signal (5m/1h OR 1h/24h)
      6. Buy pressure in 5m window (buys > sells)
      7. Top-10 holder concentration via Moralis (most expensive — last)
    """
    chain = pair.get("chainId")
    token = pair.get("baseToken", {}).get("address")
    pair_id = pair.get("pairAddress")
    if not all([chain, token, pair_id]):
        return None

    # 1. Minimum liquidity
    liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
    if liquidity < MIN_LIQUIDITY_USD:
        return None

    # 2. Dedup via LRU
    if _pair_seen(pair_id):
        return None
    _mark_pair_seen(pair_id)

    # 3. Volume data
    vol5  = float(pair.get("volume", {}).get("m5")  or 0)
    vol1h = float(pair.get("volume", {}).get("h1")  or 0)
    vol24h = float(pair.get("volume", {}).get("h24") or 0)

    # Minimum absolute 5m volume — ignore near-zero dust pairs
    if vol5 < MIN_VOL_5M_USD:
        return None

    # 4. Volume spike detection
    # Signal A: 5m vol is ≥80% of 1h vol → massive burst in last 5 minutes
    # Signal B: 1h vol is ≥15% of 24h vol → last hour running 3.6× daily average
    spike = False
    trigger = ""
    if vol1h > 0 and vol5 / vol1h >= MIN_VOLUME_SPIKE_RATIO_5M_1H:
        spike = True
        trigger = f"5m={vol5:,.0f} / 1h={vol1h:,.0f} ({vol5/vol1h*100:.0f}%)"
    elif vol24h > 0 and vol1h / vol24h >= MIN_VOLUME_RATIO_1H_24H:
        spike = True
        trigger = f"1h={vol1h:,.0f} / 24h={vol24h:,.0f} ({vol1h/vol24h*100:.0f}%)"

    if not spike:
        return None

    # 5. Buy pressure filter — must be net buy-side pressure in last 5m
    txns_5m = pair.get("txns", {}).get("m5", {})
    buys_5m  = int(txns_5m.get("buys",  0))
    sells_5m = int(txns_5m.get("sells", 0))
    total_5m = buys_5m + sells_5m
    if total_5m > 0:
        buy_pressure = buys_5m / total_5m
        if buy_pressure < MIN_BUY_PRESSURE_5M:
            return None  # volume spike is actually a sell-off

    # 6. Whale concentration (Moralis) — most expensive check, done last
    top10_pct = await get_top10_percent(session, chain, token)
    if top10_pct < TOP10_THRESHOLD:
        return None

    price_change_5m  = pair.get("priceChange", {}).get("m5", 0) or 0
    price_change_1h  = pair.get("priceChange", {}).get("h1", 0) or 0

    return {
        "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
        "symbol":     pair.get("baseToken", {}).get("symbol", "???"),
        "chain":      chain.upper(),
        "liquidity":  liquidity,
        "vol_5m":     vol5,
        "vol_1h":     vol1h,
        "vol_24h":    vol24h,
        "spike_type": trigger,
        "top10_pct":  round(top10_pct, 2),
        "buys_5m":    buys_5m,
        "sells_5m":   sells_5m,
        "price_chg_5m":  round(price_change_5m, 2),
        "price_chg_1h":  round(price_change_1h, 2),
        "dex_url":    f"https://dexscreener.com/{chain}/{pair_id}",
    }


# ===================== TELEGRAM ALERTS =====================
async def send_alert(alert: Dict, retries: int = 2):
    global alerts_sent

    buy_pct = (
        round(alert["buys_5m"] / (alert["buys_5m"] + alert["sells_5m"]) * 100, 1)
        if (alert["buys_5m"] + alert["sells_5m"]) > 0
        else "N/A"
    )

    text = (
        f"🚨 <b>EARLY PUMP SIGNAL</b> 🚨\n\n"
        f"<b>{alert['token_name']}</b> ({alert['symbol']})\n"
        f"🔗 Chain: <b>{alert['chain']}</b>\n\n"
        f"💰 Liquidity: <b>${alert['liquidity']:,.0f}</b>\n"
        f"📊 Volume 5m: <b>${alert['vol_5m']:,.0f}</b> | "
        f"1h: <b>${alert['vol_1h']:,.0f}</b> | "
        f"24h: <b>${alert['vol_24h']:,.0f}</b>\n"
        f"⚡ Spike: <b>{alert['spike_type']}</b>\n"
        f"🟢 Buy pressure 5m: <b>{buy_pct}%</b> "
        f"({alert['buys_5m']}B / {alert['sells_5m']}S)\n"
        f"📈 Price Δ 5m: <b>{alert['price_chg_5m']:+.2f}%</b> | "
        f"1h: <b>{alert['price_chg_1h']:+.2f}%</b>\n"
        f"👥 Top10 holders: <b>{alert['top10_pct']}%</b>\n\n"
        f"🔗 <a href='{alert['dex_url']}'>DexScreener</a>"
    )

    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            alerts_sent += 1
            logger.info(
                f"Alert sent → {alert['symbol']} on {alert['chain']} "
                f"(top10: {alert['top10_pct']}%)"
            )
            return
        except Exception as e:
            logger.error(f"Telegram send attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                await asyncio.sleep(2)

    logger.error(f"Alert dropped after {retries} retries: {alert['symbol']}")


async def send_heartbeat():
    uptime_hours = (time.time() - start_time) / 3600 if start_time > 0 else 0
    now = time.time()
    pairs_last_hour = sum(
        cnt for ts, cnt in hourly_scan_records if now - ts <= 3600
    )
    text = (
        f"🫀 <b>Bot Heartbeat</b>\n"
        f"Uptime: <b>{uptime_hours:.1f}h</b>\n"
        f"Alerts sent: <b>{alerts_sent}</b>\n"
        f"Total pairs scanned: <b>{total_pairs_scanned:,}</b>\n"
        f"Pairs last hour: <b>{pairs_last_hour:,}</b>\n"
        f"Holder cache size: <b>{len(holder_cache)}</b>"
    )
    for attempt in range(1, 3):
        try:
            await bot.send_message(
                chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML
            )
            logger.info("Heartbeat sent")
            return
        except Exception as e:
            logger.error(f"Heartbeat attempt {attempt}/2 failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2)


# ===================== BOT TASK =====================
async def bot_task():
    global start_time, last_heartbeat_time, total_pairs_scanned

    validate_env()
    start_time = time.time()
    last_heartbeat_time = start_time
    logger.info("🚀 Bot task started")

    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"✅ <b>Pump Bot</b> started.\n"
                f"Scan interval: {SCAN_INTERVAL}s | "
                f"Min liq: ${MIN_LIQUIDITY_USD:,.0f}\n"
                f"Top10 threshold: >{TOP10_THRESHOLD}% | "
                f"Min 5m vol: ${MIN_VOL_5M_USD:,.0f}\n"
                f"Buy pressure filter: >{MIN_BUY_PRESSURE_5M*100:.0f}% buys\n"
                f"Moralis key: {'✅' if MORALIS_API_KEY else '❌ MISSING — no alerts will fire'}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Startup message failed: {e}")

    async with aiohttp.ClientSession() as session:
        while not shutdown_flag:
            cycle_start = time.time()
            cycle_pairs = 0

            # Heartbeat
            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                await send_heartbeat()
                last_heartbeat_time = time.time()

            # Scan all networks
            for network in NETWORKS:
                if shutdown_flag:
                    break
                logger.info(f"Scanning {network}...")
                try:
                    pairs = await get_all_pairs(session, network)
                except Exception as e:
                    logger.error(f"get_all_pairs({network}) crashed: {e}")
                    continue

                for pair in pairs:
                    if shutdown_flag:
                        break
                    total_pairs_scanned += 1
                    cycle_pairs += 1
                    try:
                        alert = await evaluate_token(session, pair)
                    except Exception as e:
                        logger.error(f"evaluate_token crashed: {e}")
                        continue
                    if alert:
                        await send_alert(alert)
                        await asyncio.sleep(1)  # brief throttle between alerts

            cycle_duration = time.time() - cycle_start
            hourly_scan_records.append((cycle_start, cycle_pairs))
            logger.info(
                f"Cycle complete: {cycle_pairs} pairs in {cycle_duration:.1f}s"
            )

            # Sleep the remainder of SCAN_INTERVAL (or skip if cycle overran)
            sleep_time = max(0, SCAN_INTERVAL - cycle_duration)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                logger.warning(
                    f"Cycle overran SCAN_INTERVAL by "
                    f"{cycle_duration - SCAN_INTERVAL:.1f}s"
                )


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
        logger.info("Bot task cancelled cleanly")
    logger.info("Shutdown complete")


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health_check():
    uptime = (time.time() - start_time) / 3600 if start_time > 0 else 0
    now = time.time()
    pairs_last_hour = sum(
        cnt for ts, cnt in hourly_scan_records if now - ts <= 3600
    )
    return {
        "status": "alive",
        "uptime_hours": round(uptime, 2),
        "pairs_scanned_total": total_pairs_scanned,
        "pairs_scanned_last_hour": pairs_last_hour,
        "alerts_sent": alerts_sent,
        "holder_cache_size": len(holder_cache),
        "seen_pairs_size": len(_seen_pairs_set),
        "moralis_configured": bool(MORALIS_API_KEY),
    }


# ===================== ENTRY POINT =====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting Pump Bot on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

