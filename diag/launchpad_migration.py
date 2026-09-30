#!/usr/bin/env python3
"""Do launchpad tokens later get an AMM pool, and how soon?

The strategic question: if a token is born on a launchpad (`four-meme`, `pons-v2`,
`bankr`, `o1-launchpad`, ...) and the bot cannot trade that venue, is it worth
integrating Uniswap/PancakeSwap instead and buying on the AMM a bit later? That
depends on two measurable things:

* **the migration rate** — what share of launchpad-born tokens ever get an AMM
  pool at all;
* **the migration lag** — how long after the birth pool that AMM pool appears.

Both are measured here per launchpad venue, with the lag reported as a
distribution rather than a mean, and with age bucketing so the result is not
distorted by right-censoring (a token born 10 minutes ago cannot yet show a
6-hour migration).

Honest limitations, stated up front:

* **Survivor bias.** The universe comes from `trending`/`top_volume`, because a
  token's full pool history is only reachable once we know its address. Tokens
  that died on the launchpad and were never indexed are absent. That biases the
  migration rate *upwards*; the tool prints the birth-venue mix so the selection
  is visible rather than implied. It does not bias the lag measurement much for
  tokens that did migrate, which is the actionable number.
* **Indexer coverage.** A pool GT does not index is invisible here, so "never
  migrated" means "not observable via GeckoTerminal".
* **A birth pool is the earliest pool we can see**, which may not be the true
  first pool if GT indexed late.

Usage
-----
    bot-env/bin/python diag/launchpad_migration.py --chain bsc --pages 3 --limit 40
    bot-env/bin/python diag/launchpad_migration.py --chain robinhood --pages 3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

GT = "https://api.geckoterminal.com/api/v2"
NETWORK_SLUGS = {"ethereum": "eth", "bsc": "bsc", "base": "base", "robinhood": "robinhood"}

# Venues that are real AMMs the bot could route through if the router existed.
AMM_PATTERNS = (
    r"^uniswap$", r"uniswap[-_]?v?[234]", r"^pancakeswap$", r"pancakeswap",
    r"sushi", r"biswap", r"thena", r"squadswap", r"aerodrome", r"baseswap",
    r"quickswap", r"apeswap", r"trader[-_]?joe", r"shibaswap", r"balancer",
)
# Venues where tokens are *born* and that the bot cannot currently trade.
LAUNCHPAD_PATTERNS = (r"four[-_]?meme", r"pons", r"bankr", r"o1[-_]?launchpad",
                      r"^up[-_]?v3", r"alandale", r"ramses")

AGE_BUCKETS: Sequence[Tuple[str, float, float]] = (
    ("<1h", 0.0, 60.0),
    ("1-6h", 60.0, 360.0),
    ("6-24h", 360.0, 1440.0),
    ("1-7d", 1440.0, 10080.0),
    (">7d", 10080.0, float("inf")),
)


def classify(dex: str) -> str:
    """'amm' if the venue is routable-in-principle, 'launchpad' if birth-only."""
    dex = str(dex or "").lower()
    for pat in LAUNCHPAD_PATTERNS:
        if re.search(pat, dex):
            return "launchpad"
    for pat in AMM_PATTERNS:
        if re.search(pat, dex):
            return "amm"
    return "other"


def _ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def birth_and_first_amm(pools: Sequence[dict]) -> Optional[dict]:
    """Earliest pool overall, earliest non-launchpad AMM pool, and the lag.

    ``migrated`` is False when no AMM pool is visible, which for a young token
    only means "not yet" — the caller must bucket by age before drawing a
    conclusion.
    """
    entries: List[Tuple[float, str]] = []
    for pool in pools or []:
        attrs = (pool or {}).get("attributes") or {}
        rels = (pool or {}).get("relationships") or {}
        dex = ((rels.get("dex") or {}).get("data") or {}).get("id") or "unknown"
        ts = _ts(attrs.get("pool_created_at"))
        if ts is not None:
            entries.append((ts, dex))
    if not entries:
        return None
    entries.sort()
    birth_ts, birth_dex = entries[0]
    amm = [(ts, dex) for ts, dex in entries if classify(dex) == "amm"]
    out = {
        "birth_ts": birth_ts,
        "birth_dex": birth_dex,
        "birth_kind": classify(birth_dex),
        "migrated": bool(amm),
        "amm_dex": amm[0][1] if amm else None,
        "lag_minutes": ((amm[0][0] - birth_ts) / 60.0) if amm else None,
        "pool_count": len(entries),
    }
    return out


def bucket_for(age_minutes: float) -> Optional[str]:
    for label, lo, hi in AGE_BUCKETS:
        if lo <= age_minutes < hi:
            return label
    return None


def summarise(records: Sequence[dict], now: float) -> Dict[str, dict]:
    """Per-birth-venue migration rate and lag distribution, plus age buckets."""
    out: Dict[str, dict] = {}
    for rec in records:
        venue = rec["birth_dex"]
        entry = out.setdefault(venue, {"n": 0, "migrated": 0, "lags": [],
                                       "buckets": {}})
        entry["n"] += 1
        age_min = (now - rec["birth_ts"]) / 60.0
        bucket = bucket_for(age_min)
        if bucket:
            b = entry["buckets"].setdefault(bucket, {"n": 0, "migrated": 0})
            b["n"] += 1
        if rec["migrated"]:
            entry["migrated"] += 1
            if rec["lag_minutes"] is not None:
                entry["lags"].append(rec["lag_minutes"])
            if bucket:
                entry["buckets"][bucket]["migrated"] += 1
    for venue, entry in out.items():
        lags = sorted(entry["lags"])
        entry["rate"] = entry["migrated"] / entry["n"] if entry["n"] else 0.0
        if lags:
            entry["lag_p25"] = lags[len(lags) // 4]
            entry["lag_median"] = statistics.median(lags)
            entry["lag_p75"] = lags[(3 * len(lags)) // 4]
            entry["lag_min"] = lags[0]
        else:
            entry["lag_p25"] = entry["lag_median"] = entry["lag_p75"] = entry["lag_min"] = None
    return out


def _get(url: str, cache: str, pause: float = 2.2, tries: int = 4) -> dict:
    """Cached GET with exponential backoff on 429.

    GeckoTerminal's free tier is ~30 calls/min and answers a burst with 429. A
    silent failure here truncates a token's pool history, which would look like
    "never migrated" — so retries matter for correctness, not just politeness.
    """
    if os.path.exists(cache):
        with open(cache) as fh:
            return json.load(fh)
    delay = pause
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "sniper-bot-audit"}), timeout=30) as fh:
                data = json.load(fh)
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w") as fh:
                json.dump(data, fh)
            time.sleep(pause)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == tries - 1:
                print(f"    ! {exc}", file=sys.stderr)
                return {}
            time.sleep(delay)
            delay *= 2
        except Exception as exc:  # noqa: BLE001
            print(f"    ! {exc}", file=sys.stderr)
            return {}
    return {}


def discover_tokens(chain: str, pages: int, cache_dir: str, stamp: str) -> List[str]:
    """Token addresses from the survivor feeds (see survivor-bias note)."""
    network = NETWORK_SLUGS.get(chain)
    tokens: List[str] = []
    seen = set()
    for kind, path in (("trending", "trending_pools"),
                       ("top", "pools?sort=h24_volume_usd_desc")):
        for page in range(1, pages + 1):
            sep = "&" if "?" in path else "?"
            url = f"{GT}/networks/{network}/{path}{sep}page={page}"
            cache = os.path.join(cache_dir, f"{stamp}_lm_{chain}_{kind}_{page}.json")
            data = _get(url, cache)
            for pool in (data.get("data") or []):
                rels = (pool or {}).get("relationships") or {}
                tid = ((rels.get("base_token") or {}).get("data") or {}).get("id") or ""
                if "_" in tid:
                    addr = tid.split("_", 1)[1]
                    if addr not in seen:
                        seen.add(addr)
                        tokens.append(addr)
    return tokens


def fetch_pools(chain: str, token: str, cache_dir: str, stamp: str,
                pause: float = 2.2) -> List[dict]:
    """Page 1, and page 2 only when page 1 was full (halves the call count)."""
    network = NETWORK_SLUGS.get(chain)
    out: List[dict] = []
    for page in (1, 2):
        url = f"{GT}/networks/{network}/tokens/{token}/pools?page={page}"
        cache = os.path.join(cache_dir, f"{stamp}_lm_pools_{chain}_{token}_{page}.json")
        batch = (_get(url, cache, pause=pause) or {}).get("data") or []
        out.extend(batch)
        if len(batch) < 20:
            break
    return out


def tokens_from_newpools_cache(chain: str, cache_dir: str, only_dex: str) -> List[str]:
    """Token addresses from already-cached new_pools pages, filtered by birth DEX.

    This is the unbiased way to ask the migration question: start from tokens we
    know were *born* on the launchpad, rather than from survivor feeds where
    launchpad-born tokens barely appear.
    """
    import glob
    tokens: List[str] = []
    seen = set()
    pattern = re.compile(only_dex, re.I)
    for path in sorted(glob.glob(os.path.join(cache_dir, f"*cov_{chain}_new_pools_*.json"))):
        with open(path) as fh:
            data = json.load(fh)
        for pool in (data.get("data") or []):
            rels = (pool or {}).get("relationships") or {}
            dex = ((rels.get("dex") or {}).get("data") or {}).get("id") or ""
            if not pattern.search(dex):
                continue
            tid = ((rels.get("base_token") or {}).get("data") or {}).get("id") or ""
            if "_" in tid:
                addr = tid.split("_", 1)[1]
                if addr not in seen:
                    seen.add(addr)
                    tokens.append(addr)
    return tokens



def discover_from_dex_pools(chain: str, dex: str, pages: int, cache_dir: str,
                            stamp: str, pause: float = 2.2) -> List[Tuple[str, float]]:
    """Tokens born on one DEX, with their pool creation time, from the DEX listing.

    ``/networks/{net}/dexes/{dex}/pools`` returns pools regardless of age, which
    is what makes an age-stratified cohort possible: sampling only the birth feed
    would yield nothing but minutes-old tokens, and "has it migrated yet" is
    unanswerable for those.
    """
    network = NETWORK_SLUGS.get(chain)
    out: List[Tuple[str, float]] = []
    seen = set()
    for page in range(1, pages + 1):
        url = f"{GT}/networks/{network}/dexes/{dex}/pools?page={page}"
        cache = os.path.join(cache_dir, f"{stamp}_lmd_{chain}_{dex}_{page}.json")
        data = _get(url, cache, pause=pause)
        for pool in (data.get("data") or []):
            rels = (pool or {}).get("relationships") or {}
            attrs = (pool or {}).get("attributes") or {}
            tid = ((rels.get("base_token") or {}).get("data") or {}).get("id") or ""
            ts = _ts(attrs.get("pool_created_at"))
            if "_" not in tid or ts is None:
                continue
            addr = tid.split("_", 1)[1]
            if addr in seen:
                continue
            seen.add(addr)
            out.append((addr, ts))
    return out


def stratify_by_age(pairs: Sequence[Tuple[str, float]], now: float,
                    per_bucket: int, seed: int = 11) -> List[str]:
    """Even sample across age buckets, so every bucket is represented.

    Without this, a listing dominated by recent pools would answer only the
    short-lag question and leave the long-horizon one unmeasured.
    """
    import random
    rng = random.Random(seed)
    buckets: Dict[str, List[str]] = {}
    for addr, ts in pairs:
        label = bucket_for((now - ts) / 60.0)
        if label:
            buckets.setdefault(label, []).append(addr)
    picked: List[str] = []
    for label, _, _ in AGE_BUCKETS:
        addrs = buckets.get(label, [])
        rng.shuffle(addrs)
        picked.extend(addrs[:per_bucket])
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chain", default="bsc")
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--limit", type=int, default=40, help="max tokens to price up")
    parser.add_argument("--pause", type=float, default=2.2)
    parser.add_argument("--cache-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".cache"))
    parser.add_argument("--only-dex", default=None,
                        help="restrict to tokens born on this DEX (regex), read from the "
                             "cached new_pools pages instead of the survivor feeds")
    parser.add_argument("--dex-pools", default=None,
                        help="discover tokens from /dexes/<this>/pools and stratify by age")
    parser.add_argument("--dex-pages", type=int, default=6)
    parser.add_argument("--per-bucket", type=int, default=6)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if args.dex_pools:
        now0 = time.time()
        pairs = discover_from_dex_pools(args.chain, args.dex_pools, args.dex_pages,
                                       args.cache_dir, stamp, pause=args.pause)
        tokens = stratify_by_age(pairs, now0, args.per_bucket)
        print(f"{args.chain}: {len(pairs)} pools listed on {args.dex_pools}; "
              f"sampling {len(tokens)} tokens evenly across age buckets")
        print("Age-stratified so the long-lag question is answerable, not just")
        print("'has a minutes-old token migrated yet'.\n")
    elif args.only_dex:
        tokens = tokens_from_newpools_cache(args.chain, args.cache_dir, args.only_dex)[:args.limit]
        print(f"{args.chain}: {len(tokens)} tokens BORN on /{args.only_dex}/ "
              f"(from cached new_pools pages)")
        print("This is outcome-blind at the birth end: the cohort is every birth on")
        print("that launchpad we sampled, not only the ones that later got traction.\n")
    else:
        tokens = discover_tokens(args.chain, args.pages, args.cache_dir, stamp)[:args.limit]
        print(f"{args.chain}: {len(tokens)} unique tokens from trending/top_volume")
        print("(survivor-selected: tokens that never got traction are absent, which")
        print(" biases the migration rate UP — the lag figures are the robust part)\n")

    now = time.time()
    records: List[dict] = []
    for i, token in enumerate(tokens, 1):
        pools = fetch_pools(args.chain, token, args.cache_dir, stamp, pause=args.pause)
        rec = birth_and_first_amm(pools)
        if rec:
            rec["token"] = token
            records.append(rec)
        if i % 10 == 0:
            print(f"  ... {i}/{len(tokens)}")

    if not records:
        print("no pool histories retrieved", file=sys.stderr)
        return 1

    launches = [r for r in records if r["birth_kind"] == "launchpad"]
    print(f"\n=== birth venue mix ({len(records)} tokens) ===")
    mix: Dict[str, int] = {}
    for r in records:
        mix[r["birth_dex"]] = mix.get(r["birth_dex"], 0) + 1
    for dex, n in sorted(mix.items(), key=lambda kv: -kv[1])[:8]:
        print(f"  {dex:28s} {n:4d}  {n / len(records):5.1%}   [{classify(dex)}]")

    if not launches:
        print("\nNo launchpad-born tokens in this sample — nothing to measure.")
        return 0

    print(f"\n=== launchpad-born tokens: {len(launches)} ===")
    summary = summarise(launches, now)
    for venue, entry in sorted(summary.items(), key=lambda kv: -kv[1]["n"]):
        if entry["n"] < 3:
            continue
        print(f"\n{venue}  n={entry['n']}")
        print(f"  migrated to an AMM: {entry['migrated']}/{entry['n']} = {entry['rate']:.0%}")
        if entry["lag_median"] is not None:
            print(f"  lag after birth pool: median {entry['lag_median']:.0f}m  "
                  f"(p25 {entry['lag_p25']:.0f}m, p75 {entry['lag_p75']:.0f}m, "
                  f"min {entry['lag_min']:.0f}m)")
        print("  by token age (censoring-aware — a 10m-old token cannot show a 6h lag):")
        for label, _, _ in AGE_BUCKETS:
            b = entry["buckets"].get(label)
            if not b:
                continue
            print(f"    {label:6s} n={b['n']:3d}  migrated {b['migrated']}/{b['n']}"
                  f" = {b['migrated'] / b['n']:.0%}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"chain": args.chain, "records": records,
                       "summary": summary}, fh, indent=1, default=str)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
