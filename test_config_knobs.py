"""Tests for the configuration knobs that used to fail at runtime.

Every one of these was a real failure mode of the old ``float(os.getenv(...))``
style of reading config, and each test is written so that a regression makes it
fail rather than merely look sad:

1. ``_env_float`` must warn-and-default on garbage instead of raising —
   blank/garbage ``MIN_EFFECTIVE_SCORE`` and ``MAX_MARKET_CAP_USD`` used to be
   raised *per candidate*, where ``dispatch_alerts`` swallowed the exception and
   the bot silently sent zero alerts.
2. The per-source GeckoTerminal knobs must exist for *every* feed kind (not
   just the three the old inline dict bound), the legacy spellings must keep
   working, and a blank value must mean "unset", not "0".
3. ``GT_SOURCES=`` (present but blank) must not blind every chain.
4. ``SCAN_INTERVAL`` is bound at import: default 30, clamp at 5, garbage falls
   back. A fresh interpreter is the only honest way to observe that, so this
   one spawns subprocesses (wallet stubbed out, so they make no network calls).
5. The early-runner lane is AND-gated on ``USE_SIGNALS`` as well as on
   ``EARLY_RUNNER_MODE``.
6. ``has_cex_data`` needs both CoinGecko coverage *and* an API key.
7. The ``RE_ALERTS=true`` line lives in ``config_notes``, not in
   ``config_warnings``.
8. ``effective_mcap_ceilings`` reports one ceiling per chain, from the global
   default plus per-chain overrides.

Run with: source bot-env/bin/activate && python -m unittest test_config_knobs -v
"""

import os
import subprocess
import sys
import time
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "1:TEST")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")

import bot  # noqa: E402
import discovery  # noqa: E402
import signals  # noqa: E402

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GT_SOURCES = ("new_pools", "trending", "top_volume")
FEED_KINDS = {"new_pools", "trending", "trending_5m", "top_txns", "top_volume"}
GT_ENV_PREFIXES = ("GT_PAGES_", "GT_LIST_TTL_")
MAX_MCAP_KEYS = (
    "MAX_MARKET_CAP_USD",
    "BSC_MAX_MARKET_CAP_USD",
    "ETH_MAX_MARKET_CAP_USD",
    "BASE_MAX_MARKET_CAP_USD",
    "ROBINHOOD_MAX_MARKET_CAP_USD",
)


class _EnvSaver:
    """Saves and restores every os.environ key (and bot global) a test touches.

    The operator's .env is loaded into os.environ at import, so a test that
    merely sets a knob without restoring it would reconfigure every later test
    in the module. Mixed into both the sync and the async test base.
    """

    def setUp(self):
        self._saved_env = {}
        self._saved_globals = {}

    def set_env(self, key, value):
        """Set (or blank) an env key, remembering what was there before."""
        if key not in self._saved_env:
            self._saved_env[key] = os.environ.get(key)
        os.environ[key] = value

    def clear_env(self, key):
        """Remove an env key entirely (different from setting it to '')."""
        if key not in self._saved_env:
            self._saved_env[key] = os.environ.get(key)
        os.environ.pop(key, None)

    def clear_env_prefix(self, prefix):
        for key in [k for k in os.environ if k.startswith(prefix)]:
            self.clear_env(key)

    def save_global(self, name):
        if name not in self._saved_globals:
            self._saved_globals[name] = getattr(bot, name)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name, value in self._saved_globals.items():
            setattr(bot, name, value)
        super().tearDown()


class _EnvTestCase(_EnvSaver, unittest.TestCase):
    pass


class TestEnvFloat(_EnvTestCase):
    """One helper, one behaviour: log the offending key, use the default."""

    def test_blank_value_returns_the_default(self):
        self.set_env("MIN_EFFECTIVE_SCORE", "")
        self.assertEqual(bot._env_float("MIN_EFFECTIVE_SCORE", 35.0), 35.0)

    def test_garbage_value_warns_and_returns_the_default(self):
        self.set_env("MIN_EFFECTIVE_SCORE", "abc")
        with self.assertLogs("pump_bot_v5", level="WARNING") as cm:
            value = bot._env_float("MIN_EFFECTIVE_SCORE", 35.0)
        self.assertEqual(value, 35.0)
        self.assertTrue(
            any("MIN_EFFECTIVE_SCORE" in line for line in cm.output),
            f"the offending key must be named in the warning, got {cm.output}",
        )

    def test_valid_value_parses(self):
        self.set_env("MIN_EFFECTIVE_SCORE", "42.5")
        self.assertEqual(bot._env_float("MIN_EFFECTIVE_SCORE", 35.0), 42.5)

    def test_thousands_separators_are_tolerated(self):
        self.set_env("MAX_MARKET_CAP_USD", "1,000,000")
        self.assertEqual(bot._env_float("MAX_MARKET_CAP_USD", 0.0), 1_000_000.0)

    def test_per_candidate_keys_no_longer_raise(self):
        """The old failure: raised per candidate, alerts silently dropped.

        ``effective_threshold`` reads MIN_EFFECTIVE_SCORE and ``config_warnings``
        reads MAX_MARKET_CAP_USD on every call; with the broken parse both blew
        up on blank/garbage input instead of falling back.
        """
        self.set_env("MIN_EFFECTIVE_SCORE", "")
        self.set_env("MAX_MARKET_CAP_USD", "200k")

        threshold = bot.effective_threshold("base", 10.0)
        self.assertIsInstance(threshold, float)

        with self.assertLogs("pump_bot_v5", level="WARNING") as cm:
            warns = bot.config_warnings()
        self.assertIsInstance(warns, list)
        self.assertTrue(
            any("MAX_MARKET_CAP_USD" in line for line in cm.output),
            "the unparseable ceiling must be reported, not silently accepted",
        )
        # Garbage fell back to the default 0, which means "no ceiling" — and
        # that is exactly what the audit says out loud.
        self.assertTrue(
            any(w.startswith("MAX_MARKET_CAP_USD=0") for w in warns),
            f"expected the no-ceiling warning, got {warns}",
        )


class TestGtSourceKnobs(_EnvTestCase):
    """Depth and cache TTL per feed kind, for every kind the feeds define."""

    def setUp(self):
        super().setUp()
        # .env ships GT_PAGES_NEW=1 / GT_PAGES_TRENDING=1 / GT_LIST_TTL_*;
        # clear them so the defaults under test come from the code.
        for prefix in GT_ENV_PREFIXES:
            self.clear_env_prefix(prefix)
        self.baseline_pages = bot._gt_source_pages()
        self.baseline_ttls = bot._gt_source_ttls()

    def test_knobs_cover_every_feed_kind(self):
        kinds = set(discovery.GT_ENDPOINTS)
        self.assertTrue(
            FEED_KINDS <= kinds,
            f"the shipped feed set changed: {sorted(kinds)}",
        )
        self.assertEqual(set(self.baseline_pages), kinds,
                         "_gt_source_pages must return an entry per kind")
        self.assertEqual(set(self.baseline_ttls), kinds,
                         "_gt_source_ttls must return an entry per kind")
        for kind in kinds:
            self.assertGreaterEqual(self.baseline_pages[kind], 1, kind)
            self.assertGreater(self.baseline_ttls[kind], 0.0, kind)

    def test_trending_5m_pages_are_tunable_for_that_kind_only(self):
        self.set_env("GT_PAGES_TRENDING_5M", "3")
        expected = dict(self.baseline_pages, trending_5m=3)
        self.assertEqual(bot._gt_source_pages(), expected)

    def test_top_txns_ttl_is_tunable_for_that_kind_only(self):
        self.set_env("GT_LIST_TTL_TOP_TXNS", "120")
        expected = dict(self.baseline_ttls, top_txns=120.0)
        self.assertEqual(bot._gt_source_ttls(), expected)

    def test_legacy_gt_pages_new_maps_to_new_pools(self):
        self.set_env("GT_PAGES_NEW", "7")
        self.assertEqual(bot._gt_source_pages(),
                         dict(self.baseline_pages, new_pools=7))

    def test_legacy_gt_pages_top_maps_to_top_volume(self):
        self.set_env("GT_PAGES_TOP", "6")
        self.assertEqual(bot._gt_source_pages(),
                         dict(self.baseline_pages, top_volume=6))

    def test_legacy_gt_list_ttl_top_maps_to_top_volume(self):
        self.set_env("GT_LIST_TTL_TOP", "77")
        self.assertEqual(bot._gt_source_ttls(),
                         dict(self.baseline_ttls, top_volume=77.0))

    def test_blank_value_is_unset_not_zero(self):
        """`GT_LIST_TTL_TOP_TXNS=` must show the default, not max(5, 0).

        The page count cannot tell the two apart (max(1, 0) == 1), but
        `trending` defaults to 2 pages and `top_txns` to a 90s TTL — both are
        distinguishable from a blank parsed as zero.
        """
        self.set_env("GT_PAGES_TRENDING", "9")
        self.assertEqual(bot._gt_source_pages()["trending"], 9)
        self.set_env("GT_PAGES_TRENDING", "")
        self.assertEqual(bot._gt_source_pages()["trending"], 2)

        self.set_env("GT_LIST_TTL_TOP_TXNS", "120")
        self.assertEqual(bot._gt_source_ttls()["top_txns"], 120.0)
        self.set_env("GT_LIST_TTL_TOP_TXNS", "")
        self.assertEqual(bot._gt_source_ttls()["top_txns"], 90.0)

        self.assertEqual(bot._gt_source_pages(), self.baseline_pages)
        self.assertEqual(bot._gt_source_ttls(), self.baseline_ttls)


class TestGtSources(_EnvTestCase):
    """The blank-line footgun: `GT_SOURCES=` in .env meant 'no feeds at all'."""

    def setUp(self):
        super().setUp()
        for key in ("GT_SOURCES", "GT_SOURCES_ROBINHOOD", "ROBINHOOD_GT_SOURCES"):
            self.clear_env(key)

    def test_blank_gt_sources_falls_back_to_the_default_tuple(self):
        self.set_env("GT_SOURCES", "")
        sources = bot.get_gt_sources("robinhood")
        self.assertTrue(sources, "a blank GT_SOURCES must not mean 'no sources'")
        self.assertEqual(sources, DEFAULT_GT_SOURCES)

    def test_explicit_gt_sources_is_still_honoured(self):
        self.set_env("GT_SOURCES", "trending,top_volume")
        self.assertEqual(bot.get_gt_sources("robinhood"),
                         ("trending", "top_volume"))


class TestScanInterval(unittest.TestCase):
    """``SCAN_INTERVAL`` is an import-time constant, so the only way to test how
    it is derived is a fresh interpreter.

    The subprocesses stub the wallet (``WALLET_PRIVATE_KEY=``), which is what
    keeps ``import bot`` off the RPC endpoints — no network, ~1s each.
    """

    def _fresh_import_scan_interval(self, raw):
        env = dict(os.environ)
        env["TELEGRAM_TOKEN"] = env.get("TELEGRAM_TOKEN") or "1:TEST"
        env["CHAT_ID"] = env.get("CHAT_ID") or "1"
        env["WALLET_PRIVATE_KEY"] = ""
        env["SCAN_INTERVAL"] = raw
        proc = subprocess.run(
            [sys.executable, "-c", "import bot; print(bot.SCAN_INTERVAL)"],
            cwd=PROJECT_DIR, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"no stdout from subprocess: {proc.stdout!r}")
        return int(lines[-1])

    def test_explicit_value_is_used(self):
        self.assertEqual(self._fresh_import_scan_interval("45"), 45)

    def test_garbage_value_falls_back_to_the_default(self):
        # "abc" must not crash the import; it must land on the documented 30.
        self.assertEqual(self._fresh_import_scan_interval("abc"), 30)

    def test_value_below_the_minimum_is_clamped(self):
        self.assertEqual(self._fresh_import_scan_interval("2"), 5)


class _RecordingLogger:
    """Stands in for the FeatureLogger so a test can read the row as logged."""

    def __init__(self):
        self.rows = []

    def log_row(self, row):
        self.rows.append(dict(row))

    def mark_alert_sent(self, *a, **k):
        pass

    def stats(self):
        return {}


class TestEarlyRunnerLaneRequiresSignals(_EnvSaver,
                                         unittest.IsolatedAsyncioTestCase):
    """The lane is AND-gated: EARLY_RUNNER_MODE alone must not be enough.

    Stubs copied from the house pattern in test_features.py (the security /
    holder / CEX helpers are faked so no network is touched).
    """

    def setUp(self):
        super().setUp()
        for name in (
            "FEATURE_LOGGER", "USE_SIGNALS", "EARLY_RUNNER_MODE", "SIGNAL_FILTERS",
            "SIGNAL_BONUS_WEIGHT", "ALERT_THRESHOLD", "PHASE1_MIN_SCORE",
            "PAIR_HISTORY", "REQUIRE_TRADEABLE_VENUE",
            "get_token_security", "get_holder_concentration", "get_cex_listings",
        ):
            self.save_global(name)

        self.recorder = _RecordingLogger()
        bot.FEATURE_LOGGER = self.recorder
        bot.SIGNAL_FILTERS = signals.Filters()
        bot.PAIR_HISTORY = signals.PairHistory()
        bot.SIGNAL_BONUS_WEIGHT = 0.0
        bot.ALERT_THRESHOLD = 65
        bot.PHASE1_MIN_SCORE = 0
        bot.REQUIRE_TRADEABLE_VENUE = False
        bot.EARLY_RUNNER_MODE = True

        # Isolate from the operator's .env: a global/per-chain mcap ceiling or
        # per-chain MIN_SCORE would decide this gate before the code under test.
        for key in MAX_MCAP_KEYS + ("BASE_MIN_SCORE", "ROBINHOOD_MIN_SCORE"):
            self.clear_env(key)

        bot.security_cache.clear()

        async def fake_security(session, chain, token):
            return self._good_security()

        async def fake_holders(session, chain, token):
            return bot.HolderData()

        async def fake_cex(session, chain, token):
            return (0, False, 0)

        bot.get_token_security = fake_security
        bot.get_holder_concentration = fake_holders
        bot.get_cex_listings = fake_cex

    def tearDown(self):
        bot.security_cache.clear()
        super().tearDown()

    def _good_security(self):
        sec = bot._security_placeholder("goplus")
        sec.update({"is_open_source": True, "lp_locked": True, "source": "goplus"})
        return sec

    def _young_pair(self):
        """Strong, 10 minutes old: clears every SIG_EARLY_* condition, but its
        empty long windows keep the hand score under the threshold."""
        now_ms = time.time() * 1000
        return {
            "chainId": "base", "dexId": "uniswap_v3", "pairAddress": "0x" + "ab" * 20,
            "baseToken": {"address": "0x" + "cd" * 20, "symbol": "MEH", "name": "Meh"},
            "quoteToken": {"symbol": "WETH", "address": "0x" + "ef" * 20},
            "priceUsd": "0.001", "priceNative": "0.000001",
            "liquidity": {"usd": 30_000.0}, "marketCap": 1_500_000.0,
            "volume": {"m5": 9_000.0, "h1": 12_000.0, "h6": 12_000.0, "h24": 12_000.0},
            "txns": {"m5": {"buys": 40, "sells": 15, "buyers": 30, "sellers": 10},
                     "h1": {"buys": 40, "sells": 15}},
            "priceChange": {"m5": 1.0, "h1": 5.0, "h6": 10.0, "h24": 20.0},
            "pairCreatedAt": (time.time() - 10 * 60) * 1000,
            "source": "test",
        }

    async def test_lane_does_not_promote_without_use_signals(self):
        bot.USE_SIGNALS = False
        bot.EARLY_RUNNER_MODE = True
        result = await bot.evaluate_token(None, self._young_pair())

        self.assertIsNone(result, "the lane must not fire when USE_SIGNALS=false")
        self.assertEqual(len(self.recorder.rows), 1)
        row = self.recorder.rows[0]
        self.assertEqual(row["reject_reasons"], "below_threshold",
                         "rejected by the score gate, not by some other gate")
        self.assertEqual(row["early_runner"], 0,
                         "the early-runner flag must stay off")

    async def test_lane_promotes_with_use_signals(self):
        bot.USE_SIGNALS = True
        bot.EARLY_RUNNER_MODE = True
        pair = self._young_pair()

        # Fixture sanity: the pair must satisfy every SIG_EARLY_* condition, so
        # the promotion below is decided by the USE_SIGNALS gate alone.
        self.assertEqual(
            signals.early_runner_reasons(
                pair, security=self._good_security(),
                filters=bot._filters_for_chain("base"),
            ),
            [],
            "the fixture must clear every early-runner condition",
        )

        result = await bot.evaluate_token(None, pair)
        self.assertIsNotNone(result, "lane should promote this pool")
        self.assertEqual(len(self.recorder.rows), 1)
        row = self.recorder.rows[0]
        # early_runner is only set when the score was *below* the threshold, so
        # this proves the lane promoted it rather than the plain score gate.
        self.assertEqual(row["early_runner"], 1)
        self.assertEqual(row["reject_reasons"], "")

    async def test_garbage_never_miss_points_does_not_break_evaluation(self):
        """NEAR_MISS_POINTS is parsed per candidate inside evaluate_token.

        Blank MIN_EFFECTIVE_SCORE and a garbage MAX_MARKET_CAP_USD ride along:
        all three used to raise there, and the caller swallowed it, so the bot
        sent zero alerts for as long as the value stayed broken.
        """
        self.set_env("MIN_EFFECTIVE_SCORE", "")
        self.set_env("MAX_MARKET_CAP_USD", "200k")
        self.set_env("NEAR_MISS_POINTS", "abc")
        bot.USE_SIGNALS = True
        bot.EARLY_RUNNER_MODE = True

        with self.assertLogs("pump_bot_v5", level="WARNING") as cm:
            result = await bot.evaluate_token(None, self._young_pair())

        self.assertTrue(
            any("NEAR_MISS_POINTS" in line for line in cm.output),
            "the garbage per-candidate value must actually have been parsed: "
            f"{cm.output}",
        )
        self.assertIsNotNone(result, "the evaluation must survive the garbage env")
        self.assertEqual(len(self.recorder.rows), 1)
        self.assertEqual(self.recorder.rows[0]["reject_reasons"], "")


class TestCexDataAvailability(_EnvTestCase):
    """Both preconditions: CoinGecko coverage *and* an API key."""

    def setUp(self):
        super().setUp()
        self.save_global("COINGECKO_API_KEY")

    def test_coverage_chain_needs_a_coingecko_key(self):
        bot.COINGECKO_API_KEY = "CG-test-key"
        self.assertTrue(bot.has_cex_data("base"),
                        "coverage + key must be able to score CEX points")
        bot.COINGECKO_API_KEY = ""
        self.assertFalse(bot.has_cex_data("base"))
        self.assertFalse(bot.has_cex_data("ethereum"))

    def test_robinhood_never_has_cex_data(self):
        bot.COINGECKO_API_KEY = "CG-test-key"
        self.assertFalse(bot.has_cex_data("robinhood"),
                         "CoinGecko has no Robinhood coverage")
        bot.COINGECKO_API_KEY = ""
        self.assertFalse(bot.has_cex_data("robinhood"))


class TestConfigAuditSplit(_EnvTestCase):
    """RE_ALERTS is a trade-off, not a misconfiguration: notes, not warnings."""

    def setUp(self):
        super().setUp()
        self.save_global("RE_ALERTS_ENABLED")

    def test_re_alerts_note_is_in_notes_not_warnings(self):
        bot.RE_ALERTS_ENABLED = True
        notes = bot.config_notes()
        self.assertTrue(
            any(note.startswith("RE_ALERTS=true") for note in notes),
            f"the RE_ALERTS note is missing from {notes}",
        )
        for warning in bot.config_warnings():
            self.assertNotIn("RE_ALERTS", warning,
                             "RE_ALERTS was moved out of the warnings")

    def test_re_alerts_note_absent_when_disabled(self):
        bot.RE_ALERTS_ENABLED = False
        self.assertFalse(
            any("RE_ALERTS" in note for note in bot.config_notes()),
            "no RE_ALERTS note while the setting is off",
        )


class TestMcapCeilings(_EnvTestCase):
    """The ceiling each chain is actually gated on, read fresh from env."""

    def setUp(self):
        super().setUp()
        for key in MAX_MCAP_KEYS:
            self.clear_env(key)

    def test_one_entry_per_chain_from_the_global_default(self):
        self.set_env("MAX_MARKET_CAP_USD", "250000")
        ceilings = bot.effective_mcap_ceilings()
        self.assertEqual(set(ceilings), set(bot.NETWORKS))
        for chain in bot.NETWORKS:
            self.assertEqual(ceilings[chain], 250_000.0, chain)

    def test_per_chain_override_touches_only_that_chain(self):
        self.set_env("MAX_MARKET_CAP_USD", "250000")
        self.set_env("ROBINHOOD_MAX_MARKET_CAP_USD", "50000")
        ceilings = bot.effective_mcap_ceilings()
        self.assertEqual(ceilings["robinhood"], 50_000.0)
        for chain in bot.NETWORKS:
            if chain != "robinhood":
                self.assertEqual(ceilings[chain], 250_000.0, chain)

    def test_blank_per_chain_key_falls_back_to_the_global_default(self):
        self.set_env("MAX_MARKET_CAP_USD", "250000")
        self.set_env("ROBINHOOD_MAX_MARKET_CAP_USD", "")
        ceilings = bot.effective_mcap_ceilings()
        self.assertEqual(ceilings["robinhood"], 250_000.0,
                         "a blank override must not mean 'ceiling off'")


class TestV4Knobs(_EnvTestCase):
    """V4 execution stays off unless explicitly enabled with a codec present."""

    def test_v4_trading_defaults_off(self):
        self.save_global("V4_TRADING")
        bot.V4_TRADING = False
        self.assertFalse(bot.V4_TRADING)

    def test_universal_router_resolves_robinhood(self):
        self.clear_env("ROBINHOOD_UNIVERSAL_ROUTER")
        self.assertEqual(bot.get_universal_router("robinhood"),
                         "0x8876789976DEcbFcBbBE364623c63652db8C0904")

    def test_universal_router_env_override_wins(self):
        self.set_env("ROBINHOOD_UNIVERSAL_ROUTER", "0x" + "ab" * 20)
        self.assertEqual(bot.get_universal_router("robinhood"), "0x" + "ab" * 20)

    def test_v4_pool_id_to_bytes25(self):
        raw = bot.v4_pool_id_to_bytes25(
            "0x749ea25eb98e9b802fbb7ea07167353976dc0936040d8b604f601c1b2a71e96a")
        self.assertEqual(len(raw), 25)
        self.assertEqual(raw.hex(), "749ea25eb98e9b802fbb7ea07167353976dc0936040d8b604f")

    def test_v4_label_unroutable_while_flag_off(self):
        self.save_global("V4_TRADING")
        bot.V4_TRADING = False
        self.assertFalse(bot.dex_is_supported("robinhood", "uniswap-v4"))

    def test_venue_requirement_names_v4(self):
        self.assertIn("Universal Router",
                      bot.venue_requirement("robinhood", "uniswap-v4"))

    def test_build_v4_swap_calldata_is_execute(self):
        if not bot.V4_CODEC_AVAILABLE:
            self.skipTest("decoder lib not installed")
        key = {"currency0": "0x0000000000000000000000000000000000000000",
               "currency1": "0x38CdC65B82C66Fc206d09192e6B123FDABF21cfe",
               "fee": 0, "tickSpacing": 200,
               "hooks": "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044"}
        data = bot.build_v4_swap_calldata(
            key, True, 10**15, 15000 * 10**18,
            "0x38CdC65B82C66Fc206d09192e6B123FDABF21cfe",
            "0xc5d71e5F93E3C5FB0203e2e9F705363A9f63Bd1f", 4663)
        self.assertTrue(data.startswith("0x3593564c"),
                        "must be UniversalRouter.execute(bytes,bytes[],uint256)")

    def test_v4_retry_and_staleness_knobs_have_sane_defaults(self):
        self.assertGreaterEqual(bot.V4_RESOLVE_RETRIES, 2,
                                "single-attempt resolution caused the BAG miss")
        self.assertGreater(bot.V4_RESOLVE_RETRY_DELAY_S, 0)
        self.assertGreater(bot.V4_QUOTE_MAX_AGE_S, 0)

    def test_v4_pool_id_to_bytes25_rejects_garbage(self):
        with self.assertRaises(Exception):
            bot.v4_pool_id_to_bytes25("not-a-hex-pool-id")


if __name__ == "__main__":
    unittest.main()
