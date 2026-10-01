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


class TestPaginationAndSplitTTL(unittest.TestCase):
    """2026-09-29 recall fix: page-1-only discovery never sees a runner on
    page 2, and a blanket 90s TTL skips whole new_pools birth cohorts."""

    def test_candidates_walk_multiple_pages(self):
        fetch = FakeFetch({
            "new_pools?page=1": payload([gt_pool(address="0xp1", token="0xt1")]),
            "new_pools?page=2": payload([gt_pool(address="0xp2", token="0xt2",
                                                 symbol="MOON")]),
            "new_pools?page=3": {"data": [], "included": []},
        })
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=60,
                           max_pages=3, page_size=1)

        async def run():
            return await gt.candidates("bsc", kinds=("new_pools",))

        cands = asyncio.run(run())
        symbols = {p["baseToken"]["symbol"] for p in cands}
        self.assertEqual(symbols, {"RUN", "MOON"})

    def test_short_last_page_stops_early(self):
        """A *short* (non-empty) page ends the walk, not an empty one.

        The empty-batch stop breaks the loop just as well, so a fixture where
        page 2 404s would pass without proving anything: page 2 is answerable
        here, and the walk still has to stop on the length of page 1.
        """
        fetch = FakeFetch({
            "new_pools?page=1": payload([gt_pool(address="0xp1", token="0xt1")]),
            "new_pools?page=2": payload([gt_pool(address="0xp2", token="0xt2",
                                                 symbol="MOON")]),
        })
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=60,
                           max_pages=5, page_size=20)

        async def run():
            return await gt.candidates("bsc", kinds=("new_pools",))

        cands = asyncio.run(run())
        symbols = {p["baseToken"]["symbol"] for p in cands}
        self.assertEqual(symbols, {"RUN"},
                         "page 1 returned 1 of 20 slots: short, so the walk stops")
        # Exactly one call, and page 2 — which the fake would have answered —
        # was never requested. That is the short-page rule, not the empty one.
        self.assertEqual(len(fetch.calls), 1)
        self.assertFalse(any("page=2" in url for url in fetch.calls))

    def test_new_pools_ttl_shorter_than_top_volume(self):
        """The blanket 90s TTL must not survive for new_pools.

        ``new_pools`` churns a whole birth cohort every ~2-4 minutes, so its
        cache entry has to expire before ``top_volume``'s. A client that fell
        back to one TTL for both sources would still cache both for the
        immediate re-reads below, so the per-source resolutions themselves are
        asserted: with and without an explicit override they must differ, with
        new_pools the shorter of the two.
        """
        fetch = FakeFetch({
            "new_pools": payload([gt_pool()]),
            "sort=h24_volume_usd_desc": payload([gt_pool()]),
        })
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=90,
                           list_ttls={"new_pools": 30.0, "top_volume": 180.0})

        # Per-source override is resolved per kind, not flattened to cache_ttl_s.
        self.assertEqual(gt._ttl_for("new_pools"), 30.0)
        self.assertEqual(gt._ttl_for("top_volume"), 180.0)
        self.assertLess(gt._ttl_for("new_pools"), gt._ttl_for("top_volume"))
        # The shipped defaults carry the same invariant without an override.
        default_gt = GeckoTerminal(FakeFetch({}), min_interval_s=0.0, cache_ttl_s=90)
        self.assertLess(default_gt._ttl_for("new_pools"),
                        default_gt._ttl_for("top_volume"),
                        "DEFAULT_LIST_TTLS must keep new_pools the freshest list")
        # A source with no per-kind entry (token_info is not a pool list) still
        # falls back to the blanket TTL.
        self.assertEqual(gt._ttl_for("token_info"), 90.0)
        self.assertEqual(gt._ttl_for(None), 90.0)

        async def run():
            await gt.list_pools("bsc", kind="new_pools")
            await gt.list_pools("bsc", kind="top_volume")
            # Immediate re-fetch: both served from cache.
            await gt.list_pools("bsc", kind="new_pools")
            await gt.list_pools("bsc", kind="top_volume")

        asyncio.run(run())
        self.assertEqual(len(fetch.calls), 2)

    def test_list_pools_honours_the_per_kind_ttl(self):
        """`kind` must actually reach the cache.

        Pins a real regression: ``list_pools`` called ``_get(path)`` **without**
        ``kind``, so ``_ttl_for`` saw ``None``, returned the blanket
        ``cache_ttl_s``, and every ``GT_LIST_TTL_*`` knob — plus this module's
        own ``DEFAULT_LIST_TTLS`` — was inert for pool lists. The resolution is
        asserted from both directions: a zero per-kind TTL must force a refetch
        (the blanket 90s would serve it from cache), and a non-zero per-kind TTL
        must be honoured even when the blanket TTL is 0 (the blanket would
        refetch).
        """
        # Under-wiring: kind ignored -> blanket 90s caches a ttl=0 entry.
        fetch = FakeFetch({"new_pools": payload([gt_pool()])})
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=90,
                           list_ttls={"new_pools": 0.0})

        async def run_zero():
            await gt.list_pools("bsc", kind="new_pools")
            await gt.list_pools("bsc", kind="new_pools")

        asyncio.run(run_zero())
        self.assertEqual(len(fetch.calls), 2,
                         "ttl=0 for new_pools must not be served from cache")

        # Over-wiring: blanket 0 must not override the per-kind 60.
        fetch2 = FakeFetch({"new_pools": payload([gt_pool()])})
        gt2 = GeckoTerminal(fetch2, min_interval_s=0.0, cache_ttl_s=0.0,
                            list_ttls={"new_pools": 60.0})

        async def run_sixty():
            await gt2.list_pools("bsc", kind="new_pools")
            await gt2.list_pools("bsc", kind="new_pools")

        asyncio.run(run_sixty())
        self.assertEqual(len(fetch2.calls), 1,
                         "the per-kind TTL must win over the blanket one")

    def test_explicit_pages_kwarg_overrides_client_default(self):
        fetch = FakeFetch({
            "new_pools?page=1": payload([gt_pool(address="0xp1", token="0xt1")]),
            "new_pools?page=2": payload([gt_pool(address="0xp2", token="0xt2",
                                                 symbol="MOON")]),
        })
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=0,
                           max_pages=5)

        async def run():
            return await gt.candidates("bsc", kinds=("new_pools",), pages=1)

        cands = asyncio.run(run())
        self.assertEqual(len(cands), 1)


class TestPerSourcePages(unittest.TestCase):
    """Freshness is per source: new_pools page 1 is the newest cohort, while
    trending rewards depth. One page count for everything wasted calls on the
    feed where depth buys least."""

    def test_source_pages_override_max_pages_per_kind(self):
        fetch = FakeFetch({
            "new_pools?page=1": payload([gt_pool(address="0xn1", token="0xn1t")]),
            "new_pools?page=2": payload([gt_pool(address="0xn2", token="0xn2t",
                                                 symbol="DEEP")]),
            "trending_pools?page=1": payload([gt_pool(address="0xt1", token="0xt1t",
                                                      symbol="TREND1")]),
            "trending_pools?page=2": payload([gt_pool(address="0xt2", token="0xt2t",
                                                      symbol="TREND2")]),
        })
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=0,
                           page_size=1,
                           source_pages={"new_pools": 1, "trending": 2})

        async def run():
            return await gt.candidates("bsc", kinds=("new_pools", "trending"))

        cands = asyncio.run(run())
        symbols = {p["baseToken"]["symbol"] for p in cands}
        # new_pools stopped at page 1 (its override), trending walked to page 2.
        self.assertEqual(symbols, {"RUN", "TREND1", "TREND2"})
        self.assertFalse(any("new_pools?page=2" in u for u in fetch.calls))
        self.assertTrue(any("trending_pools?page=2" in u for u in fetch.calls))

    def test_pages_kwarg_still_overrides_every_source(self):
        fetch = FakeFetch({
            "new_pools?page=1": payload([gt_pool(address="0xn1", token="0xn1t")]),
            "new_pools?page=2": payload([gt_pool(address="0xn2", token="0xn2t",
                                                 symbol="DEEP")]),
            "trending_pools?page=1": payload([gt_pool(address="0xt1", token="0xt1t",
                                                      symbol="TREND1")]),
        })
        gt = GeckoTerminal(fetch, min_interval_s=0.0, cache_ttl_s=0, page_size=1,
                           source_pages={"new_pools": 1, "trending": 2})

        async def run():
            return await gt.candidates("bsc", kinds=("new_pools", "trending"), pages=1)

        cands = asyncio.run(run())
        self.assertEqual({p["baseToken"]["symbol"] for p in cands}, {"RUN", "TREND1"})
        self.assertFalse(any("page=2" in u for u in fetch.calls))


if __name__ == "__main__":
    unittest.main()
