"""Unit tests for discovery.py — run with: python -m unittest -v test_discovery"""

import asyncio
import unittest

from discovery import (
    GeckoTerminal,
    dedupe_best_pool,
    dexscreener_search,
    pool_to_pair,
)


def gt_pool(address="0xpool1", token="0xbase1", symbol="RUN", liq="50000",
            created="2026-09-17T20:39:06Z", source_kind="new_pools"):
    return {
        "id": f"bsc_{address}",
        "type": "pool",
        "attributes": {
            "address": address,
            "name": f"{symbol} / WBNB",
            "base_token_price_usd": "0.001",
            "base_token_price_native_currency": "0.000001",
            "quote_token_price_usd": "600",
            "reserve_in_usd": liq,
            "fdv_usd": "800000",
            "market_cap_usd": "700000",
            "pool_created_at": created,
            "volume_usd": {"m5": "1000", "m15": "3000", "h1": "9000", "h6": "30000", "h24": "90000"},
            "price_change_percentage": {"m5": "5", "m15": "8", "h1": "20", "h6": "50", "h24": "80"},
            "transactions": {
                "m5": {"buys": 10, "sells": 5, "buyers": 8, "sellers": 4},
                "h1": {"buys": 100, "sells": 60, "buyers": 80, "sellers": 50},
            },
        },
        "relationships": {
            "base_token": {"data": {"id": f"bsc_{token}", "type": "token"}},
            "quote_token": {"data": {"id": "bsc_0xwbnb", "type": "token"}},
            "dex": {"data": {"id": "pancakeswap_v3", "type": "dex"}},
        },
    }


def payload(pools, kind="new_pools"):
    """Build a GT payload whose `included` tokens match the pools given."""
    included = {}
    for pool in pools:
        base_sym, _, quote_sym = pool["attributes"]["name"].partition(" / ")
        for rel, sym in (("base_token", base_sym), ("quote_token", quote_sym or "WBNB")):
            data = pool["relationships"][rel]["data"]
            tid = data["id"]
            addr = tid.split("_", 1)[1] if "_" in tid else tid
            included[tid] = {
                "id": tid, "type": "token",
                "attributes": {"address": addr, "symbol": sym, "name": sym},
            }
    return {"data": pools, "included": list(included.values())}


class TestPoolMapping(unittest.TestCase):
    def test_pool_to_pair_shape(self):
        included = {"bsc_0xbase1": {"address": "0xbase1", "symbol": "RUN", "name": "Runner"},
                    "bsc_0xwbnb": {"address": "0xwbnb", "symbol": "WBNB", "name": "WBNB"}}
        pair = pool_to_pair(gt_pool(), included, "bsc", "geckoterminal:new_pools")
        self.assertEqual(pair["chainId"], "bsc")
        self.assertEqual(pair["baseToken"]["address"], "0xbase1")
        self.assertEqual(pair["quoteToken"]["symbol"], "WBNB")
        self.assertEqual(pair["dexId"], "pancakeswap_v3")
        self.assertEqual(pair["liquidity"]["usd"], 50000.0)
        self.assertEqual(pair["marketCap"], 700000.0)
        self.assertEqual(pair["volume"]["h1"], 9000.0)
        self.assertEqual(pair["txns"]["m5"]["buyers"], 8)
        self.assertEqual(pair["priceChange"]["m5"], 5.0)
        self.assertIsNotNone(pair["pairCreatedAt"])
        self.assertEqual(pair["source"], "geckoterminal:new_pools")

    def test_pool_to_pair_without_included(self):
        pair = pool_to_pair(gt_pool(), {}, "bsc", "src")
        self.assertEqual(pair["baseToken"]["address"], "0xbase1")  # stripped from id
        self.assertEqual(pair["quoteToken"]["address"], "0xwbnb")

    def test_missing_address_returns_none(self):
        self.assertIsNone(pool_to_pair({"attributes": {}}, {}, "bsc", "src"))


class TestDedupe(unittest.TestCase):
    def test_keeps_deepest_pool_per_token(self):
        shallow = {"chainId": "bsc", "baseToken": {"address": "0xtok"}, "liquidity": {"usd": 1000}}
        deep = {"chainId": "bsc", "baseToken": {"address": "0xtok"}, "liquidity": {"usd": 9000}}
        other = {"chainId": "bsc", "baseToken": {"address": "0xother"}, "liquidity": {"usd": 50}}
        out = dedupe_best_pool([shallow, deep, other])
        self.assertEqual(len(out), 2)
        by_token = {p["baseToken"]["address"]: p for p in out}
        self.assertEqual(by_token["0xtok"]["liquidity"]["usd"], 9000)

    def test_case_insensitive_token_key(self):
        a = {"chainId": "bsc", "baseToken": {"address": "0xABC"}, "liquidity": {"usd": 1}}
        b = {"chainId": "bsc", "baseToken": {"address": "0xabc"}, "liquidity": {"usd": 2}}
        self.assertEqual(len(dedupe_best_pool([a, b])), 1)


class FakeFetch:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        for needle, response in self.responses.items():
            if needle in url:
                return response
        return None


class TestGeckoTerminalClient(unittest.TestCase):
    def test_list_pools_and_candidates(self):
        fetch = FakeFetch({
            "new_pools": payload([gt_pool(address="0xp1", token="0xt1")], "new_pools"),
            "trending_pools": payload([gt_pool(address="0xp2", token="0xt2", symbol="MOON")],
                                      "trending_pools"),
        })
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=60)

        async def run():
            pools = await gt.list_pools("bsc", kind="new_pools")
            self.assertEqual(len(pools), 1)
            self.assertEqual(pools[0]["baseToken"]["symbol"], "RUN")
            cands = await gt.candidates("bsc")
            return cands

        cands = asyncio.run(run())
        self.assertEqual(len(cands), 2)
        self.assertTrue(all(p["chainId"] == "bsc" for p in cands))

    def test_cache_avoids_second_fetch(self):
        fetch = FakeFetch({"new_pools": payload([gt_pool()])})
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=60)

        async def run():
            await gt.list_pools("bsc")
            await gt.list_pools("bsc")

        asyncio.run(run())
        self.assertEqual(len(fetch.calls), 1)

    def test_unsupported_chain_returns_empty(self):
        fetch = FakeFetch({})
        gt = GeckoTerminal(fetch, min_interval_s=0.0)

        async def run():
            return await gt.list_pools("dogechain")

        self.assertEqual(asyncio.run(run()), [])

    def test_token_info(self):
        fetch = FakeFetch({"tokens/0xt1/info": {"data": {"attributes": {"gt_score": 77}}}})
        gt = GeckoTerminal(fetch, min_interval_s=0.0)

        async def run():
            return await gt.token_info("bsc", "0xt1")

        self.assertEqual(asyncio.run(run())["gt_score"], 77)


class TestDexScreenerSearch(unittest.TestCase):
    def test_filters_by_chain(self):
        fetch = FakeFetch({
            "dex/search": {"pairs": [
                {"chainId": "bsc", "baseToken": {"address": "0xa"}, "liquidity": {"usd": 1}},
                {"chainId": "base", "baseToken": {"address": "0xb"}, "liquidity": {"usd": 2}},
            ]},
        })

        async def run():
            return await dexscreener_search(fetch, "WBNB", "bsc")

        out = asyncio.run(run())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["baseToken"]["address"], "0xa")
        self.assertEqual(out[0]["source"], "dexscreener_search")


if __name__ == "__main__":
    unittest.main()
