#!/usr/bin/env python3
"""Derive the early-lane floors from the population, not from chosen tokens.

Why this exists
---------------
The early-lane floors used to be set by replaying two tokens that were already
known to have run (a confirmation/survivorship error: they were selected *because*
their outcome was known, then used to set the filter that was supposed to predict
outcomes). ``n=2`` from an ``n=20`` snapshot cannot support a threshold.

This script replaces that with a procedure that never looks at an outcome:

1. sample the population the scanner actually sees — ``new_pools`` cohorts as
   they are born, across every chain, several pages deep. These are *unselected*
   pools: they are not filtered by having run, so the sample is not survivorship
   biased;
2. drop only majors (an existing structural rule in ``signals.is_major_asset``)
   and pools older than the early-lane age cap (the lane is defined on young
   pools);
3. report the quantiles of each participation metric and set the floor at a
   stated quantile (default p75 = "keep the top quarter of births").

That is objective in the sense that matters here: **reproducible, outcome-blind,
and a property of the population rather than of any named token.** It is *not* a
claim about forward returns — ``diag/component_audit.py`` measured the underlying
components at AUC ~0.59 for a >=10% move and ~0.24 for >=100%, i.e. close to
useless for the fat tail. Thresholds that predict outcomes can only be fitted on
labelled data; see ``diag/fit_thresholds.py``, which refuses to fit until enough
labelled rows exist.

Provenance rule: every number emitted here carries the sample size, date and the
command that produced it. A floor without that provenance is an opinion.

Usage
-----
    bot-env/bin/python diag/population_floors.py --pages 2 --quantile 0.75
    bot-env/bin/python diag/population_floors.py --chains robinhood,base --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

GT = "https://api.geckoterminal.com/api/v2"
NETWORK_SLUGS = {"ethereum": "eth", "bsc": "bsc", "base": "base", "robinhood": "robinhood"}

# Majors are skipped by the scanner itself (signals.is_major_asset), so they are
# not part of the population the early lane is asked to judge.
MAJOR_SYMBOLS = {
    "WETH", "WBNB", "WMATIC", "WAVAX", "WSOL", "USDC", "USDT", "DAI", "BUSD",
    "TUSD", "FDUSD", "USDE", "SUSDE", "WBTC", "CBBTC", "TBTC", "WSTETH",
    "STETH", "RETH", "WEETH", "CAKE", "UNI", "AAVE", "LINK", "MKR", "CRV", "LDO",
}

# The metrics the early lane actually thresholds, and the env key each floor sets.
METRIC_ENV_KEYS = {
    "liquidity_usd": "SIG_EARLY_MIN_LIQUIDITY_USD",
    "vol_liq_ratio": "SIG_EARLY_MIN_VOL_LIQ",
    "txns_5m": "SIG_EARLY_MIN_TXNS_5M",
    "buy_ratio_5m": "SIG_EARLY_MIN_BUY_RATIO",
    "buyers_5m": "SIG_EARLY_MIN_UNIQUE_BUYERS",
    "age_minutes": "SIG_EARLY_MAX_AGE_MINUTES",
}

# Floors that are "at least this much" are taken at the quantile; the age cap is
# the opposite direction (keep pools *younger* than the quantile).
ASCENDING = {"liquidity_usd", "vol_liq_ratio", "txns_5m", "buy_ratio_5m", "buyers_5m"}


# ─────────────────────────────────────────────────────────────────────────────
# pure statistics (dependency-free so they are unit-testable offline)
# ─────────────────────────────────────────────────────────────────────────────


def quantile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation quantile (the numpy/statistics convention).

    Implemented here rather than via ``statistics.quantiles`` because that
    function needs n >= 2 and excludes the extremes, which is wrong for a
    threshold-setting job where q=0.75 of a small sample must still be defined.
    """
    xs = sorted(float(v) for v in values if v is not None and not math.isnan(float(v)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    q = min(1.0, max(0.0, float(q)))
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def bootstrap_quantile_ci(
    values: Sequence[float],
    q: float,
    *,
    iterations: int = 1000,
    seed: int = 12345,
    alpha: float = 0.05,
) -> Tuple[Optional[float], Optional[float]]:
    """Percentile bootstrap CI for a quantile.

    Reported alongside every floor because with n in the tens the quantile is
    itself uncertain, and a floor quoted without that uncertainty invites the
    same overconfidence the two-token version had.
    """
    xs = [float(v) for v in values if v is not None]
    if len(xs) < 3:
        return (None, None)
    rng = random.Random(seed)
    n = len(xs)
    stats = []
    for _ in range(iterations):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        stats.append(quantile(sample, q))
    stats = sorted(s for s in stats if s is not None)
    if not stats:
        return (None, None)
    lo = stats[int((alpha / 2) * (len(stats) - 1))]
    hi = stats[int((1 - alpha / 2) * (len(stats) - 1))]
    return (lo, hi)


def pool_metrics(pool: dict) -> Optional[Dict[str, float]]:
    """Extract the early-lane metrics from one GeckoTerminal pool resource.

    Returns None when the pool cannot be described by the lane's inputs at all
    (no address, or no usable age), so those rows are dropped explicitly rather
    than silently counted as zeros.
    """
    attrs = (pool or {}).get("attributes") or {}
    if not attrs.get("address"):
        return None

    created = attrs.get("pool_created_at")
    age_minutes = None
    if created:
        try:
            text = str(created).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        except (TypeError, ValueError):
            age_minutes = None
    if age_minutes is None:
        return None

    liquidity = float(attrs.get("reserve_in_usd") or 0)
    volume = attrs.get("volume_usd") or {}
    vol_5m = float(volume.get("m5") or 0)
    txns = (attrs.get("transactions") or {}).get("m5") or {}
    buys = float(txns.get("buys") or 0)
    sells = float(txns.get("sells") or 0)
    buyers = float(txns.get("buyers") or 0)
    total = buys + sells

    return {
        "age_minutes": age_minutes,
        "liquidity_usd": liquidity,
        "vol_5m": vol_5m,
        "vol_liq_ratio": (vol_5m / liquidity) if liquidity > 0 else 0.0,
        "txns_5m": total,
        "buy_ratio_5m": (buys / total) if total > 0 else 0.0,
        "buyers_5m": buyers,
    }


def base_symbol(pool: dict) -> str:
    name = str(((pool or {}).get("attributes") or {}).get("name") or "")
    return name.split("/")[0].strip().upper()


def select_cohort(
    pools: Sequence[dict],
    *,
    max_age_minutes: float,
    chain_of: Dict[str, str],
) -> List[Dict[str, float]]:
    """Outcome-blind cohort selection.

    The only exclusions are structural: majors (the scanner skips them too) and
    pools already older than the lane's age definition. Nothing here consults
    price or subsequent performance, which is what keeps the sample free of
    survivorship bias.
    """
    out: List[Dict[str, float]] = []
    for pool in pools:
        addr = ((pool or {}).get("attributes") or {}).get("address")
        if base_symbol(pool) in MAJOR_SYMBOLS:
            continue
        metrics = pool_metrics(pool)
        if metrics is None:
            continue
        if metrics["age_minutes"] > max_age_minutes:
            continue
        metrics["chain"] = chain_of.get(addr, "?")
        out.append(metrics)
    return out


def summarise(rows: Sequence[Dict[str, float]], *, quantile_map: Dict[str, float]) -> Dict[str, dict]:
    """Per-metric quantiles + bootstrap CI, for the metrics the lane uses.

    ``age_minutes`` is reported but flagged ``censored``: the age of pools in a
    ``new_pools`` response is bounded by how deep we paginated into the feed and
    by our own cohort filter, not by any property of the pools. A quantile of a
    censored variable is meaningless as a threshold, so it is excluded from the
    derived floors and shown only as coverage.
    """
    summary: Dict[str, dict] = {}
    for metric, env_key in METRIC_ENV_KEYS.items():
        values = [r[metric] for r in rows if r.get(metric) is not None]
        if not values:
            summary[metric] = {"env": env_key, "n": 0}
            continue
        q = quantile_map.get(metric, quantile_map["_default"])
        lo, hi = bootstrap_quantile_ci(values, q)
        summary[metric] = {
            "env": env_key,
            "n": len(values),
            "q": q,
            "p50": quantile(values, 0.50),
            "p75": quantile(values, 0.75),
            "p90": quantile(values, 0.90),
            "min": min(values),
            "max": max(values),
            "value": quantile(values, q),
            "ci_low": lo,
            "ci_high": hi,
            "mean": statistics.fmean(values),
            "censored": metric == "age_minutes",
        }
    return summary


def format_env_block(summary: Dict[str, dict], *, chain: str, n: int, date: str, command: str) -> str:
    """Render the floors as .env lines with provenance and no token references."""
    lines = [
        f"# Derived from the {chain} new_pools cohort (n={n}) on {date}.",
        f"# Reproduce: {command}",
        "# Objective and outcome-blind: a quantile of the population the scanner",
        "# sees, not a value chosen to admit any particular token. The quantile is",
        "# a POLICY choice (alert volume vs precision) — outcomes are needed to",
        "# pick it, and diag/fit_thresholds.py refuses until they exist.",
    ]
    order = [
        "liquidity_usd", "vol_liq_ratio", "txns_5m", "buy_ratio_5m",
        "buyers_5m", "age_minutes",
    ]
    for metric in order:
        info = summary.get(metric) or {}
        if not info.get("value") and info.get("value") != 0:
            lines.append(f"# {metric}: no data")
            continue
        if info.get("censored"):
            lines.append(
                f"# {metric}: NOT derived (censored by feed depth — observed "
                f"{info['min']:.0f}-{info['max']:.0f}m). Set the early-lane age cap "
                f"from the feed window you actually scan, not from a quantile."
            )
            continue
        value = info["value"]
        env = info["env"]
        if metric in ("txns_5m", "buyers_5m"):
            rendered = str(int(math.ceil(value)))
        elif metric == "liquidity_usd":
            rendered = str(int(round(value, -2)))
        else:
            rendered = f"{value:.3g}"
        ci = ""
        if info.get("ci_low") is not None:
            ci = f"  # 95% CI {info['ci_low']:.3g}-{info['ci_high']:.3g}, n={info['n']}"
        lines.append(f"{env}={rendered}{ci}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# network
# ─────────────────────────────────────────────────────────────────────────────


def _get(url: str, cache_path: Optional[str] = None, pause: float = 2.2) -> Optional[dict]:
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as fh:
            return json.load(fh)
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "sniper-bot-audit"}), timeout=30) as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - a missing page must not kill the run
        print(f"    ! {exc}", file=sys.stderr)
        return None
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as fh:
            json.dump(data, fh)
    time.sleep(pause)
    return data


def fetch_population(
    chains: Sequence[str],
    *,
    pages: int,
    sources: Sequence[str],
    cache_dir: str,
    stamp: str,
) -> Tuple[List[dict], Dict[str, str]]:
    """Sample each chain's feeds. Returns (pools, address -> chain)."""
    pools: List[dict] = []
    chain_of: Dict[str, str] = {}
    endpoints = {
        "new_pools": "new_pools",
        "trending": "trending_pools",
        "top_volume": "pools?sort=h24_volume_usd_desc",
    }
    for chain in chains:
        network = NETWORK_SLUGS.get(chain)
        if not network:
            continue
        for source in sources:
            path = endpoints.get(source)
            if not path:
                continue
            for page in range(1, pages + 1):
                sep = "&" if "?" in path else "?"
                url = f"{GT}/networks/{network}/{path}{sep}page={page}"
                cache = os.path.join(cache_dir, f"{stamp}_{chain}_{source}_{page}.json")
                data = _get(url, cache)
                batch = (data or {}).get("data") or []
                print(f"  {chain:10s} {source:11s} p{page}: {len(batch)} pools")
                for pool in batch:
                    addr = (pool.get("attributes") or {}).get("address")
                    if addr:
                        chain_of[addr] = chain
                    pools.append(pool)
    return pools, chain_of


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chains", default="robinhood,base,bsc,ethereum")
    parser.add_argument("--sources", default="new_pools",
                        help="comma list of new_pools,trending,top_volume")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--quantile", type=float, default=0.75,
                        help="population quantile the floors sit at (default 0.75)")
    parser.add_argument("--max-age-minutes", type=float, default=30.0,
                        help="cohort definition: pools at most this old")
    parser.add_argument("--cache-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".cache"))
    parser.add_argument("--json", default=None, help="also write the summary as JSON")
    args = parser.parse_args()

    chains = [c.strip() for c in args.chains.split(",") if c.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")

    print(f"Sampling {','.join(chains)} sources={','.join(sources)} pages={args.pages}")
    pools, chain_of = fetch_population(
        chains, pages=args.pages, sources=sources,
        cache_dir=args.cache_dir, stamp=stamp,
    )
    cohort = select_cohort(pools, max_age_minutes=args.max_age_minutes, chain_of=chain_of)
    print(f"\nSampled {len(pools)} pools; cohort {len(cohort)} "
          f"(majors and pools older than {args.max_age_minutes:g}m excluded)")

    if not cohort:
        print("No cohort rows — nothing to derive. Re-run when the feeds are reachable.",
              file=sys.stderr)
        return 1

    quantile_map = {"_default": args.quantile}
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Provenance must reproduce the exact cohort, chain filter included: a block
    # labelled "all" when only Robinhood was sampled is the same class of error
    # as a pinned token.
    command = (
        f"bot-env/bin/python diag/population_floors.py "
        f"--chains {','.join(chains)} --sources {','.join(sources)} "
        f"--pages {args.pages} --quantile {args.quantile}"
    )

    payload = {"generated": date, "cohort_n": len(cohort), "quantile": args.quantile,
               "chains": chains, "sources": sources, "per_chain": {}}

    # Pooled, then per chain — the floors are per-chain env, so both matter.
    groups: Dict[str, List[Dict[str, float]]] = {"__all__": cohort}
    for row in cohort:
        groups.setdefault(row.get("chain", "?"), []).append(row)

    for name, rows in groups.items():
        summary = summarise(rows, quantile_map=quantile_map)
        payload["per_chain"][name] = summary
        label = "POOLED (all sampled chains)" if name == "__all__" else name
        print(f"\n{'=' * 72}\n{label}  n={len(rows)}\n{'=' * 72}")
        print(f"{'metric':16s} {'n':>5s} {'p50':>12s} {'p75':>12s} {'p90':>12s} "
              f"{'floor@q':>12s}  95% CI")
        for metric in METRIC_ENV_KEYS:
            info = summary.get(metric) or {}
            if not info.get("n"):
                print(f"{metric:16s} {0:>5d}  (no data)")
                continue
            ci = (f"{info['ci_low']:.4g}-{info['ci_high']:.4g}"
                  if info.get("ci_low") is not None else "n/a")
            mark = " *" if info.get("censored") else "  "
            print(f"{metric:16s} {info['n']:>5d} {info['p50']:>12.4g} "
                  f"{info['p75']:>12.4g} {info['p90']:>12.4g} {info['value']:>12.4g}  {ci}{mark}")
        print("  (* censored: bounded by feed depth, not a pool property — not derived)")

    print("\n" + "=" * 72)
    print("Suggested .env blocks, one per cohort — copy the one matching the feeds")
    print("your config actually enables. The early lane only sees pools that reach")
    print("it as young, which in the shipped config means Robinhood new_pools.")
    print("=" * 72)
    for name, rows in groups.items():
        label = "POOLED (every sampled chain)" if name == "__all__" else name
        print(f"\n--- {label} (n={len(rows)}) ---")
        print(format_env_block(payload["per_chain"][name], chain=label,
                               n=len(rows), date=date, command=command))
    print()
    print("CAVEAT, stated so this is not mistaken for calibration: these are")
    print("cross-sectional quantiles of the cohort the scanner sees. They are")
    print("outcome-blind and reproducible, but they do not claim the floors")
    print("predict forward returns. That requires labelled data — see")
    print("diag/fit_thresholds.py, which refuses to fit below its sample gate.")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=1)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
