"""Audit the hand-tuned score components against realised forward returns.

The question this answers is the one the boar miss raises: **do the component
points the model awards actually correspond to a higher forward return?** It
does not need the bot's feature DB and it does not need the bot to be running —
it rebuilds the trailing volume/price windows from public GeckoTerminal OHLCV
(5-minute candles, ~83 h of history) and measures what the price did next.

What it can and cannot test, stated up front:

* **Testable from candles:** ``score_5m_1h`` (8 pts), ``score_1h_6h`` (7 pts),
  ``score_6h_24h`` (5 pts), ``score_price`` (15 pts) — 35 of the 100 points.
  The bot's own scoring functions are imported and called, so the tiers are
  reproduced exactly rather than re-implemented.
* **Not testable from candles:** ``score_vol_liq`` (10 pts, needs liquidity
  history), buy-pressure 5m/1h (20 pts, needs per-window transaction counts),
  holders (20 pts), security (10), CEX (5). Their absence is stated with every
  result.

Method: for each pool, every ``--step`` candles form one observation (default
every 12 candles = 1 h, non-overlapping in the 1 h horizon). Each observation
carries the trailing windows exactly as the bot would see them at that moment,
and the forward maximum return over the next 1 h and 6 h. Because adjacent
observations inside a pool are autocorrelated, the headline statistic is a
**per-pool sign test**: within each pool, are forward returns higher when the
component awarded points than when it awarded none? That is robust to
within-pool dependence and to pool-level survivorship, since it never compares
one pool to another.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GT = "https://api.geckoterminal.com/api/v2"
NETWORK = "base"

# Majors the bot's own filters skip (signals.is_major_asset); excluded so the
# sample looks like the tokens the scanner actually scores.
MAJOR_SYMBOLS = {"WETH", "USDC", "USDT", "DAI", "cbBTC", "WBTC", "WSTETH"}


# ─────────────────────────────────────────────────────────────────────────────
# pure statistics (kept dependency-free so they are unit-testable offline)
# ─────────────────────────────────────────────────────────────────────────────

def _ranks(values: Sequence[float]) -> List[float]:
    """Average ranks (1-based), ties shared."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Spearman rank correlation; None when a series is constant."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def auc(scores: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Probability a positive outranks a negative (Mann-Whitney), 0.5 = noise."""
    pos = [s for s, lab in zip(scores, labels) if lab]
    neg = [s for s, lab in zip(scores, labels) if not lab]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def binom_two_sided(wins: int, n: int) -> Optional[float]:
    """Exact two-sided sign-test p-value (H0: p = 0.5)."""
    if n == 0:
        return None
    total = 2 ** n
    tail = sum(math.comb(n, k) for k in range(0, min(wins, n - wins) + 1))
    return min(1.0, 2 * tail / total)


# ─────────────────────────────────────────────────────────────────────────────
# observation building
# ─────────────────────────────────────────────────────────────────────────────

def _sum_range(volumes: Sequence[float], end: int, count: int) -> float:
    start = end - count + 1
    if start < 0:
        return float("nan")
    return float(sum(volumes[start:end + 1]))


def _sum_partial(volumes: Sequence[float], end: int, count: int) -> float:
    """Trailing sum clamped to the available history.

    For a pool younger than the window, GeckoTerminal reports the cumulative
    figure since creation (its ``volume_usd.h24`` is not really 24 hours for a
    6-hour-old pool), so clamping mirrors what the bot actually received at the
    time rather than inventing data.
    """
    return float(sum(volumes[max(0, end - count + 1):end + 1]))


def to_grid(candles: Sequence[Sequence[float]], step_s: int = 300) -> List[List[float]]:
    """Align candles to a contiguous ``step_s`` grid.

    GeckoTerminal omits bars with no trades, so the raw list is *not* evenly
    spaced: for boar, 426 candles span 467 five-minute slots. Index-based
    windows (``volumes[t-11:t+1]``) would then silently measure 70 minutes
    instead of 60. Missing slots are filled with zero volume and a carried-forward
    close, which is what "no trades in this window" actually means.
    """
    by_ts = {int(float(c[0])): c for c in candles}
    stamps = sorted(by_ts)
    if not stamps:
        return []
    grid: List[List[float]] = []
    close = high = low = open_ = None
    for t in range(stamps[0], stamps[-1] + 1, step_s):
        bar = by_ts.get(t)
        if bar is not None:
            open_, high, low, close = (float(bar[1]), float(bar[2]),
                                       float(bar[3]), float(bar[4]))
            grid.append([float(t), open_, high, low, close, float(bar[5])])
        elif close is not None:
            grid.append([float(t), close, close, close, close, 0.0])
    return grid


def build_observations(candles: Sequence[Sequence[float]], created_ts: float,
                       *, step: int = 12, lookback: int = 288,
                       horizon: int = 12, allow_partial: bool = False) -> List[Dict[str, float]]:
    """One dict per sampled candle, carrying trailing windows and forward move.

    ``candles`` is GeckoTerminal's ``[ts, open, high, low, close, volume]`` list,
    oldest first. It is first aligned to a 5-minute grid (see :func:`to_grid`) so
    every window is a real time window. An observation is taken at the bar's
    close, and forward returns use the bars strictly after it.

    ``allow_partial`` clamps the trailing windows to the available history, for
    reconstructing a young pool's earliest bars. With it off (the default) a bar
    without a full 24 h of history is skipped, which keeps the pooled statistics
    honest.
    """
    candles = to_grid(candles)
    if len(candles) < lookback + horizon + 2:
        if not allow_partial or len(candles) < lookback + horizon:
            return []

    ts = [float(c[0]) for c in candles]
    highs = [float(c[2]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    window = _sum_partial if allow_partial else _sum_range

    def back(idx: int, count: int) -> float:
        """Close ``count`` bars before ``idx``; NaN when history is too short."""
        j = idx - count
        if j < 0:
            if not allow_partial:
                return float("nan")
            j = 0
        return closes[j]

    out: List[Dict[str, float]] = []
    for t in range(lookback, len(candles) - horizon):
        if (t - lookback) % step:
            continue
        vol_1h = window(volumes, t, 12)
        vol_6h = window(volumes, t, 72)
        vol_24h = window(volumes, t, 288)
        if not (vol_1h > 0 and vol_6h > 0 and vol_24h > 0):
            continue
        close = closes[t]
        if close <= 0:
            continue
        fwd_high_1h = max(highs[t + 1:t + 1 + horizon])
        fwd_high_6h = max(highs[t + 1:t + 1 + 6 * horizon])
        c1, c12, c72 = closes[t - 1], back(t, 12), back(t, 72)
        out.append({
            "ts": ts[t] + 300.0,
            "age_minutes": (ts[t] + 300.0 - created_ts) / 60.0,
            "vol_5m": volumes[t],
            "vol_1h": vol_1h,
            "vol_6h": vol_6h,
            "vol_24h": vol_24h,
            "vol_5m_1h": volumes[t] / vol_1h,
            "vol_1h_6h": vol_1h / vol_6h,
            "vol_6h_24h": vol_6h / vol_24h,
            "chg_5m": (close / c1 - 1.0) * 100.0 if c1 else 0.0,
            "chg_1h": (close / c12 - 1.0) * 100.0 if c12 and c12 == c12 else 0.0,
            "chg_6h": (close / c72 - 1.0) * 100.0 if c72 and c72 == c72 else 0.0,
            "fwd_max_1h": (fwd_high_1h / close - 1.0) * 100.0,
            "fwd_max_6h": (fwd_high_6h / close - 1.0) * 100.0,
        })
    return out


def score_observations(observations: Sequence[Dict[str, float]]) -> None:
    """Attach the bot's real component points to each observation, in place."""
    sys.path.insert(0, ROOT)
    import bot  # imported lazily so the statistics above stay testable offline

    for obs in observations:
        age = obs["age_minutes"]
        obs["score_vol_5m_1h"] = bot.score_5m_1h(obs["vol_5m"], obs["vol_1h"], age)
        obs["score_vol_1h_6h"] = bot.score_1h_6h(obs["vol_1h"], obs["vol_6h"], age)
        obs["score_vol_6h_24h"] = bot.score_6h_24h(obs["vol_6h"], obs["vol_24h"], age)
        obs["score_price"] = bot.score_price(
            obs["chg_5m"], obs["chg_1h"], obs["chg_6h"], age
        )
        obs["score_total_testable"] = (
            obs["score_vol_5m_1h"] + obs["score_vol_1h_6h"]
            + obs["score_vol_6h_24h"] + obs["score_price"]
        )


# ─────────────────────────────────────────────────────────────────────────────
# fetching
# ─────────────────────────────────────────────────────────────────────────────

def _get(url: str, cache_path: Optional[str] = None, pause: float = 2.2) -> Optional[dict]:
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as fh:
            return json.load(fh)
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "sniper-bot-audit"}), timeout=30) as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - a missing pool must not kill the run
        print(f"    ! {exc}", file=sys.stderr)
        return None
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as fh:
            json.dump(data, fh)
    time.sleep(pause)
    return data


def pool_universe(pages: int, cache_dir: str) -> List[dict]:
    """Trending + top-volume pools, majors and thin pools removed."""
    out: Dict[str, dict] = {}
    for kind, path in (("trending", "trending_pools"),
                       ("top_volume", "pools?sort=h24_volume_usd_desc")):
        for page in range(1, pages + 1):
            sep = "&" if "?" in path else "?"
            url = f"{GT}/networks/{NETWORK}/{path}{sep}page={page}"
            cache = os.path.join(cache_dir, f"list_{kind}_{page}.json")
            data = _get(url, cache)
            for pool in (data or {}).get("data") or []:
                attrs = pool.get("attributes") or {}
                name = str(attrs.get("name") or "")
                base_symbol = name.split("/")[0].strip().upper()
                if base_symbol in MAJOR_SYMBOLS:
                    continue
                if float(attrs.get("reserve_in_usd") or 0) < 8_000:
                    continue
                out[attrs.get("address")] = {
                    "address": attrs.get("address"),
                    "name": name,
                    "created": attrs.get("pool_created_at"),
                    "liq": float(attrs.get("reserve_in_usd") or 0),
                    "source": kind,
                }
    return list(out.values())


def fetch_candles(pool: str, cache_dir: str) -> Optional[list]:
    url = (f"{GT}/networks/{NETWORK}/pools/{pool}/ohlcv/minute"
           f"?aggregate=5&limit=1000&currency=usd&token=base")
    cache = os.path.join(cache_dir, f"ohlcv_{pool}.json")
    data = _get(url, cache)
    if not data:
        return None
    rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted(rows, key=lambda r: r[0]) or None


# ─────────────────────────────────────────────────────────────────────────────
# reporting
# ─────────────────────────────────────────────────────────────────────────────

COMPONENTS = ["score_vol_5m_1h", "score_vol_1h_6h", "score_vol_6h_24h",
              "score_price", "score_total_testable"]
RAW_RATIOS = ["vol_5m_1h", "vol_1h_6h", "vol_6h_24h"]


def summarise(per_pool: Dict[str, List[Dict[str, float]]], horizon: str) -> None:
    """Print ranking stats per component, pooled and per pool.

    Pooled statistics are reported for completeness, but the inference rests on
    the per-pool sign tests: observations inside a pool overlap and are
    autocorrelated, so a pooled p-value would be far too optimistic.
    """
    fwd = f"fwd_max_{horizon}"
    all_obs = [o for rows in per_pool.values() for o in rows]
    thresholds = {"1h": (10.0, 25.0, 50.0), "6h": (25.0, 50.0, 100.0)}[horizon]

    print(f"\n=== forward horizon {horizon} — {len(all_obs)} observations "
          f"from {len(per_pool)} pools ===")
    print(f"{'component':22s} {'rho_pool':>9s} {'rho_poolmean':>13s} "
          f"{'pools+/-':>9s} {'p_sign':>7s} {'mean>0':>8s} {'mean=0':>8s} {'n>0':>6s}")

    for name in COMPONENTS + RAW_RATIOS:
        xs = [o[name] for o in all_obs]
        ys = [o[fwd] for o in all_obs]
        pooled_rho = spearman(xs, ys)

        per_rhos: List[float] = []
        for rows in per_pool.values():
            if len(rows) >= 10:
                r = spearman([o[name] for o in rows], [o[fwd] for o in rows])
                if r is not None:
                    per_rhos.append(r)
        wins = sum(1 for r in per_rhos if r > 0)
        losses = sum(1 for r in per_rhos if r < 0)
        p_sign = binom_two_sided(wins, wins + losses)

        nonzero = [o[fwd] for o in all_obs if o[name] > 0]
        zero = [o[fwd] for o in all_obs if o[name] <= 0]
        print(f"{name:22s} {pooled_rho if pooled_rho is None else round(pooled_rho, 3):>9} "
              f"{(round(statistics.fmean(per_rhos), 3) if per_rhos else float('nan')):>13} "
              f"{f'{wins}/{losses}':>9} "
              f"{(round(p_sign, 4) if p_sign is not None else float('nan')):>7} "
              f"{(round(statistics.fmean(nonzero), 1) if nonzero else float('nan')):>8} "
              f"{(round(statistics.fmean(zero), 1) if zero else float('nan')):>8} "
              f"{len(nonzero):>6d}")

    print(f"\n  AUC — can the component rank a big mover? (0.5 = coin flip)")
    for threshold in thresholds:
        labels = [1 if o[fwd] >= threshold else 0 for o in all_obs]
        if not any(labels) or all(labels):
            print(f"    >= {threshold:>5.0f}% forward: no class balance "
                  f"({sum(labels)}/{len(labels)} positives) — skipped")
            continue
        parts = []
        for name in COMPONENTS:
            a = auc([o[name] for o in all_obs], labels)
            parts.append(f"{name.replace('score_', '')}={a:.3f}")
        print(f"    >= {threshold:>5.0f}% forward ({sum(labels):>4d}/{len(labels)}): "
              + "  ".join(parts))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pages", type=int, default=2,
                        help="pages per listing source (20 pools/page)")
    parser.add_argument("--step", type=int, default=12,
                        help="candles between observations (12 = 1h)")
    parser.add_argument("--max-pools", type=int, default=80)
    parser.add_argument("--cache-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".cache"))
    parser.add_argument("--csv", default="")
    args = parser.parse_args()

    pools = pool_universe(args.pages, args.cache_dir)[:args.max_pools]
    print(f"{len(pools)} candidate pools after removing majors and thin pools")

    per_pool: Dict[str, List[Dict[str, float]]] = {}
    for i, pool in enumerate(pools, 1):
        candles = fetch_candles(pool["address"], args.cache_dir)
        if not candles:
            continue
        created = 0.0
        try:
            created = time.mktime(time.strptime(pool["created"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except Exception:
            pass
        obs = build_observations(candles, created, step=args.step)
        if not obs:
            continue
        score_observations(obs)
        for row in obs:
            row["pool"] = pool["address"]
            row["symbol"] = pool["name"]
        per_pool[pool["address"]] = obs
        print(f"  [{i}/{len(pools)}] {pool['name'][:34]:34s} "
              f"{len(candles):>4d} candles -> {len(obs):>3d} obs")

    if not per_pool:
        print("no usable pools — nothing to report", file=sys.stderr)
        return 2

    for horizon in ("1h", "6h"):
        summarise(per_pool, horizon)

    if args.csv:
        import csv
        rows = [o for v in per_pool.values() for o in v]
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwrote {len(rows)} rows -> {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
