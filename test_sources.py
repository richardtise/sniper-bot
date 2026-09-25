"""Tests for the holder/security data sources and the chain-aware alert gate.

Covers the fixes for the "no alerts at all" state:

* holder concentration now comes from GeckoTerminal (Blockscout is Cloudflare
  blocked, Moralis's free tier is suspended);
* contract verification on Robinhood comes from Etherscan, and a provider
  outage no longer looks like an unverified token;
* the alert gate scales to the points actually reachable on a chain/age, so a
  young Robinhood runner is not required to score 65 out of a reachable ~43;
* SIG_REQUIRE_OPEN_SOURCE is configurable (and off by default).

Run with: python -m unittest -v test_sources
"""

import os
import time
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "1:TEST")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")

import bot  # noqa: E402  (import after env stubs)
import signals  # noqa: E402


# A trimmed but structurally faithful GeckoTerminal /tokens/{addr}/info payload.
GT_INFO = {
    "data": {
        "id": "robinhood_0x3c934eee3fd33be89d0c0a5d073dcf2ea3b3dc26",
        "type": "token",
        "attributes": {
            "address": "0x3c934eee3fd33be89d0c0a5d073dcf2ea3b3dc26",
            "symbol": "SCHIFFY",
            "holders": {
                "count": 5226,
                "distribution_percentage": {
                    "top_10": "4.5944",
                    "11_30": "6.6805",
                    "31_50": "5.3374",
                    "rest": "83.3877",
                },
            },
            "gt_score": 74.92,
        },
    }
}


class TestGeckoTerminalHolders(unittest.TestCase):
    def test_parses_bands_into_top10_and_top50(self):
        data = bot._gt_holder_data(GT_INFO)
        self.assertTrue(data.measured)
        self.assertAlmostEqual(data.top10, 4.5944, places=4)
        # 11_30 and 31_50 are cumulative bands, so top50 is their sum.
        self.assertAlmostEqual(data.top50, 4.5944 + 6.6805 + 5.3374, places=4)
        self.assertEqual(data.holder_count, 5226)
        self.assertEqual(data.source, "geckoterminal")

    def test_top100_is_unmeasured_not_zero(self):
        """GT publishes no 51-100 band, so top100 must be None, never 0.0.

        Scoring it as 0.0 would silently punish a token; scoring it as a guess
        would silently reward one. None lets the gate scale down instead.
        """
        self.assertIsNone(bot._gt_holder_data(GT_INFO).top100)

    def test_missing_distribution_is_unmeasured(self):
        for payload in ({}, {"data": {}}, {"data": {"attributes": {}}},
                        {"data": {"attributes": {"holders": {}}}},
                        {"data": {"attributes": {"holders": {"distribution_percentage": {}}}}}):
            data = bot._gt_holder_data(payload)
            self.assertFalse(data.measured, msg=repr(payload))
            self.assertEqual(data.source, "")

    def test_non_numeric_bands_are_unmeasured(self):
        bad = {"data": {"attributes": {"holders": {"distribution_percentage": {"top_10": "n/a"}}}}}
        self.assertFalse(bot._gt_holder_data(bad).measured)

    def test_top50_is_clamped_to_100(self):
        over = {"data": {"attributes": {"holders": {"distribution_percentage": {
            "top_10": "80", "11_30": "25", "31_50": "10"}}}}}
        self.assertEqual(bot._gt_holder_data(over).top50, 100.0)


class TestScoreHolder(unittest.TestCase):
    def test_none_top100_skips_only_that_block(self):
        """A None top100 must cost exactly the HOLDER_TOP100_PTS block."""
        with_t100 = bot.score_holder(80.0, 90.0, 95.0, 600)
        without_t100 = bot.score_holder(80.0, 90.0, None, 600)
        self.assertEqual(with_t100 - without_t100, bot.HOLDER_TOP100_PTS)

    def test_top100_zero_still_scores_zero_for_that_block(self):
        self.assertEqual(bot.score_holder(80.0, 90.0, 0.0, 600),
                         bot.score_holder(80.0, 90.0, None, 600))

    def test_schiffy_profile_earns_nothing(self):
        """SCHIFFY is genuinely well distributed (top10 ~4.6%), so no points.

        Holder scoring *rewards* concentration, so a healthy float scores zero.
        This is why fixing the provider alone does not make SCHIFFY alert.
        """
        self.assertEqual(bot.score_holder(4.59, 16.61, None, 450), 0)


class TestMaxPossibleScore(unittest.TestCase):
    def test_older_robinhood_token_loses_only_the_cex_points(self):
        # Robinhood cannot score CEX (no CoinGecko listing) but can score holders.
        without_cex = bot.max_possible_score("robinhood", 450, has_top100=True, has_cex=False)
        full = bot.max_possible_score("bsc", 450, has_top100=True, has_cex=True)
        self.assertAlmostEqual(full - without_cex, bot.CEX_LISTING_PTS + bot.CEX_PERPS_PTS)

    def test_missing_top100_lowers_the_ceiling(self):
        with_t100 = bot.max_possible_score("robinhood", 450, has_top100=True, has_cex=False)
        without_t100 = bot.max_possible_score("robinhood", 450, has_top100=False, has_cex=False)
        self.assertAlmostEqual(with_t100 - without_t100, bot.HOLDER_TOP100_PTS)

    def test_age_gates_lower_the_ceiling_for_young_pools(self):
        """A 1h-old pool cannot score the 6h/24h tier at all; a 10m one loses
        both the 1h/6h and 6h/24h tiers plus the 1h buy-pressure block."""
        old = bot.max_possible_score("robinhood", 450, has_top100=True, has_cex=False)
        one_hour = bot.max_possible_score("robinhood", 60, has_top100=True, has_cex=False)
        ten_min = bot.max_possible_score("robinhood", 10, has_top100=True, has_cex=False)
        self.assertLess(one_hour, old)
        self.assertLess(ten_min, one_hour)

    def test_ceiling_is_never_above_100(self):
        for chain in ("bsc", "ethereum", "base", "robinhood"):
            for age in (0.5, 10, 60, 61, 360, 361, 5000):
                ceiling = bot.max_possible_score(chain, age, has_top100=True, has_cex=True)
                self.assertLessEqual(ceiling, 100.0 + 1e-9, msg=f"{chain}@{age}")

    def test_ceiling_matches_a_perfect_token(self):
        """The ceiling must be achievable: driving every scorer to its top tier
        must reproduce it. Guards against the ceiling drifting from the scorer."""
        age = 450
        recomputed = (
            bot.score_volume_liquidity(1000.0, 1000.0)
            + bot.score_5m_1h(1000.0, 1000.0, age)
            + bot.score_1h_6h(1000.0, 1000.0, age)
            + bot.score_6h_24h(1000.0, 1000.0, age)
            + bot.BUY_PRESSURE_5M_PTS + bot.BUY_PRESSURE_1H_PTS
            + bot.score_price(1000.0, 1000.0, 1000.0, age)
            + bot.SECURITY_PTS
            + bot.score_holder(100.0, 100.0, 100.0, age)
            + bot.score_cex(bot.CEX_LISTING_PTS, True, 1)
        )
        self.assertAlmostEqual(
            recomputed, bot.max_possible_score("bsc", age, has_top100=True, has_cex=True)
        )


class TestEffectiveThreshold(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("SCORE_NORMALIZE", "MIN_EFFECTIVE_SCORE", "MIN_SCORE",
                        "ROBINHOOD_MIN_SCORE", "BASE_MIN_SCORE")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_scaling_is_off_when_disabled(self):
        os.environ["SCORE_NORMALIZE"] = "false"
        self.assertEqual(bot.effective_threshold("robinhood", 450), float(bot.ALERT_THRESHOLD))
        self.assertEqual(bot.effective_threshold("bsc", 450), float(bot.ALERT_THRESHOLD))

    def test_robinhood_threshold_is_below_the_raw_threshold(self):
        """The whole point: 5 unreachable CEX points + 4 unmeasurable holder
        points must not be silently demanded."""
        os.environ["SCORE_NORMALIZE"] = "true"
        os.environ["MIN_EFFECTIVE_SCORE"] = "0"
        t = bot.effective_threshold("robinhood", 450, has_top100=False, has_cex=False)
        self.assertLess(t, float(bot.ALERT_THRESHOLD))
        self.assertGreater(t, 0.0)

    def test_young_pool_threshold_is_lower_still(self):
        os.environ["SCORE_NORMALIZE"] = "true"
        os.environ["MIN_EFFECTIVE_SCORE"] = "0"
        old = bot.effective_threshold("robinhood", 450, has_top100=False, has_cex=False)
        young = bot.effective_threshold("robinhood", 10, has_top100=False, has_cex=False)
        self.assertLess(young, old)

    def test_floor_is_respected(self):
        os.environ["SCORE_NORMALIZE"] = "true"
        os.environ["MIN_EFFECTIVE_SCORE"] = "35"
        t = bot.effective_threshold("robinhood", 0.5, has_top100=False, has_cex=False)
        self.assertGreaterEqual(t, 35.0)

    def test_chain_override_wins(self):
        os.environ["SCORE_NORMALIZE"] = "false"
        os.environ["ROBINHOOD_MIN_SCORE"] = "48"
        self.assertEqual(bot.effective_threshold("robinhood", 450), 48.0)
        # and does not leak into other chains
        self.assertEqual(bot.effective_threshold("bsc", 450), float(bot.ALERT_THRESHOLD))

    def test_threshold_never_exceeds_the_configured_bar(self):
        os.environ["SCORE_NORMALIZE"] = "true"
        os.environ["MIN_EFFECTIVE_SCORE"] = "0"
        for chain in ("bsc", "ethereum", "base", "robinhood"):
            for age in (1, 30, 120, 500):
                self.assertLessEqual(bot.effective_threshold(chain, age), float(bot.ALERT_THRESHOLD))

    def test_cex_data_availability(self):
        self.assertFalse(bot.has_cex_data("robinhood"))
        self.assertTrue(bot.has_cex_data("bsc"))


class TestEtherscanChainMap(unittest.TestCase):
    def test_robinhood_chain_id_is_present(self):
        """Etherscan EAAS lists Robinhood Chain as chainid 4663."""
        self.assertEqual(bot.ETHERSCAN_CHAIN_ID["robinhood"], "4663")

    def test_all_networks_have_an_etherscan_chain_id(self):
        for chain in bot.NETWORKS:
            self.assertIn(chain, bot.ETHERSCAN_CHAIN_ID)

    def test_scanner_api_key_is_accepted_as_etherscan_alias(self):
        """Deployments already carry an Etherscan key under SCANNER_API_KEY."""
        import importlib
        os.environ["SCANNER_API_KEY"] = "TESTKEY123"
        os.environ.pop("ETHERSCAN_API_KEY", None)
        reloaded = importlib.reload(bot)
        self.assertEqual(reloaded.ETHERSCAN_API_KEY, "TESTKEY123")
        # restore module state for the rest of the suite
        os.environ.pop("SCANNER_API_KEY", None)
        importlib.reload(bot)


class TestOpenSourceRequirement(unittest.TestCase):
    def test_signals_default_does_not_require_open_source(self):
        """Most Robinhood tokens are unverified; requiring source rejected the
        whole chain."""
        self.assertFalse(signals.Filters().require_open_source)

    def test_env_can_restore_the_old_behaviour(self):
        os.environ["SIG_REQUIRE_OPEN_SOURCE"] = "true"
        try:
            self.assertTrue(signals.Filters.from_env().require_open_source)
        finally:
            os.environ.pop("SIG_REQUIRE_OPEN_SOURCE", None)

    def test_env_can_explicitly_disable_it(self):
        os.environ["SIG_REQUIRE_OPEN_SOURCE"] = "false"
        try:
            self.assertFalse(signals.Filters.from_env().require_open_source)
        finally:
            os.environ.pop("SIG_REQUIRE_OPEN_SOURCE", None)

    def test_unverified_token_is_not_rejected_by_default(self):
        pair = {
            "chainId": "robinhood",
            "baseToken": {"address": "0xabc", "symbol": "X"},
            "quoteToken": {"address": "0xdef", "symbol": "WETH"},
            "priceUsd": "0.01", "liquidity": {"usd": 50_000},
            "marketCap": 500_000, "fdv": 500_000,
            "volume": {"m5": 20_000, "h1": 200_000, "h6": 400_000, "h24": 400_000},
            "txns": {"m5": {"buys": 40, "sells": 10, "buyers": 35, "sellers": 9},
                     "h1": {"buys": 300, "sells": 100}},
            "priceChange": {"m5": 8, "h1": 20, "h6": 50, "h24": 60},
            "pairCreatedAt": 1790242400000,
        }
        sec = {"is_honeypot": False, "buy_tax": 0.0, "sell_tax": 0.0,
               "is_open_source": False, "source": "etherscan"}
        verdict = signals.evaluate(pair, security=sec, filters=signals.Filters())
        self.assertNotIn("not_open_source", verdict.reject_reasons)


class TestGeckoTerminalThrottle(unittest.TestCase):
    """GT's free tier is ~30 calls/min and answers a burst with 429.

    Holder lookups and discovery both hit api.geckoterminal.com, so they must
    share one budget — otherwise each path believes it owns the whole quota.
    """

    def setUp(self):
        self._saved_interval = bot.GT_MIN_INTERVAL
        self._saved_last = bot._gt_last_call
        self._saved_fetch = bot.fetch_json
        bot._gt_last_call = 0.0

    def tearDown(self):
        bot.GT_MIN_INTERVAL = self._saved_interval
        bot._gt_last_call = self._saved_last
        bot.fetch_json = self._saved_fetch

    def test_serialises_calls_to_the_configured_interval(self):
        import asyncio

        calls = []

        async def fake_fetch(session, url, **kwargs):
            calls.append(time.monotonic())
            return {"ok": True}

        bot.fetch_json = fake_fetch
        bot.GT_MIN_INTERVAL = 0.15

        async def go():
            await asyncio.gather(*(bot.gt_fetch_json(None, f"u{i}") for i in range(4)))

        start = time.monotonic()
        asyncio.run(go())
        elapsed = time.monotonic() - start

        self.assertEqual(len(calls), 4)
        # 4 calls at a 0.15s spacing cannot finish in less than ~0.45s.
        self.assertGreaterEqual(elapsed, 0.4)
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.10)

    def test_zero_interval_disables_the_wait(self):
        import asyncio

        async def fake_fetch(session, url, **kwargs):
            return {"ok": True}

        bot.fetch_json = fake_fetch
        bot.GT_MIN_INTERVAL = 0.0

        async def go():
            await asyncio.gather(*(bot.gt_fetch_json(None, f"u{i}") for i in range(3)))

        start = time.monotonic()
        asyncio.run(go())
        self.assertLess(time.monotonic() - start, 0.2)

    def test_lock_survives_a_new_event_loop(self):
        """Per-test asyncio.run() creates a fresh loop each time."""
        import asyncio

        async def fake_fetch(session, url, **kwargs):
            return {"ok": True}

        bot.fetch_json = fake_fetch
        bot.GT_MIN_INTERVAL = 0.0
        asyncio.run(bot.gt_fetch_json(None, "u1"))
        asyncio.run(bot.gt_fetch_json(None, "u2"))  # must not raise


class TestAlertFormatting(unittest.TestCase):
    """Regression: an unmeasured top-100 must not crash the alert.

    GeckoTerminal publishes no 51-100 band, so ``holder_pct`` carries
    ``top100=None``. ``send_alert`` formatted it with ``:.1f``, raising
    "unsupported format string passed to NoneType.__format__". Because the alert
    text is built outside the try that guards ``tg_send``, that killed the whole
    scan cycle on every alert — so the bot looked like it found nothing while it
    was actually finding candidates and dying at the last step.
    """

    def test_fmt_pct_handles_unmeasured(self):
        self.assertEqual(bot._fmt_pct(None), "n/a")
        self.assertEqual(bot._fmt_pct(4.5944), "4.6%")
        self.assertEqual(bot._fmt_pct(0), "0.0%")
        self.assertEqual(bot._fmt_pct("bad"), "n/a")

    def _alert(self, top100):
        return {
            "chain": "robinhood", "token_address": "0x" + "ab" * 20, "symbol": "X",
            "name": "X", "pair_address": "0x" + "cd" * 20, "total_score": 61.0,
            "vol_5m": 1.0, "liquidity": 1.0, "market_cap": 1.0,
            "buys_5m": 1, "sells_5m": 1, "buys_1h": 1, "sells_1h": 1,
            "chg_5m": 1.0, "chg_1h": 1.0, "chg_6h": 1.0,
            "security": {"source": "etherscan", "is_verified": True},
            "holder_pct": (4.59, 16.61, top100),
            "cex_count": 0, "has_perps": False, "tier1": 0,
            "age_minutes": None,          # also null for GT pools with no created_at
            "dex_url": "", "price_usd": 1.0, "price_native": 0.0,
            "signal_bonus": 0.0, "signal_penalty": 0.0, "signal_notes": [],
        }

    def test_send_alert_survives_unmeasured_top100_and_null_age(self):
        import asyncio

        sent = []

        async def fake_tg(text, **kwargs):
            sent.append(text)

        saved_tg = bot.tg_send
        saved_kb = bot.build_alert_keyboard
        bot.tg_send = fake_tg
        bot.build_alert_keyboard = lambda *a, **k: None
        try:
            asyncio.run(bot.send_alert(self._alert(None)))
        finally:
            bot.tg_send = saved_tg
            bot.build_alert_keyboard = saved_kb

        self.assertEqual(len(sent), 1)
        self.assertIn("Top 100: n/a", sent[0])
        self.assertIn("Age: <b>? min</b>", sent[0])

    def test_send_alert_still_renders_a_measured_top100(self):
        import asyncio

        sent = []

        async def fake_tg(text, **kwargs):
            sent.append(text)

        saved_tg = bot.tg_send
        saved_kb = bot.build_alert_keyboard
        bot.tg_send = fake_tg
        bot.build_alert_keyboard = lambda *a, **k: None
        try:
            asyncio.run(bot.send_alert(self._alert(85.0)))
        finally:
            bot.tg_send = saved_tg
            bot.build_alert_keyboard = saved_kb

        self.assertIn("Top 100: 85.0%", sent[0])

    def test_alert_build_failure_does_not_propagate(self):
        """A send failure is logged, not raised, so the cycle continues."""
        import asyncio

        async def boom(text, **kwargs):
            raise RuntimeError("telegram down")

        saved_tg = bot.tg_send
        saved_kb = bot.build_alert_keyboard
        bot.tg_send = boom
        bot.build_alert_keyboard = lambda *a, **k: None
        try:
            asyncio.run(bot.send_alert(self._alert(None)))  # must not raise
        finally:
            bot.tg_send = saved_tg
            bot.build_alert_keyboard = saved_kb


    def test_etherscan_alert_shows_verification_not_goplus(self):
        """Robinhood's security source is Etherscan; the alert must say so and
        surface the verification result instead of mislabelling it GoPlus."""
        import asyncio

        sent = []

        async def fake_tg(text, **kwargs):
            sent.append(text)

        saved_tg = bot.tg_send
        saved_kb = bot.build_alert_keyboard
        bot.tg_send = fake_tg
        bot.build_alert_keyboard = lambda *a, **k: None
        try:
            alert = self._alert(None)
            alert["security"] = {"source": "etherscan", "is_verified": True,
                                 "verification_known": True}
            asyncio.run(bot.send_alert(alert))
        finally:
            bot.tg_send = saved_tg
            bot.build_alert_keyboard = saved_kb

        self.assertIn("Security (Etherscan)", sent[0])
        self.assertIn("Verified source: ✅ Yes", sent[0])
        self.assertNotIn("GoPlus", sent[0])


if __name__ == "__main__":
    unittest.main()
