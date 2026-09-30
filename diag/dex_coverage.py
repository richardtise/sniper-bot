#!/usr/bin/env python3
"""Which DEX actually creates the new pools, and what would it take to trade them?

The question this answers is "is Uniswap enough for ETH/Base/Robinhood and
PancakeSwap enough for BSC?". It is answered from the birth feed
(``new_pools``), which is the population a latency-sensitive bot actually has to
choose from, and the answer is reported as a share per DEX with the sample size
attached — not as an impression from a handful of pools.

Two distinct things are measured, and conflating them is the usual mistake:

* **where pools are created** — the DEX id of each birth;
* **whether the bot can execute there** — a DEX is reachable only if the
  configured inventory holds *that DEX's* router. A router routes only pools from
  its own factory, so "Uniswap V3 router on Base" reaches Uniswap V3 Base pools
  and nothing else.

The inventory is expressed as regex patterns per chain so it can be changed
without editing code, and the tool always prints what is *uncovered*, because
that list is the actual to-do.

Usage
-----
    bot-env/bin/python diag/dex_coverage.py --pages 10
    bot-env/bin/python diag/dex_coverage.py --pages 10 --sources new_pools,trending
    bot-env/bin/python diag/dex_coverage.py --pages 10 --hypothetical v4-on-bsc
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Sequence, Tuple

GT = "https://api.geckoterminal.com/api/v2"
NETWORK_SLUGS = {"ethereum": "eth", "bsc": "bsc", "base": "base", "robinhood": "robinhood"}

# Router inventories, as patterns matched against the feed's dexId.
# "configured" is what bot.py can execute today; the hypothetical sets below are
# counterfactuals so the value of each addition is visible before building it.
# Note (r"uniswap") matches uniswap-v4 too — V4 is Uniswap — which is why the
# hypotheticals are kept deliberately distinct rather than layered carelessly.
INVENTORY_CONFIGURED = {
    "ethereum": (r"^uniswap$", r"uniswap[-_]?v?3", r"uniswap[-_]?v?2"),
    "base": (r"^uniswap$", r"uniswap[-_]?v?3", r"uniswap[-_]?v?2"),
    "robinhood": (r"^uniswap$", r"uniswap[-_]?v?3", r"uniswap[-_]?v?2"),
    "bsc": (r"^pancakeswap$", r"pancakeswap[-_]?v?3", r"pancakeswap[-_]?v?2"),
}

# B: configured PLUS Uniswap V4 on every chain (V4 is not in "configured", so
# this isolates exactly what implementing the Universal Router would buy).
INVENTORY_PLUS_V4 = {
    chain: tuple(pats) + (r"uniswap[-_]?v4",)
    for chain, pats in INVENTORY_CONFIGURED.items()
}

# C: the hypothesis under test — "Uniswap for ETH/Base/Robinhood, PancakeSwap
# for BSC", where "Uniswap" means the brand in any version (incl. V4).
INVENTORY_HYPOTHESIS = {
    "ethereum": (r"uniswap",),
    "base": (r"uniswap",),
    "robinhood": (r"uniswap",),
    "bsc": (r"pancakeswap",),
}

# D: the hypothesis, but BSC additionally gets Uniswap V4 (which is what the
# birth feed there actually shows).
INVENTORY_HYPOTHESIS_PLUS_BSC_V4 = {
    **INVENTORY_HYPOTHESIS,
    "bsc": (r"pancakeswap", r"uniswap[-_]?v4"),
}

HYPOTHETICALS = {
    "configured": INVENTORY_CONFIGURED,
    "plus-v4": INVENTORY_PLUS_V4,
    "uniswap-everywhere": INVENTORY_HYPOTHESIS,
    "hypothesis-plus-bsc-v4": INVENTORY_HYPOTHESIS_PLUS_BSC_V4,
}


def aggregate(pools: Sequence[dict], chain_of: Dict[str, str]) -> Dict[str, collections.Counter]:
    """dex counts per chain over the sampled pools."""
    per_chain: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for pool in pools:
        attrs = (pool or {}).get("attributes") or {}
        addr = attrs.get("address")
        rels = (pool or {}).get("relationships") or {}
        dex = ((rels.get("dex") or {}).get("data") or {}).get("id") or "unknown"
        per_chain[chain_of.get(addr, "?")][dex] += 1
    return per_chain


def is_covered(dex: str, patterns: Sequence[str]) -> bool:
    dex = str(dex or "").lower()
    return any(re.search(p, dex) for p in patterns)


def coverage_report(
    per_chain: Dict[str, collections.Counter], inventory: Dict[str, Sequence[str]]
) -> Dict[str, dict]:
    """Covered share per chain, plus the uncovered venues that make up the gap."""
    out: Dict[str, dict] = {}
    for chain, counts in per_chain.items():
        total = sum(counts.values())
        patterns = inventory.get(chain, ())
        covered = sum(n for dex, n in counts.items() if is_covered(dex, patterns))
        missing = [(dex, n) for dex, n in counts.most_common() if not is_covered(dex, patterns)]
        out[chain] = {
            "total": total,
            "covered": covered,
            "share": (covered / total) if total else 0.0,
            "missing": missing,
        }
    return out


def _get(url: str, cache_path: str, pause: float = 2.2) -> Tuple[dict, bool]:
    """Fetch one page. Returns ``(payload, ok)``.

    ``ok=False`` means the request failed (429/network) and nothing was cached,
    so a caller can tell "this page does not exist" (empty ``data`` list) from
    "this page was never retrieved". Conflating the two silently yields a sample
    with uneven depth per chain, which then looks like a real chain-level
    difference and is not one.
    """
    if os.path.exists(cache_path):
        with open(cache_path) as fh:
            return json.load(fh), True
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "sniper-bot-audit"}), timeout=30) as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - one dead page must not kill the run
        print(f"    ! {exc}", file=sys.stderr)
        return {}, False
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump(data, fh)
    time.sleep(pause)
    return data, True


def fetch(chains: Sequence[str], sources: Sequence[str], pages: int, cache_dir: str,
          stamp: str, pause: float = 2.2) -> Tuple[List[dict], Dict[str, str], Dict[str, dict]]:
    """Sample the feeds, tracking per-chain page depth so gaps stay visible."""
    endpoints = {
        "new_pools": "new_pools",
        "trending": "trending_pools",
        "top_volume": "pools?sort=h24_volume_usd_desc",
    }
    pools: List[dict] = []
    chain_of: Dict[str, str] = {}
    stats: Dict[str, dict] = {}
    for chain in chains:
        network = NETWORK_SLUGS.get(chain)
        if not network:
            continue
        st = stats.setdefault(chain, {"requested": 0, "ok": 0, "failed": 0,
                                      "empty": 0, "pools": 0})
        for source in sources:
            path = endpoints.get(source)
            if not path:
                continue
            for page in range(1, pages + 1):
                st["requested"] += 1
                sep = "&" if "?" in path else "?"
                url = f"{GT}/networks/{network}/{path}{sep}page={page}"
                cache = os.path.join(cache_dir, f"{stamp}_cov_{chain}_{source}_{page}.json")
                payload, ok = _get(url, cache, pause=pause)
                if not ok:
                    st["failed"] += 1
                    continue
                st["ok"] += 1
                batch = payload.get("data") or []
                if not batch:
                    st["empty"] += 1
                    continue
                for pool in batch:
                    addr = (pool.get("attributes") or {}).get("address")
                    if addr:
                        chain_of[addr] = chain
                pools.extend(batch)
                st["pools"] += len(batch)
    return pools, chain_of, stats


def print_table(title: str, per_chain: Dict[str, collections.Counter],
                inventory: Dict[str, Sequence[str]]) -> Dict[str, dict]:
    report = coverage_report(per_chain, inventory)
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    grand_total = sum(r["total"] for r in report.values())
    grand_covered = sum(r["covered"] for r in report.values())
    for chain in ("ethereum", "base", "bsc", "robinhood"):
        r = report.get(chain)
        if not r or not r["total"]:
            continue
        print(f"\n{chain}  n={r['total']}  covered {r['covered']}/{r['total']} "
              f"= {r['share']:.1%}")
        for dex, n in per_chain[chain].most_common(6):
            mark = "OK " if is_covered(dex, inventory.get(chain, ())) else "MISS"
            print(f"    [{mark}] {dex:30s} {n:5d}  {n / r['total']:6.1%}")
    if grand_total:
        print(f"\nPOOLED: covered {grand_covered}/{grand_total} = {grand_covered / grand_total:.1%}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chains", default="ethereum,base,bsc,robinhood")
    parser.add_argument("--sources", default="new_pools")
    parser.add_argument("--pages", type=int, default=10,
                        help="pages per chain per source (page 1 = newest births)")
    parser.add_argument("--cache-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".cache"))
    parser.add_argument("--pause", type=float, default=2.2,
                        help="seconds between calls against the ~30/min limiter")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    chains = [c.strip() for c in args.chains.split(",") if c.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    print(f"Sampling {','.join(chains)} sources={','.join(sources)} pages={args.pages}")
    pools, chain_of, stats = fetch(chains, sources, args.pages, args.cache_dir, stamp,
                                   pause=args.pause)
    per_chain = aggregate(pools, chain_of)
    total = sum(sum(c.values()) for c in per_chain.values())
    print(f"Sampled {total} pools total\n")
    print("Page depth per chain (a FAILED page means the chain is under-sampled —")
    print("its shares are NOT comparable to a chain with full depth):")
    incomplete = []
    for chain, st in stats.items():
        flag = ""
        if st["failed"]:
            flag = f"  <-- {st['failed']} FAILED, treat with suspicion"
            incomplete.append(chain)
        print(f"  {chain:10s} pages ok={st['ok']:2d} empty={st['empty']:2d} "
              f"failed={st['failed']:2d} pools={st['pools']:4d}{flag}")
    if incomplete:
        print(f"\n!! Under-sampled chains: {', '.join(incomplete)}. Re-run until failed=0")
        print("   before drawing any conclusion from their shares.")

    payload = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
               "pages": args.pages, "sources": sources, "total": total,
               "page_depth": stats,
               "per_chain_dex": {c: dict(v) for c, v in per_chain.items()},
               "reports": {}}

    payload["reports"]["configured"] = print_table(
        "A. What bot.py can execute TODAY (uniswap v2/v3 on eth/base/rh, "
        "pancakeswap v2/v3 on bsc)", per_chain, INVENTORY_CONFIGURED)

    payload["reports"]["plus-v4"] = print_table(
        "B. A + Uniswap V4 (Universal Router + Permit2) on every chain",
        per_chain, INVENTORY_PLUS_V4)

    payload["reports"]["uniswap-everywhere"] = print_table(
        "C. HYPOTHESIS: 'uniswap for eth/base/rh, pancakeswap for bsc'",
        per_chain, INVENTORY_HYPOTHESIS)

    payload["reports"]["hypothesis-plus-bsc-v4"] = print_table(
        "D. HYPOTHESIS + Uniswap V4 on BSC (what the BSC birth feed shows)",
        per_chain, INVENTORY_HYPOTHESIS_PLUS_BSC_V4)

    print("\n" + "=" * 78)
    print("Reading this: the MISS lines in section C are the venues a")
    print("'uniswap + pancakeswap' plan cannot reach. B isolates what")
    print("implementing V4 buys; D shows the hypothesis repaired.")
    print("=" * 78)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=1)
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
