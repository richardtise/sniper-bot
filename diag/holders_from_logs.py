"""Exact holder concentration for any Base token, computed from Transfer logs.

No provider, no API key: replay every ERC-20 ``Transfer`` event for the token
over Base JSON-RPC and aggregate balances. This is the only free way to get an
exact top-100 on Base, because:

* GeckoTerminal publishes bands only up to ``31_50`` (no 51-100);
* Moralis is 403 on the current key and its free tier is regularly suspended;
* Etherscan v2 answers "Free API access is not supported for this chain";
* Base Blockscout has a ``/holders`` endpoint but its index for V4-era tokens is
  incomplete (for boar it reports 51 holders and 8.6 % of supply, and omits the
  Uniswap V4 PoolManager entirely).

Self-validating: the top-10 and top-50 sums this prints should reproduce the
``top_10`` / ``11_30`` + ``31_50`` bands that GeckoTerminal publishes for the
same token. If they disagree, the replay is wrong (or its lookback is short) and
the number must not be trusted.

    bot-env/bin/python diag/holders_from_logs.py [token_address] [--rpc URL]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Dict, List, Optional

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEFAULT_TOKEN = "0x0cbf291Ba052174879d90bf781dF1A5F2BC5Bb07"
DEFAULT_RPC = "https://mainnet.base.org"


def rpc(url: str, method: str, params: list, timeout: int = 30):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params}).encode()
    request = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "holders-audit"})
    with urllib.request.urlopen(request, timeout=timeout) as fh:
        body = json.load(fh)
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def fetch_logs(rpc_url: str, token: str, start: int, end: int,
               topic: str = TRANSFER_TOPIC) -> List[dict]:
    """getLogs with chunk halving, so a provider range cap cannot fail the run."""
    out: List[dict] = []
    queue = [(start, end)]
    while queue:
        lo, hi = queue.pop()
        try:
            logs = rpc(rpc_url, "eth_getLogs", [{
                "fromBlock": hex(lo), "toBlock": hex(hi),
                "address": token, "topics": [topic],
            }])
            out.extend(logs)
        except Exception:
            if hi - lo <= 1:
                raise
            mid = (lo + hi) // 2
            queue.append((lo, mid))
            queue.append((mid + 1, hi))
    return out


def replay(token: str, rpc_url: str, chunk: int = 10_000,
           max_lookback: int = 400_000) -> Dict[str, float]:
    latest = int(rpc(rpc_url, "eth_blockNumber", []), 16)
    balances: Dict[str, int] = {}
    blocks_seen = 0
    earliest = latest

    end = latest
    while blocks_seen < max_lookback and end > 0:
        start = max(0, end - chunk + 1)
        logs = fetch_logs(rpc_url, token, start, end)
        blocks_seen += end - start + 1
        if logs:
            earliest = start
            for entry in logs:
                topics = entry.get("topics") or []
                if len(topics) < 3:
                    continue
                src = "0x" + topics[1][-40:]
                dst = "0x" + topics[2][-40:]
                value = int(entry.get("data") or "0x0", 16)
                if value == 0:
                    continue
                balances[src] = balances.get(src, 0) - value
                balances[dst] = balances.get(dst, 0) + value
            end = start - 1
            continue
        # Two consecutive empty chunks back means we are past deployment.
        start2 = max(0, start - chunk)
        if start2 == start or fetch_logs(rpc_url, token, start2, start - 1) == []:
            break
        end = start - 1

    balances = {a: b for a, b in balances.items()
                if b > 0 and a.lower() != "0x" + "0" * 40}
    return {"balances": balances, "latest_block": latest,
            "earliest_block": earliest, "blocks_scanned": blocks_seen}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("token", nargs="?", default=DEFAULT_TOKEN)
    parser.add_argument("--rpc", default=DEFAULT_RPC)
    parser.add_argument("--supply", type=float, default=100e9,
                        help="total supply in whole tokens (default 100e9 for boar)")
    parser.add_argument("--top", type=int, default=100)
    args = parser.parse_args()

    print(f"replaying Transfer logs for {args.token} via {args.rpc}")
    result = replay(args.token, args.rpc)
    balances = result["balances"]
    print(f"scanned {result['blocks_scanned']} blocks back to "
          f"{result['earliest_block']} (latest {result['latest_block']})")
    print(f"holders with positive balance: {len(balances)}")
    if not balances:
        print("no transfers found — lookback too short or wrong token", file=sys.stderr)
        return 2

    ordered = sorted(balances.items(), key=lambda kv: kv[1], reverse=True)
    supply_wei = args.supply * 10**18
    for n in (10, 50, 100):
        if len(ordered) >= n:
            pct = sum(b for _, b in ordered[:n]) / supply_wei * 100
            print(f"  top{n:<4d} = {pct:6.2f}% of supply")

    print(f"\n{'#':>3} {'address':44s} {'tokens':>16s} {'%supply':>8s}")
    for i, (addr, bal) in enumerate(ordered[:args.top], 1):
        print(f"{i:>3} {addr:44s} {bal / 1e18:>16,.0f} {bal / supply_wei * 100:>8.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
