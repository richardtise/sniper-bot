"""
signals.py — runner detection & false-positive filters for the pump bot.

This module is deliberately **pure and dependency-free** (stdlib only). It takes
the same DexScreener-shaped pair dict that ``bot.py`` already builds, plus
optional enrichment (GoPlus/Blockscout security, GeckoTerminal token info) and
returns a verdict you can use to:

  1. hard-reject obvious rugs / traps *before* spending API calls, and
  2. add a runner bonus / manipulation penalty on top of the existing score.

Quick wiring (see REVIEW.md for the full diff)::

    import signals

    HISTORY = signals.PairHistory()
    FILTERS = signals.Filters.from_env()

    verdict = signals.evaluate(pair, security=security, gt=gt_meta,
                               history=HISTORY, filters=FILTERS)
    HISTORY.observe(pair, security=security, gt=gt_meta)
    if verdict.rejected:
        return None
    total_score = legacy_score + verdict.bonus - verdict.penalty

Design notes
------------
* Holder concentration is **stance-dependent**. ``holder_stance="pump"``
  (default) assumes early runners are concentrated on purpose — a few wallets
  holding most of the supply is what lets the coin move — so a large top-10 /
  dev bag is *rewarded* and only the absurd (>95% top-10, >40% dev) is rejected.
  ``holder_stance="rug"`` treats concentration as exit risk instead. The legacy
  ``score_holder`` in ``bot.py`` also rewards concentration, so "pump" keeps the
  two consistent.
* Every threshold lives in :class:`Filters` so you can tune it from env
  without touching code.
* Signal codes are stable strings so you can log/back-test them.
"""

from __future__ import annotations

import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

__all__ = [
    "Filters",
    "Verdict",
    "PairHistory",
    "evaluate",
    "hard_reject_reasons",
    "runner_signals",
    "normalize_security",
    "is_major_asset",
]

# ─────────────────────────────────────────────────────────────────────────────
# small helpers
# ─────────────────────────────────────────────────────────────────────────────


def _num(value: Any, default: float = 0.0) -> float:
    """Best-effort float coercion (GoPlus returns most fields as strings)."""
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _dig(obj: Any, *keys: str, default: float = 0.0) -> float:
    cur: Any = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return _num(cur, default)


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


# Assets that should never be "discovered" as a runner. Pasting them is still
# allowed by hand, but scanning them wastes API budget and produces fake alerts.
MAJOR_SYMBOLS = {
    "WETH", "WBNB", "WMATIC", "WAVAX", "WHYPE", "WSOL",
    "USDC", "USDT", "DAI", "BUSD", "TUSD", "FDUSD", "USDE", "SUSDE",
    "WBTC", "CBBTC", "TBTC", "WSTETH", "STETH", "RETH", "WEETH",
    "CAKE", "UNI", "AAVE", "LINK", "MKR", "CRV", "LDO",
}

MAJOR_ADDRESSES = {
    # checksummed addresses are lower-cased here
    "ethereum": {
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
    },
    "bsc": {
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
    },
    "base": {
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    },
}

_STABLE_QUOTES = {
    "WETH", "WBNB", "BNB", "ETH", "USDC", "USDT", "DAI", "BUSD", "FDUSD",
    "USDBC", "USDE", "CBBTC", "WBTC", "SOL", "WMATIC", "MATIC", "AVAX",
}

_GOOD_DEXES = {
    "uniswap", "uniswap_v2", "uniswap_v3", "pancakeswap", "pancakeswap_v2",
    "pancakeswap_v3", "aerodrome", "aerodrome_slipstream", "baseswap",
    "sushiswap", "quickswap", "thena", "biswap", "apeswap", "trader_joe",
}


# ─────────────────────────────────────────────────────────────────────────────
# configuration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Filters:
    """All tunable thresholds. Override with ``Filters.from_env()``."""

    # --- discovery / liquidity ------------------------------------------------
    # Restored to the original bot's strictness. The earlier pump-friendly
    # defaults (4000 / 250 / 1 min / 5 txns) let a lot of Base noise through.
    min_liquidity_usd: float = 8_000.0
    min_liquidity_mcap_ratio: float = 0.010   # liquidity >= 1% of mcap
    max_fdv_mcap_ratio: float = 8.0           # huge unlocked supply = dilution
    min_vol_5m_usd: float = 500.0
    min_market_cap_usd: float = 15_000.0

    # --- age / timing ---------------------------------------------------------
    min_age_minutes: float = 3.0
    max_age_minutes: float = 60.0 * 24 * 5   # beyond a week it is not a sniper
    fresh_age_minutes: float = 240.0         # bonus window

    # --- activity -------------------------------------------------------------
    min_txns_5m: int = 8
    min_unique_buyers_5m: int = 5
    min_buy_ratio_5m: float = 0.35
    max_avg_trade_liq_ratio: float = 0.10    # one wallet moving the pool
    max_vol_liq_ratio: float = 25.0          # absurd turnover = wash

    # --- holder stance --------------------------------------------------------
    # "pump" (default) = early-runner mode. Early runners are usually dominated
    #   by a few wallets; concentrated supply and a large dev bag are expected
    #   and rewarded, because a controlled float is what lets a coin run.
    # "rug" = conservative mode. Concentration is penalised and hard-rejected.
    holder_stance: str = "pump"

    # --- security -------------------------------------------------------------
    max_tax_pct: float = 10.0
    max_top10_pct: float = 60.0     # used when holder_stance == "rug"
    max_creator_pct: float = 5.0    # used when holder_stance == "rug"
    min_holders: int = 50
    require_lp_locked: bool = False          # penalty, not reject, by default
    require_open_source: bool = True

    # --- late-entry / dump guards --------------------------------------------
    max_chg_1h_late: float = 900.0           # >900% in an hour = blow-off risk
    max_dump_5m: float = 35.0                # -35% in 5m = distribution

    # --- bonuses / penalties --------------------------------------------------
    # The bonus is only ever applied through SIGNAL_BONUS_WEIGHT in bot.py,
    # which defaults to 0.0 (bonus cannot create an alert).
    max_bonus: float = 45.0
    max_penalty: float = 50.0

    @classmethod
    def from_env(cls) -> "Filters":
        """Read ``SIG_*`` env overrides; anything unset keeps its default."""
        f = cls()
        mapping = {
            "SIG_MIN_LIQUIDITY_USD": ("min_liquidity_usd", float),
            "SIG_MIN_VOL_5M_USD": ("min_vol_5m_usd", float),
            "SIG_MAX_AGE_MINUTES": ("max_age_minutes", float),
            "SIG_MIN_AGE_MINUTES": ("min_age_minutes", float),
            "SIG_MIN_TXNS_5M": ("min_txns_5m", int),
            "SIG_MAX_AVG_TRADE_LIQ": ("max_avg_trade_liq_ratio", float),
            "SIG_HOLDER_STANCE": ("holder_stance", lambda v: str(v).strip().lower()),
            "SIG_MAX_TAX_PCT": ("max_tax_pct", float),
            "SIG_MAX_TOP10_PCT": ("max_top10_pct", float),
            "SIG_MAX_CREATOR_PCT": ("max_creator_pct", float),
            "SIG_MIN_HOLDERS": ("min_holders", int),
            "SIG_REQUIRE_LP_LOCKED": ("require_lp_locked", _truthy),
            "SIG_MAX_BONUS": ("max_bonus", float),
            "SIG_MAX_PENALTY": ("max_penalty", float),
        }
        for env_key, (attr, caster) in mapping.items():
            raw = os.getenv(env_key)
            if raw not in (None, ""):
                try:
                    setattr(f, attr, caster(raw))
                except (TypeError, ValueError):
                    pass
        return f


# ─────────────────────────────────────────────────────────────────────────────
# security normalisation
# ─────────────────────────────────────────────────────────────────────────────


def normalize_security(raw: Optional[dict], gt: Optional[dict] = None) -> Dict[str, Any]:
    """Merge GoPlus/Blockscout security + GeckoTerminal token info.

    Accepts the already-normalised dict produced by ``bot.get_token_security``
    (which uses snake_case booleans + ``buy_tax``/``sell_tax`` floats) and the
    raw GeckoTerminal ``.../tokens/{addr}/info`` ``attributes`` dict.
    """
    sec: Dict[str, Any] = {
        "honeypot": False, "buy_tax": 0.0, "sell_tax": 0.0,
        "open_source": None, "proxy": False, "mintable": False,
        "owner_change_balance": False, "transfer_pausable": False,
        "slippage_modifiable": False, "hidden_owner": False,
        "cannot_sell_all": False, "selfdestruct": False,
        "trading_cooldown": False, "take_back_ownership": False,
        "blacklist": False, "whitelist": False, "lp_locked": None,
        "holder_count": 0, "top10_pct": 0.0, "creator_pct": 0.0,
        "gt_score": None, "gt_verified": False, "socials": [], "has_website": False,
    }

    if raw:
        sec.update({
            "honeypot": _truthy(raw.get("is_honeypot", raw.get("honeypot"))),
            "buy_tax": _num(raw.get("buy_tax")),
            "sell_tax": _num(raw.get("sell_tax")),
            "open_source": raw.get("is_open_source", raw.get("is_verified")),
            "proxy": _truthy(raw.get("is_proxy")),
            "mintable": _truthy(raw.get("is_mintable")),
            "owner_change_balance": _truthy(raw.get("owner_change_balance")),
            "transfer_pausable": _truthy(raw.get("transfer_pausable")),
            "slippage_modifiable": _truthy(raw.get("slippage_modifiable")),
            "hidden_owner": _truthy(raw.get("hidden_owner")),
            "cannot_sell_all": _truthy(raw.get("cannot_sell_all")),
            "selfdestruct": _truthy(raw.get("selfdestruct")),
            "trading_cooldown": _truthy(raw.get("trading_cooldown")),
            "take_back_ownership": _truthy(raw.get("can_take_back_ownership")),
            "blacklist": _truthy(raw.get("is_blacklisted")),
            "whitelist": _truthy(raw.get("is_whitelisted")),
            "lp_locked": raw.get("lp_locked", raw.get("is_lp_locked")),
            "holder_count": int(_num(raw.get("holder_count"))),
            "creator_pct": _num(raw.get("creator_percent")) * 100.0,
        })
        if sec["open_source"] is not None:
            sec["open_source"] = _truthy(sec["open_source"])

    if gt:
        holders = gt.get("holders") or {}
        dist = (holders.get("distribution_percentage") or {}) if isinstance(holders, dict) else {}
        sec["top10_pct"] = _num(dist.get("top_10"), sec["top10_pct"])
        sec["holder_count"] = int(_num(holders.get("count"), sec["holder_count"]))
        sec["creator_pct"] = _num(gt.get("developer_holding_percentage"), sec["creator_pct"])
        sec["gt_score"] = gt.get("gt_score")
        sec["gt_verified"] = _truthy(gt.get("gt_verified"))
        # Honeypot is OR-ed, never overwritten: if either source flags it, reject.
        sec["honeypot"] = bool(sec["honeypot"]) or _truthy(gt.get("is_honeypot"))
        socials = []
        if gt.get("twitter_handle"):
            socials.append("twitter")
        if gt.get("telegram_handle"):
            socials.append("telegram")
        if gt.get("discord_url"):
            socials.append("discord")
        if gt.get("websites"):
            socials.append("website")
            sec["has_website"] = True
        sec["socials"] = socials
    return sec


def is_major_asset(pair: dict, chain: str) -> bool:
    base = (pair.get("baseToken") or {})
    symbol = str(base.get("symbol") or "").upper().strip()
    address = str(base.get("address") or "").lower()
    if symbol in MAJOR_SYMBOLS:
        return True
    return address in MAJOR_ADDRESSES.get(chain, set())


# ─────────────────────────────────────────────────────────────────────────────
# hard filters — reject before enrichment / alerting
# ─────────────────────────────────────────────────────────────────────────────


def hard_reject_reasons(
    pair: dict,
    *,
    security: Optional[dict] = None,
    gt: Optional[dict] = None,
    filters: Optional[Filters] = None,
) -> List[str]:
    """Return a list of hard-reject reason codes (empty list == pass)."""
    f = filters or Filters()
    sec = normalize_security(security, gt)
    reasons: List[str] = []

    chain = str(pair.get("chainId") or "")
    if is_major_asset(pair, chain):
        reasons.append("major_asset")

    liquidity = _num(_dig(pair, "liquidity", "usd"))
    market_cap = _num(pair.get("marketCap")) or _num(pair.get("fdv"))
    vol_5m = _num(_dig(pair, "volume", "m5"))
    txns_5m = int(_dig(pair, "txns", "m5", "buys") + _dig(pair, "txns", "m5", "sells"))

    if liquidity < f.min_liquidity_usd:
        reasons.append(f"liq<{f.min_liquidity_usd:.0f}")
    if market_cap and liquidity and _ratio(liquidity, market_cap) < f.min_liquidity_mcap_ratio:
        reasons.append("liq/mcap_too_low")
    if vol_5m < f.min_vol_5m_usd:
        reasons.append("vol5m_too_low")
    if market_cap < f.min_market_cap_usd:
        reasons.append("mcap_too_low")

    fdv = _num(pair.get("fdv"))
    if fdv and market_cap and _ratio(fdv, market_cap) > f.max_fdv_mcap_ratio and fdv > 250_000:
        reasons.append("fdv>>mcap")

    created = pair.get("pairCreatedAt")
    age = None
    if created:
        age = (time.time() - _num(created) / 1000.0) / 60.0
    if age is not None:
        if age < f.min_age_minutes:
            reasons.append("too_new")
        if age > f.max_age_minutes:
            reasons.append("too_old")

    # --- quote quality --------------------------------------------------------
    quote_symbol = str((pair.get("quoteToken") or {}).get("symbol") or "").upper()
    if quote_symbol and quote_symbol not in _STABLE_QUOTES:
        reasons.append("exotic_quote")

    # --- activity / manipulation ---------------------------------------------
    if txns_5m < f.min_txns_5m:
        reasons.append(f"txns5m<{f.min_txns_5m}")
    buys_5m = int(_dig(pair, "txns", "m5", "buys"))
    sells_5m = int(_dig(pair, "txns", "m5", "sells"))
    buy_ratio = _ratio(buys_5m, txns_5m)
    if txns_5m and buy_ratio < f.min_buy_ratio_5m:
        reasons.append("sell_dominated_5m")

    avg_trade = _ratio(vol_5m, max(txns_5m, 1))
    if liquidity and _ratio(avg_trade, liquidity) > f.max_avg_trade_liq_ratio:
        reasons.append("avg_trade>10%liq")
    if liquidity and _ratio(vol_5m, liquidity) > f.max_vol_liq_ratio:
        reasons.append("wash_turnover")

    chg_5m = _num(_dig(pair, "priceChange", "m5"))
    chg_1h = _num(_dig(pair, "priceChange", "h1"))
    if chg_1h > f.max_chg_1h_late and chg_5m < 15:
        reasons.append("late_blowoff")
    if chg_5m < -f.max_dump_5m and buy_ratio < 0.45:
        reasons.append("active_dump")

    # --- security -------------------------------------------------------------
    if security is not None or gt is not None:
        if sec["honeypot"]:
            reasons.append("honeypot")
        if sec["buy_tax"] > f.max_tax_pct or sec["sell_tax"] > f.max_tax_pct:
            reasons.append("tax")
        if sec["open_source"] is False and f.require_open_source:
            reasons.append("not_open_source")
        for code, key in (
            ("hidden_owner", "hidden_owner"),
            ("cannot_sell_all", "cannot_sell_all"),
            ("selfdestruct", "selfdestruct"),
            ("trading_cooldown", "trading_cooldown"),
            ("owner_change_balance", "owner_change_balance"),
            ("transfer_pausable", "transfer_pausable"),
            ("slippage_modifiable", "slippage_modifiable"),
            ("take_back_ownership", "take_back_ownership"),
            ("blacklist", "blacklist"),
        ):
            if sec[key]:
                reasons.append(code)
        if f.holder_stance == "rug":
            if sec["top10_pct"] and sec["top10_pct"] > f.max_top10_pct:
                reasons.append("top10_concentrated")
            if sec["creator_pct"] and sec["creator_pct"] > f.max_creator_pct:
                reasons.append("dev_holds_high")
        else:
            # Early-runner mode: a few wallets holding most of the supply is the
            # norm and is what lets the coin move. Only reject the absurd.
            if sec["top10_pct"] > 95:
                reasons.append("top10>95%")
            if sec["creator_pct"] > 40:
                reasons.append("dev_holds>40%")
        if sec["holder_count"] and sec["holder_count"] < f.min_holders:
            reasons.append(f"holders<{f.min_holders}")
        if f.require_lp_locked and sec["lp_locked"] is not None and not _truthy(sec["lp_locked"]):
            reasons.append("lp_unlocked")

    return reasons


# ─────────────────────────────────────────────────────────────────────────────
# cross-scan history — persistence & acceleration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _Snapshot:
    ts: float
    vol_5m: float
    vol_1h: float
    buys_5m: int
    sellers_5m: int
    buyers_5m: int
    liquidity: float
    holders: int
    top10: float
    score: float


class PairHistory:
    """Rolling per-token snapshots used for acceleration/persistence signals."""

    def __init__(self, maxlen: int = 24, ttl_seconds: float = 3600.0) -> None:
        self.maxlen = maxlen
        self.ttl = ttl_seconds
        self._data: Dict[Tuple[str, str], Deque[_Snapshot]] = {}

    @staticmethod
    def key(chain: str, token: str) -> Tuple[str, str]:
        return (chain.lower(), token.lower())

    def observe(
        self,
        pair: dict,
        *,
        security: Optional[dict] = None,
        gt: Optional[dict] = None,
        score: float = 0.0,
        ts: Optional[float] = None,
    ) -> None:
        chain = str(pair.get("chainId") or "")
        token = str((pair.get("baseToken") or {}).get("address") or "")
        if not chain or not token:
            return
        sec = normalize_security(security, gt)
        snap = _Snapshot(
            ts=ts if ts is not None else time.time(),
            vol_5m=_num(_dig(pair, "volume", "m5")),
            vol_1h=_num(_dig(pair, "volume", "h1")),
            buys_5m=int(_dig(pair, "txns", "m5", "buys")),
            sellers_5m=int(_dig(pair, "txns", "m5", "sells")),
            buyers_5m=int(_dig(pair, "txns", "m5", "buyers")),
            liquidity=_num(_dig(pair, "liquidity", "usd")),
            holders=sec["holder_count"],
            top10=sec["top10_pct"],
            score=score,
        )
        bucket = self._data.setdefault(self.key(chain, token), deque(maxlen=self.maxlen))
        bucket.append(snap)

    def trend(self, chain: str, token: str) -> Dict[str, float]:
        """Return acceleration / persistence stats; zeros when no history."""
        bucket = self._data.get(self.key(chain, token))
        if not bucket:
            return {"observations": 0, "vol_accel": 0.0, "rising_scans": 0,
                    "buyer_trend": 0.0, "holder_delta": 0.0, "score_slope": 0.0}
        snaps = list(bucket)
        current = snaps[-1]
        prev = snaps[:-1]
        baseline = statistics.median([s.vol_5m for s in prev]) if prev else 0.0
        vol_accel = _ratio(current.vol_5m, baseline) if baseline > 0 else 0.0
        rising = 0
        for a, b in zip(snaps, snaps[1:]):
            if b.vol_5m > a.vol_5m:
                rising += 1
        buyer_trend = _ratio(current.buyers_5m, prev[-1].buyers_5m) if prev and prev[-1].buyers_5m else 0.0
        holder_delta = current.holders - prev[-1].holders if prev else 0.0
        score_slope = current.score - prev[-1].score if prev else 0.0
        return {
            "observations": len(snaps),
            "vol_accel": vol_accel,
            "rising_scans": float(rising),
            "buyer_trend": buyer_trend,
            "holder_delta": float(holder_delta),
            "score_slope": score_slope,
        }

    def prune(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        for key in list(self._data):
            bucket = self._data[key]
            while bucket and now - bucket[0].ts > self.ttl:
                bucket.popleft()
            if not bucket:
                del self._data[key]


# ─────────────────────────────────────────────────────────────────────────────
# runner scoring
# ─────────────────────────────────────────────────────────────────────────────


def runner_signals(
    pair: dict,
    *,
    security: Optional[dict] = None,
    gt: Optional[dict] = None,
    history: Optional[PairHistory] = None,
    filters: Optional[Filters] = None,
) -> Tuple[float, float, Dict[str, float], List[str]]:
    """Return ``(bonus, penalty, signals, notes)``.

    ``bonus`` rewards the things that precede an organic runner; ``penalty``
    punishes the things that precede a rug / wash-trade / late entry.
    """
    f = filters or Filters()
    sec = normalize_security(security, gt)
    notes: List[str] = []
    s: Dict[str, float] = {}

    liquidity = _num(_dig(pair, "liquidity", "usd"))
    market_cap = _num(pair.get("marketCap")) or _num(pair.get("fdv"))
    vol_5m = _num(_dig(pair, "volume", "m5"))
    vol_1h = _num(_dig(pair, "volume", "h1"))
    vol_6h = _num(_dig(pair, "volume", "h6"))
    vol_24h = _num(_dig(pair, "volume", "h24"))
    buys_5m = int(_dig(pair, "txns", "m5", "buys"))
    sells_5m = int(_dig(pair, "txns", "m5", "sells"))
    buyers_5m = int(_dig(pair, "txns", "m5", "buyers"))
    txns_5m = buys_5m + sells_5m
    chg_5m = _num(_dig(pair, "priceChange", "m5"))
    chg_1h = _num(_dig(pair, "priceChange", "h1"))
    created = pair.get("pairCreatedAt")
    age = (time.time() - _num(created) / 1000.0) / 60.0 if created else None
    quote_symbol = str((pair.get("quoteToken") or {}).get("symbol") or "").upper()

    bonus = 0.0
    penalty = 0.0

    # ── liquidity depth vs. intended trade size ------------------------------
    s["vol_liq"] = _ratio(vol_5m, liquidity)
    if s["vol_liq"] >= 1.0:
        bonus += 8; notes.append("vol≥liq(5m)")
    elif s["vol_liq"] >= 0.4:
        bonus += 5; notes.append("vol≥40%liq")
    elif s["vol_liq"] >= 0.15:
        bonus += 2

    # ── organic activity: unique wallets, not just tx count ------------------
    s["buyers_per_buy"] = _ratio(buyers_5m, buys_5m)
    if buyers_5m:
        if s["buyers_per_buy"] >= 0.7 and buyers_5m >= f.min_unique_buyers_5m:
            bonus += 8; notes.append(f"{buyers_5m} unique buyers")
        elif s["buyers_per_buy"] < 0.4 and buys_5m >= 10:
            penalty += 8; notes.append("wash:few_unique_buyers")

    # ── healthy trade size (not dust bots, not one whale) --------------------
    avg_trade = _ratio(vol_5m, max(txns_5m, 1))
    s["avg_trade_liq"] = _ratio(avg_trade, liquidity)
    if liquidity and 0.0005 <= s["avg_trade_liq"] <= 0.03:
        bonus += 4; notes.append("healthy_trade_size")
    elif s["avg_trade_liq"] > f.max_avg_trade_liq_ratio * 0.5:
        penalty += 6; notes.append("whale_dominated")

    # ── buy pressure ----------------------------------------------------------
    s["buy_ratio_5m"] = _ratio(buys_5m, txns_5m)
    if s["buy_ratio_5m"] >= 0.65:
        bonus += 7; notes.append("buy_pressure_5m")
    elif s["buy_ratio_5m"] <= 0.35 and txns_5m:
        penalty += 7; notes.append("sell_pressure_5m")

    # ── momentum acceleration (own history, then 5m-vs-1h fallback) ----------
    accel = 0.0
    rising = 0
    if history is not None:
        tr = history.trend(str(pair.get("chainId") or ""),
                           str((pair.get("baseToken") or {}).get("address") or ""))
        accel = tr["vol_accel"]
        rising = int(tr["rising_scans"])
        s["observations"] = tr["observations"]
        if tr["holder_delta"] > 0:
            bonus += min(6.0, tr["holder_delta"] / 10.0)
            notes.append(f"+{int(tr['holder_delta'])} holders")
        if tr["observations"] >= 3 and tr["score_slope"] > 0:
            bonus += 3; notes.append("score_rising")
    if accel >= 2.0:
        bonus += 8; notes.append(f"vol_accel x{accel:.1f}")
    elif vol_1h > 0 and _ratio(vol_5m, vol_1h / 12.0) >= 2.0:
        bonus += 6; notes.append("vol_5m≥2x_hourly_pace")
    if rising >= 2:
        bonus += 3; notes.append(f"vol_rising_{rising}x")

    # ── freshness: runners usually start inside a window ---------------------
    if age is not None:
        s["age_min"] = age
        if 10 <= age <= f.fresh_age_minutes:
            bonus += 6; notes.append("fresh")
        elif age > 60 * 24:
            penalty += 5; notes.append("stale>24h")

    # ── valuation headroom ----------------------------------------------------
    s["mcap"] = market_cap
    if 25_000 <= market_cap <= 5_000_000:
        bonus += 5; notes.append("mcap_room_to_run")
    elif market_cap > 50_000_000:
        penalty += 4; notes.append("mcap_already_big")

    # ── legitimacy ------------------------------------------------------------
    if sec["open_source"]:
        bonus += 3; notes.append("contract_verified")
    if sec["lp_locked"] is not None:
        if _truthy(sec["lp_locked"]):
            bonus += 6; notes.append("lp_locked")
        else:
            penalty += 8; notes.append("lp_unlocked")
    if sec["socials"]:
        bonus += min(4.0, 1.5 * len(sec["socials"]))
        notes.append("socials:" + ",".join(sec["socials"]))
    if sec["gt_score"] is not None:
        s["gt_score"] = _num(sec["gt_score"])
        if s["gt_score"] >= 60:
            bonus += 5; notes.append(f"gt_score={s['gt_score']:.0f}")
        elif s["gt_score"] < 30:
            penalty += 5; notes.append("low_gt_score")

    # ── concentration / dev risk ---------------------------------------------
    # Early runners are concentrated by nature (that is the pump mechanism), so
    # "pump" stance rewards it. "rug" stance prefers distribution.
    if sec["top10_pct"]:
        s["top10"] = sec["top10_pct"]
        if f.holder_stance == "pump":
            if sec["top10_pct"] >= 70:
                bonus += 8; notes.append(f"concentrated_supply={sec['top10_pct']:.0f}%")
            elif sec["top10_pct"] >= 45:
                bonus += 5; notes.append(f"concentrated_supply={sec['top10_pct']:.0f}%")
            elif sec["top10_pct"] >= 30:
                bonus += 2
            elif sec["top10_pct"] <= 15:
                bonus += 2; notes.append("well_distributed")
        else:
            if sec["top10_pct"] <= 25:
                bonus += 6; notes.append("well_distributed")
            elif sec["top10_pct"] >= 45:
                penalty += 10; notes.append(f"top10={sec['top10_pct']:.0f}%")
    if sec["creator_pct"] > 0:
        if f.holder_stance == "pump":
            if 3 <= sec["creator_pct"] <= 30:
                bonus += 3; notes.append(f"dev_skin={sec['creator_pct']:.1f}%")
            elif sec["creator_pct"] > 30:
                penalty += 4; notes.append(f"dev={sec['creator_pct']:.1f}%")
        else:
            if sec["creator_pct"] <= 1:
                bonus += 3; notes.append("dev_holding_ok")
            elif sec["creator_pct"] >= 3:
                penalty += 8; notes.append(f"dev={sec['creator_pct']:.1f}%")

    # ── late-entry / distribution guards -------------------------------------
    if chg_1h > 300:
        penalty += 8; notes.append(f"already+{chg_1h:.0f}%_1h")
    if chg_5m > 120:
        penalty += 4; notes.append("5m_spike>120%")
    if chg_5m < -15:
        penalty += 6; notes.append("5m_dump")

    # ── paid promotion is not organic demand ---------------------------------
    boosted = bool(pair.get("boosts")) or str(pair.get("source", "")).startswith(
        ("dexscreener_boost", "dexscreener_profile")
    )
    if boosted:
        penalty += 10; notes.append("paid_boost")

    # ── pool / quote quality --------------------------------------------------
    dex_id = str(pair.get("dexId") or "").lower()
    if dex_id in _GOOD_DEXES:
        bonus += 2
    if quote_symbol in _STABLE_QUOTES:
        bonus += 2
    if vol_24h and vol_6h and _ratio(vol_6h, vol_24h) < 0.05 and vol_24h > 100_000:
        penalty += 4; notes.append("volume_died")

    bonus = max(0.0, min(bonus, f.max_bonus))
    penalty = max(0.0, min(penalty, f.max_penalty))
    return bonus, penalty, s, notes


# ─────────────────────────────────────────────────────────────────────────────
# top-level verdict
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Verdict:
    rejected: bool
    reject_reasons: List[str] = field(default_factory=list)
    bonus: float = 0.0
    penalty: float = 0.0
    signals: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def delta(self) -> float:
        return self.bonus - self.penalty

    def __str__(self) -> str:  # pragma: no cover - convenience only
        if self.rejected:
            return f"REJECT({', '.join(self.reject_reasons)})"
        return f"+{self.bonus:.0f}/-{self.penalty:.0f} [{', '.join(self.notes)}]"


def evaluate(
    pair: dict,
    *,
    security: Optional[dict] = None,
    gt: Optional[dict] = None,
    history: Optional[PairHistory] = None,
    filters: Optional[Filters] = None,
) -> Verdict:
    """Run hard filters, then runner/penalty signals, in one call."""
    reasons = hard_reject_reasons(pair, security=security, gt=gt, filters=filters)
    if reasons:
        return Verdict(rejected=True, reject_reasons=reasons)
    bonus, penalty, signals, notes = runner_signals(
        pair, security=security, gt=gt, history=history, filters=filters
    )
    return Verdict(
        rejected=False, bonus=bonus, penalty=penalty, signals=signals, notes=notes
    )
