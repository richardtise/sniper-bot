"""
discovery.py — candidate pair discovery for the sniper bot.

Why this module exists
----------------------
``bot.get_all_pairs()`` starts with::

    GET https://api.dexscreener.com/latest/dex/pairs/{chain}?page=0&pageSize=300

That endpoint does not exist — it returns **HTTP 404**, so the only candidates
the bot ever sees come from ``token-boosts/top/v1`` (paid promotions) and
``token-profiles/latest/v1`` (paid profiles). In other words the scanner was
looking at the shill list, which is the opposite of organic runner discovery.

This module replaces discovery with **GeckoTerminal** (free, no API key), which
exposes real pool listings *and* the fields the old code was missing:

* ``new_pools``      — freshly created pools (the actual sniper surface)
* ``trending_pools`` — pools with real momentum
* ``pools?sort=h24_volume_usd_desc`` — liquid universe for context
* unique ``buyers`` / ``sellers`` per window (not just tx counts)
* ``gt_score`` / ``gt_verified`` / socials / holder distribution via token info

Every function is dependency-injected with a ``fetch`` callable matching
``bot.fetch_json(session, url)`` so it reuses the existing aiohttp session,
semaphore and retry logic — and so it can be unit-tested offline.

GeckoTerminal's free tier is ~30 calls/min, so this module has its own throttle
(``min_interval_s``) and short-lived cache. Two list calls per network per scan
is ~16 calls/min for 4 chains.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

__all__ = [
    "GT_BASE",
    "GT_NETWORKS",
    "GeckoTerminal",
    "pool_to_pair",
    "dedupe_best_pool",
    "dexscreener_search",
]

GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_NETWORKS = {
    "ethereum": "eth",
    "bsc": "bsc",
    "base": "base",
    "robinhood": "robinhood",
}

# label -> path fragment appended to /networks/{net}/
GT_ENDPOINTS: Dict[str, str] = {
    "new_pools": "new_pools",
    "trending": "trending_pools",
    "top_volume": "pools?sort=h24_volume_usd_desc",
}

Fetch = Callable[[str], Awaitable[Optional[Any]]]


def _iso_to_ms(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000.0
    except (ValueError, TypeError):
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:
        return default
    return out


def pool_to_pair(
    pool: dict,
    included_tokens: Dict[str, dict],
    chain: str,
    source: str,
) -> Optional[dict]:
    """Convert a GeckoTerminal pool resource into a DexScreener-shaped pair.

    The output is intentionally the same shape ``bot.evaluate_token`` already
    consumes, plus the extra ``buyers``/``sellers`` keys used by signals.py.
    """
    attrs = (pool or {}).get("attributes") or {}
    rels = (pool or {}).get("relationships") or {}
    address = attrs.get("address")
    if not address:
        return None

    def token_of(rel_name: str) -> dict:
        rel = (rels.get(rel_name) or {}).get("data") or {}
        tid = rel.get("id") or ""
        if tid in included_tokens:
            return included_tokens[tid]
        # id looks like "bsc_0xabc..." -> strip the network prefix
        return {"address": tid.split("_", 1)[1] if "_" in tid else "", "symbol": "", "name": ""}

    base = token_of("base_token")
    quote = token_of("quote_token")
    if not base.get("address"):
        return None

    volume = attrs.get("volume_usd") or {}
    changes = attrs.get("price_change_percentage") or {}
    txns = attrs.get("transactions") or {}
    mcap = attrs.get("market_cap_usd")
    fdv = attrs.get("fdv_usd")

    dex_id = ((rels.get("dex") or {}).get("data") or {}).get("id") or ""

    return {
        "chainId": chain,
        "dexId": dex_id,
        "pairAddress": address,
        "url": f"https://www.geckoterminal.com/{GT_NETWORKS.get(chain, chain)}/pools/{address}",
        "baseToken": base,
        "quoteToken": quote,
        "priceUsd": attrs.get("base_token_price_usd"),
        "priceNative": attrs.get("base_token_price_native_currency"),
        "liquidity": {"usd": _num(attrs.get("reserve_in_usd"))},
        "fdv": _num(fdv),
        "marketCap": _num(mcap) or _num(fdv),
        "volume": {
            "m5": _num(volume.get("m5")),
            "m15": _num(volume.get("m15")),
            "h1": _num(volume.get("h1")),
            "h6": _num(volume.get("h6")),
            "h24": _num(volume.get("h24")),
        },
        "txns": {
            "m5": _txn(txns.get("m5")),
            "m15": _txn(txns.get("m15")),
            "h1": _txn(txns.get("h1")),
            "h6": _txn(txns.get("h6")),
            "h24": _txn(txns.get("h24")),
        },
        "priceChange": {
            "m5": _num(changes.get("m5")),
            "m15": _num(changes.get("m15")),
            "h1": _num(changes.get("h1")),
            "h6": _num(changes.get("h6")),
            "h24": _num(changes.get("h24")),
        },
        "pairCreatedAt": _iso_to_ms(attrs.get("pool_created_at")),
        "info": None,
        "boosts": None,
        "source": source,
    }


def _txn(window: Optional[dict]) -> dict:
    window = window or {}
    return {
        "buys": int(_num(window.get("buys"))),
        "sells": int(_num(window.get("sells"))),
        "buyers": int(_num(window.get("buyers"))),
        "sellers": int(_num(window.get("sellers"))),
    }


def dedupe_best_pool(pairs: List[dict]) -> List[dict]:
    """One row per (chain, base token), keeping the deepest pool.

    The old code evaluated every pool of the same token independently, which
    burned API budget and produced duplicate/contradictory alerts.
    """
    best: Dict[Tuple[str, str], dict] = {}
    for pair in pairs:
        token = str((pair.get("baseToken") or {}).get("address") or "").lower()
        if not token:
            continue
        key = (str(pair.get("chainId") or "").lower(), token)
        current = best.get(key)
        if current is None:
            best[key] = pair
            continue
        new_liq = _num((pair.get("liquidity") or {}).get("usd"))
        old_liq = _num((current.get("liquidity") or {}).get("usd"))
        if new_liq > old_liq:
            best[key] = pair
    return list(best.values())


def _parse_included(payload: dict) -> Dict[str, dict]:
    tokens: Dict[str, dict] = {}
    for item in payload.get("included") or []:
        if item.get("type") != "token":
            continue
        attrs = item.get("attributes") or {}
        tokens[item.get("id")] = {
            "address": attrs.get("address") or "",
            "symbol": attrs.get("symbol") or "",
            "name": attrs.get("name") or "",
        }
    return tokens


class GeckoTerminal:
    """Throttled, cached GeckoTerminal client around an injected ``fetch``."""

    def __init__(
        self,
        fetch: Fetch,
        *,
        min_interval_s: float = 2.1,
        cache_ttl_s: float = 90.0,
    ) -> None:
        self.fetch = fetch
        self.min_interval_s = min_interval_s
        self.cache_ttl_s = cache_ttl_s
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._cache: Dict[str, Tuple[Any, float]] = {}

    async def _get(self, path: str) -> Optional[Any]:
        now = time.time()
        cached = self._cache.get(path)
        if cached and now - cached[1] < self.cache_ttl_s:
            return cached[0]
        async with self._lock:
            wait = self.min_interval_s - (time.time() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.time()
            data = await self.fetch(f"{GT_BASE}/{path}")
        # Only cache successful payloads so a transient 429 is retried.
        if data:
            self._cache[path] = (data, time.time())
        return data

    async def list_pools(self, chain: str, kind: str = "new_pools",
                         page: int = 1, include: str = "base_token,quote_token") -> List[dict]:
        network = GT_NETWORKS.get(chain)
        fragment = GT_ENDPOINTS.get(kind)
        if not network or not fragment:
            return []
        sep = "&" if "?" in fragment else "?"
        path = f"networks/{network}/{fragment}{sep}page={page}&include={include}"
        payload = await self._get(path)
        if not isinstance(payload, dict):
            return []
        included = _parse_included(payload)
        pairs = []
        for pool in payload.get("data") or []:
            pair = pool_to_pair(pool, included, chain, f"geckoterminal:{kind}")
            if pair:
                pairs.append(pair)
        return pairs

    async def token_info(self, chain: str, token_address: str) -> Optional[dict]:
        """``.../tokens/{addr}/info`` attributes — gt_score, socials, holders."""
        network = GT_NETWORKS.get(chain)
        if not network or not token_address:
            return None
        path = f"networks/{network}/tokens/{token_address}/info"
        payload = await self._get(path)
        if not isinstance(payload, dict):
            return None
        return ((payload.get("data") or {}).get("attributes")) or None

    async def candidates(
        self,
        chain: str,
        kinds: Tuple[str, ...] = ("new_pools", "trending"),
        page: int = 1,
    ) -> List[dict]:
        """Fetch + dedupe candidates for one chain (one best pool per token)."""
        gathered: List[dict] = []
        for kind in kinds:
            gathered.extend(await self.list_pools(chain, kind=kind, page=page))
        return dedupe_best_pool(gathered)


async def dexscreener_search(fetch: Fetch, query: str, chain: str) -> List[dict]:
    """Fallback: DexScreener search, filtered to one chain."""
    payload = await fetch(f"https://api.dexscreener.com/latest/dex/search?q={query}")
    if not isinstance(payload, dict):
        return []
    out = []
    for pair in payload.get("pairs") or []:
        if pair.get("chainId") == chain:
            pair.setdefault("source", "dexscreener_search")
            out.append(pair)
    return dedupe_best_pool(out)
