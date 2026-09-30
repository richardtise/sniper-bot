#!/usr/bin/env python3
"""Of the tokens the bot would ALERT on, how many can actually be bought?

This is not the same question as "what share of births are routable". Alerts are
a filtered population: the mcap ceiling, the liquidity/volume floors and the
early-lane AND-gates all remove candidates before anything is sent, and they do
not remove venues uniformly. So the birth-share table in the README can overstate
or understate what lands in Telegram.

This tool closes that gap by replaying the bot's **real** gate code over a sampled
birth cohort:

* ``bot.dex_is_supported`` for venue tradeability (the same call the scan uses);
* ``signals.early_runner_reasons`` with the bot's actual per-chain Filters for the
  fast lane, so the AND-gates are the shipped ones rather than a reimplementation;
* the same per-chain floors the scanner applies.

It reports, among tokens that would pass, how many are tradeable and what the
untradeable remainder needs. Run it after changing floors or venue config.

Limitations, stated because they bound what the number means:

* It simulates the **early lane** on ``new_pools`` candidates. The momentum lane
  needs holder/CEX/security lookups and a live score, which cannot be reproduced
  offline, so it is not simulated. For young pools the early lane is the path that
  matters, which is also why the cohort is the birth feed.
* Pool metrics are a snapshot taken when the feed was sampled, not the exact
  values at the moment the bot would have alerted.
* Nothing here places a trade; it only answers "would the buy button have worked".

Usage
-----
    bot-env/bin/python diag/alert_tradeability.py
    bot-env/bin/python diag/alert_tradeability.py --chains robinhood,base --pages 6
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Dict, List, Tuple

os.environ.setdefault("TELEGRAM_TOKEN", "1:TEST")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot  # noqa: E402
import discovery  # noqa: E402
import signals  # noqa: E402


def benign_security() -> dict:
    """A security record with no flags set.

    The venue question is orthogonal to the security question, and security
    lookups need live APIs. On Robinhood the real record is Etherscan
    verification only, so this stands in for "nothing disqualifying was found".
    """
    sec = bot._security_placeholder("diag")
    sec.update({"is_open_source": True, "lp_locked": True, "verification_known": True,
                "is_verified": True})
    return sec


def load_pools(chain: str, cache_dir: str, pages: int) -> List[dict]:
    """Raw GeckoTerminal pools for one chain from the cached feed pages."""
    out: List[dict] = []
    pattern = os.path.join(cache_dir, f"*cov_{chain}_new_pools_*.json")
    for path in sorted(glob.glob(pattern))[:pages]:
        with open(path) as fh:
            out.extend((json.load(fh).get("data") or []))
    return out


def passes_floors(pair: dict, chain: str) -> bool:
    """The same per-chain floors ``evaluate_token`` applies, in the same order."""
    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
    price = float(pair.get("priceUsd") or 0)
    market_cap = float(pair.get("marketCap") or 0)
    vol_5m = float((pair.get("volume") or {}).get("m5") or 0)

    default_liq = bot.ROBINHOOD_MIN_LIQUIDITY_USD if chain == "robinhood" else bot.MIN_LIQUIDITY_USD
    min_liq = bot._chain_floor(chain, "MIN_LIQUIDITY_USD", default_liq)
    min_vol = bot._chain_floor(chain, "MIN_VOL_5M_USD", bot.MIN_VOL_5M_USD)
    min_mcap = bot._chain_floor(chain, "MIN_MARKET_CAP_USD", bot.MIN_MARKET_CAP_USD)
    max_mcap = bot._chain_floor(chain, "MAX_MARKET_CAP_USD",
                                float(os.getenv("MAX_MARKET_CAP_USD", "0") or 0))
    if liquidity < min_liq:
        return False
    if price < bot.MIN_PRICE:
        return False
    if market_cap < min_mcap:
        return False
    if max_mcap and market_cap > max_mcap:
        return False
    if vol_5m < min_vol:
        return False
    return True


def simulate(chain: str, pools: List[dict]) -> Dict[str, object]:
    """Replay floors + early lane, then split survivors by tradeability."""
    security = benign_security()
    filters = bot._filters_for_chain(chain)
    stats = {"cohort": 0, "age_eligible": 0, "floor_pass": 0, "early_lane": 0,
             "tradeable": 0, "untradeable": 0, "venues": {}, "needs": {}}
    age_cap = filters.early_max_age_minutes if filters else 30.0
    for raw in pools:
        pair = discovery.pool_to_pair(raw, {}, chain, "geckoterminal:new_pools")
        if not pair:
            continue
        stats["cohort"] += 1
        # The early lane is age-gated, and new_pools pagination reaches back well
        # past that window: sampled 6 pages deep, every floor-passing token was
        # 59-64 minutes old and died on `not_early`. Simulating the lane over
        # tokens it can never consider says nothing about its recall.
        created = pair.get("pairCreatedAt")
        if not created:
            continue
        age_minutes = (time.time() - created / 1000.0) / 60.0
        if age_minutes > age_cap:
            continue
        stats["age_eligible"] += 1
        if not passes_floors(pair, chain):
            continue
        stats["floor_pass"] += 1
        if signals.early_runner_reasons(pair, security=security, filters=filters):
            continue
        stats["early_lane"] += 1

        dex = str(pair.get("dexId") or "").strip().lower()
        tradeable = bot.dex_is_supported(chain, dex)
        key = dex or "(no dexId)"
        stats["venues"][key] = stats["venues"].get(key, 0) + 1
        if tradeable:
            stats["tradeable"] += 1
        else:
            stats["untradeable"] += 1
            need = bot.venue_requirement(chain, dex)
            stats["needs"][need] = stats["needs"].get(need, 0) + 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chains", default="ethereum,base,bsc,robinhood")
    parser.add_argument("--pages", type=int, default=6)
    parser.add_argument("--cache-dir", default=os.path.join(ROOT, "diag", ".cache"))
    args = parser.parse_args()

    chains = [c.strip() for c in args.chains.split(",") if c.strip()]
    print("Replaying the shipped floors + early-lane gates over cached births.")
    print("Simulates the EARLY LANE only (the momentum lane needs live lookups).")
    print("Tokens older than the lane's age cap are excluded and counted:")
    print("deeper new_pools pages are already past it, so they are not evidence")
    print("about the lane either way.\n")

    grand = {"early_lane": 0, "tradeable": 0, "untradeable": 0}
    needs_all: Dict[str, int] = {}
    for chain in chains:
        pools = load_pools(chain, args.cache_dir, args.pages)
        if not pools:
            print(f"{chain}: no cached pools (run diag/dex_coverage.py first)")
            continue
        st = simulate(chain, pools)
        print(f"=== {chain} ===")
        print(f"  cohort {st['cohort']}  ->  young enough for the lane "
              f"{st['age_eligible']}  ->  floors {st['floor_pass']}  ->  "
              f"early lane {st['early_lane']}")
        if st["early_lane"]:
            print(f"  of the early-lane alerts: tradeable {st['tradeable']}"
                  f" / {st['early_lane']} = {st['tradeable'] / st['early_lane']:.0%}")
            print("  venues that would alert:")
            for dex, n in sorted(st["venues"].items(), key=lambda kv: -kv[1]):
                mark = "OK  " if bot.dex_is_supported(chain, dex) else "NORA"
                print(f"    [{mark}] {dex:28s} {n:4d}")
            if st["needs"]:
                print("  missing integrations behind the no-route alerts:")
                for need, n in sorted(st["needs"].items(), key=lambda kv: -kv[1]):
                    print(f"          {need:44s} {n:4d}")
                for need, n in st["needs"].items():
                    needs_all[need] = needs_all.get(need, 0) + n
        grand["early_lane"] += st["early_lane"]
        grand["tradeable"] += st["tradeable"]
        grand["untradeable"] += st["untradeable"]
        print()

    if grand["early_lane"]:
        print("=" * 74)
        print(f"POOLED: {grand['tradeable']} of {grand['early_lane']} simulated early-lane "
              f"alerts are tradeable = {grand['tradeable'] / grand['early_lane']:.0%}")
        print(f"        {grand['untradeable']} would alert with NO ROUTE "
              f"(buttons withheld)")
        print("=" * 74)
    else:
        print("No token passed the early lane in this cohort — nothing to report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
