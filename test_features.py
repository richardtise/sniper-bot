"""Tests for Phase 2 feature logging + outcome labelling.

Run with: python -m unittest -v test_features
"""

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "1:TEST")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")

import bot  # noqa: E402  (import after env stubs)
import label_outcomes  # noqa: E402


def _insert_feature(conn, **overrides):
    row = bot._empty_feature()
    row.update(overrides)
    cols = bot.FEATURE_FIELDS
    conn.execute(
        f"INSERT INTO features ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [row[c] for c in cols],
    )
    return row


class TestFeatureSchema(unittest.TestCase):
    def test_schema_contains_every_field(self):
        sql = bot._feature_schema_sql()
        for field in bot.FEATURE_FIELDS:
            self.assertIn(field, sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS features", sql)

    def test_empty_feature_has_all_fields(self):
        row = bot._empty_feature()
        self.assertEqual(set(row), set(bot.FEATURE_FIELDS))


class TestFeatureLogger(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "features.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(bot._feature_schema_sql())
        conn.commit()
        conn.close()

    def test_writes_rows_and_marks_alerts(self):
        logger = bot.FeatureLogger(self.db, enabled=True)
        logger.start()
        _ = None
        row = bot._empty_feature()
        row.update({
            "ts_utc": "2024-01-01T00:00:00+00:00", "ts_epoch": 1.0,
            "chain": "bsc", "token_address": "0xabc", "symbol": "RUN",
            "price_usd": 1.0, "hand_score": 72.0,
        })
        logger.log_row(row)
        logger.mark_alert_sent("bsc", "0xabc")
        logger.stop()

        conn = sqlite3.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        symbol, score, alert = conn.execute(
            "SELECT symbol, hand_score, alert_sent FROM features"
        ).fetchone()
        conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(symbol, "RUN")
        self.assertEqual(score, 72.0)
        self.assertEqual(alert, 1)

    def test_disabled_logger_writes_nothing(self):
        logger = bot.FeatureLogger(self.db, enabled=False)
        logger.start()
        logger.log_row(bot._empty_feature())
        logger.stop()
        conn = sqlite3.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_migration_adds_new_columns_to_an_existing_table(self):
        """A deployment's existing features table must gain the new columns.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so the
        added score-breakdown columns only appear because ``init_db`` calls
        ``ensure_feature_columns``.
        """
        db = os.path.join(self.tmpdir, "old.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE features (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts_utc TEXT, chain TEXT, token_address TEXT, hand_score REAL, "
            "rejected INTEGER, reject_reasons TEXT)"
        )
        conn.commit()

        bot.ensure_feature_columns(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(features)")}
        conn.close()

        for field in ("score_vol_liq", "score_buy_5m", "penalties_total",
                      "ceiling_score", "score_holder", "score_cex"):
            self.assertIn(field, cols, f"{field} was not migrated onto the old table")

    def test_writer_persists_the_score_breakdown(self):
        logger = bot.FeatureLogger(self.db, enabled=True)
        logger.start()
        row = bot._empty_feature()
        row.update({
            "ts_utc": "2026-09-26T13:53:00+00:00", "ts_epoch": 1.0,
            "chain": "base", "token_address": "0xboar", "symbol": "boar",
            "score_vol_liq": 4.0, "score_buy_5m": 6.0, "score_price": 0.0,
            "penalties_total": 0.0, "ceiling_score": 98.25,
            "hand_score": 48.0, "alert_threshold": 63.86,
        })
        logger.log_row(row)
        logger.stop()

        conn = sqlite3.connect(self.db)
        got = conn.execute(
            "SELECT score_vol_liq, penalties_total, ceiling_score, hand_score, "
            "alert_threshold FROM features"
        ).fetchone()
        conn.close()
        self.assertEqual(got, (4.0, 0.0, 98.25, 48.0, 63.86))


class TestLabelOutcomes(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "labels.db")
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript(bot._feature_schema_sql())
        self.base = 1_700_000_000
        for offset, price in [(0, 1.0), (600, 2.5), (3600, 3.0), (7200, 1.2)]:
            _insert_feature(
                self.conn, ts_epoch=self.base + offset, chain="bsc",
                token_address="0xdead", price_usd=price,
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_forward_multiple_and_hit_labels(self):
        label_outcomes.label(self.conn, [1, 6, 24], verbose=False)
        row = self.conn.execute(
            "SELECT max_mult_1h, max_mult_6h, max_mult_24h, "
            "hit_2x_1h, hit_3x_1h, hit_5x_1h FROM features WHERE ts_epoch=?",
            (self.base,),
        ).fetchone()
        self.assertAlmostEqual(row[0], 3.0)   # 2.5 then 3.0 within 1h
        self.assertAlmostEqual(row[1], 3.0)
        self.assertAlmostEqual(row[2], 3.0)
        self.assertEqual(row[3], 1)           # hit 2x
        self.assertEqual(row[4], 1)           # hit 3x
        self.assertEqual(row[5], 0)           # never 5x

    def test_last_row_has_no_label(self):
        label_outcomes.label(self.conn, [1], verbose=False)
        value = self.conn.execute(
            "SELECT max_mult_1h FROM features WHERE ts_epoch=?", (self.base + 7200,)
        ).fetchone()[0]
        self.assertIsNone(value)

    def test_export_csv(self):
        label_outcomes.label(self.conn, [1], verbose=False)
        out = os.path.join(self.tmpdir, "export.csv")
        n = label_outcomes.export_csv(self.conn, out)
        self.assertEqual(n, 4)
        with open(out) as fh:
            header = fh.readline()
        self.assertIn("max_mult_1h", header)
        self.assertIn("hand_score", header)


class TestFilterGates(unittest.IsolatedAsyncioTestCase):
    """Guards against the pass-2 regression that flooded Base with alerts."""

    async def asyncSetUp(self):
        import signals as signals_module

        self._saved = {
            "USE_SIGNALS": bot.USE_SIGNALS,
            "SIGNAL_BONUS_WEIGHT": bot.SIGNAL_BONUS_WEIGHT,
            "EARLY_RUNNER_MODE": bot.EARLY_RUNNER_MODE,
            "ALERT_THRESHOLD": bot.ALERT_THRESHOLD,
            "PHASE1_MIN_SCORE": bot.PHASE1_MIN_SCORE,
            "ALLOW_SECURITY_FALLBACK": bot.ALLOW_SECURITY_FALLBACK,
            "SIGNAL_FILTERS": bot.SIGNAL_FILTERS,
            "PAIR_HISTORY": bot.PAIR_HISTORY,
            "get_token_security": bot.get_token_security,
            "get_holder_concentration": bot.get_holder_concentration,
            "get_cex_listings": bot.get_cex_listings,
            "fetch_json": bot.fetch_json,
        }
        # Isolate from the operator's .env: a global/per-chain mcap ceiling or
        # per-chain MIN_SCORE override would otherwise decide these gate tests
        # before the code under test runs.
        self._saved_env = {
            k: os.environ.get(k) for k in (
                "MAX_MARKET_CAP_USD", "BASE_MAX_MARKET_CAP_USD",
                "ROBINHOOD_MAX_MARKET_CAP_USD", "BASE_MIN_SCORE",
                "ROBINHOOD_MIN_SCORE",
            )
        }
        for k in self._saved_env:
            os.environ.pop(k, None)
        bot.security_cache.clear()
        bot.USE_SIGNALS = True
        bot.SIGNAL_FILTERS = signals_module.Filters()
        bot.PAIR_HISTORY = signals_module.PairHistory()
        bot.PHASE1_MIN_SCORE = 0

        async def fake_holders(session, chain, token):
            # No provider data: an unmeasured HolderData, not measured zeros.
            return bot.HolderData()

        async def fake_cex(session, chain, token):
            return (0, False, 0)

        bot.get_holder_concentration = fake_holders
        bot.get_cex_listings = fake_cex

    async def asyncTearDown(self):
        for key, value in self._saved.items():
            setattr(bot, key, value)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        bot.security_cache.clear()

    def _pair(self):
        now_ms = time.time() * 1000
        return {
            "chainId": "base", "dexId": "uniswap_v3", "pairAddress": "0x" + "ab" * 20,
            "baseToken": {"address": "0x" + "cd" * 20, "symbol": "MEH", "name": "Meh"},
            "quoteToken": {"symbol": "WETH", "address": "0x" + "ef" * 20},
            "priceUsd": "0.001", "priceNative": "0.000001",
            "liquidity": {"usd": 10_000.0}, "marketCap": 100_000.0,
            "volume": {"m5": 1_000.0, "h1": 5_000.0, "h6": 20_000.0, "h24": 60_000.0},
            "txns": {"m5": {"buys": 10, "sells": 5, "buyers": 8, "sellers": 4},
                     "h1": {"buys": 30, "sells": 20}},
            "priceChange": {"m5": 1.0, "h1": 5.0, "h6": 10.0, "h24": 20.0},
            "pairCreatedAt": now_ms - 60 * 60 * 1000,
            "source": "test",
        }

    def _good_security(self):
        sec = bot._security_placeholder("goplus")
        sec.update({"is_open_source": True, "lp_locked": True, "source": "goplus"})
        return sec

    def _install_security(self, security):
        async def fake_security(session, chain, token):
            return dict(security)
        bot.get_token_security = fake_security

    async def test_signal_bonus_cannot_promote_at_the_default_weight(self):
        """SIGNAL_BONUS_WEIGHT defaults to 0.0, so the hand score stays the gate.

        This is the pass-2 anti-noise guarantee and it must not regress.
        """
        self._install_security(self._good_security())
        bot.SIGNAL_BONUS_WEIGHT = 0.0
        bot.ALERT_THRESHOLD = 0
        promoted = await bot.evaluate_token(None, self._pair())
        self.assertIsNotNone(promoted, "sanity: pair should clear a zero threshold")

        bot.ALERT_THRESHOLD = 65
        result = await bot.evaluate_token(None, self._pair())
        self.assertIsNone(result, "at weight 0.0 the bonus must not promote anything")

    async def test_signal_bonus_can_promote_when_explicitly_weighted(self):
        """Above 0.0 the knob is now live; before this change it did nothing.

        The gate used to read `legacy_total`, which never contains the bonus, so
        SIGNAL_BONUS_WEIGHT could not promote a token at *any* value. The gate is
        now `total_score` (identical at the 0.0 default). This pins the opt-in
        behaviour so the risk of re-opening the pass-2 flood stays a deliberate
        choice rather than an accident.
        """
        self._install_security(self._good_security())
        bot.SIGNAL_BONUS_WEIGHT = 1.0
        bot.ALERT_THRESHOLD = 65
        result = await bot.evaluate_token(None, self._pair())
        self.assertIsNotNone(result, "a fully weighted bonus should be able to promote")

    async def test_unknown_security_dropped_by_default(self):
        calls = {"honeypot": 0}

        async def fake_fetch(session, url, headers=None, use_coingecko_limiter=False):
            if "gopluslabs" in url:
                return {"result": {}}
            if "honeypot.is" in url:
                calls["honeypot"] += 1
                return {"simulationSuccess": True, "honeypotResult": {"isHoneypot": False},
                        "simulationResult": {"buyTax": 0, "sellTax": 0}, "contractCode": {}}
            return None

        bot.fetch_json = fake_fetch
        bot.ALLOW_SECURITY_FALLBACK = False
        self.assertIsNone(await bot.get_token_security(None, "base", "0xdead"))
        self.assertEqual(calls["honeypot"], 0, "fallback must not be called when disabled")

    async def test_fallback_requires_successful_simulation(self):
        responses = {
            "goplus": {"result": {}},
            "no_sim": {"simulationSuccess": False, "honeypotResult": {"isHoneypot": False}},
            "sim": {"simulationSuccess": True, "honeypotResult": {"isHoneypot": True},
                    "simulationResult": {"buyTax": 0, "sellTax": 0}, "contractCode": {}},
        }
        state = {"mode": "no_sim"}

        async def fake_fetch(session, url, headers=None, use_coingecko_limiter=False):
            if "gopluslabs" in url:
                return responses["goplus"]
            if "honeypot.is" in url:
                return responses[state["mode"]]
            return None

        self._saved_fetch = bot.fetch_json
        bot.fetch_json = fake_fetch
        bot.ALLOW_SECURITY_FALLBACK = True

        bot.security_cache.clear()
        self.assertIsNone(await bot.get_token_security(None, "base", "0xaaa"),
                          "un-simulated honeypot.is data must be treated as unknown")

        state["mode"] = "sim"
        bot.security_cache.clear()
        sec = await bot.get_token_security(None, "base", "0xbbb")
        self.assertIsNotNone(sec)
        self.assertEqual(sec["source"], "honeypot.is")
        self.assertTrue(sec["is_honeypot"])

    async def test_early_runner_lane_is_and_gated(self):
        self._install_security(self._good_security())
        bot.USE_SIGNALS = True
        bot.SIGNAL_BONUS_WEIGHT = 0.0
        bot.ALERT_THRESHOLD = 65
        self._young = self._pair()
        self._young.update({
            "liquidity": {"usd": 30_000.0},
            "marketCap": 1_500_000.0,
            "volume": {"m5": 9_000.0, "h1": 12_000.0, "h6": 12_000.0, "h24": 12_000.0},
            "txns": {"m5": {"buys": 40, "sells": 15, "buyers": 30, "sellers": 10},
                     "h1": {"buys": 40, "sells": 15}},
            "pairCreatedAt": (time.time() - 10 * 60) * 1000,
        })

        # Off: a strong young pool cannot reach the hand-tuned threshold.
        bot.EARLY_RUNNER_MODE = False
        self.assertIsNone(await bot.evaluate_token(None, dict(self._young)))

        # On: the AND-gated fast lane lets it through.
        bot.EARLY_RUNNER_MODE = True
        result = await bot.evaluate_token(None, dict(self._young))
        self.assertIsNotNone(result, "young strong pool should alert via the fast lane")

        # Weak unique-buyer base disqualifies it even with the lane on.
        weak = dict(self._young)
        weak["txns"] = {"m5": {"buys": 40, "sells": 15, "buyers": 2, "sellers": 2},
                        "h1": {"buys": 40, "sells": 15}}
        self.assertIsNone(await bot.evaluate_token(None, weak))

    async def test_per_chain_floor_override(self):
        os.environ["BASE_MIN_LIQUIDITY_USD"] = "50000"
        self.addCleanup(lambda: os.environ.pop("BASE_MIN_LIQUIDITY_USD", None))
        self.assertEqual(bot._chain_floor("base", "MIN_LIQUIDITY_USD", 8000.0), 50000.0)
        self.assertEqual(bot._chain_floor("bsc", "MIN_LIQUIDITY_USD", 8000.0), 8000.0)


class _RecordingLogger:
    """Stands in for the FeatureLogger so tests can read the rows as logged."""

    def __init__(self):
        self.rows = []

    def log_row(self, row):
        self.rows.append(dict(row))

    def mark_alert_sent(self, *a, **k):
        pass

    def stats(self):
        return {}


class TestScoreBreakdownLogging(unittest.IsolatedAsyncioTestCase):
    """Every evaluation must leave a row a model can actually learn from.

    Two regressions are pinned here, both of which bit on the boar miss:

    * a floor reject used to be logged with zeros for every window and ratio,
      because the block that filled them sat *after* the floor checks;
    * only the hand total was stored, so "48 against a bar of 64" could not be
      attributed to the components that withheld the points.
    """

    async def asyncSetUp(self):
        import signals as signals_module

        self._saved = {
            "FEATURE_LOGGER": bot.FEATURE_LOGGER,
            "USE_SIGNALS": bot.USE_SIGNALS,
            "SIGNAL_FILTERS": bot.SIGNAL_FILTERS,
            "SIGNAL_BONUS_WEIGHT": bot.SIGNAL_BONUS_WEIGHT,
            "EARLY_RUNNER_MODE": bot.EARLY_RUNNER_MODE,
            "ALERT_THRESHOLD": bot.ALERT_THRESHOLD,
            "PHASE1_MIN_SCORE": bot.PHASE1_MIN_SCORE,
            "PAIR_HISTORY": bot.PAIR_HISTORY,
            "get_token_security": bot.get_token_security,
            "get_holder_concentration": bot.get_holder_concentration,
            "get_cex_listings": bot.get_cex_listings,
            # Isolate the venue gate: this class tests score attribution, and its
            # fixtures use a v4 pool. An operator who sets
            # REQUIRE_TRADEABLE_VENUE=true would otherwise short-circuit scoring
            # before any component is logged.
            "REQUIRE_TRADEABLE_VENUE": bot.REQUIRE_TRADEABLE_VENUE,
        }
        self.recorder = _RecordingLogger()
        bot.FEATURE_LOGGER = self.recorder
        bot.USE_SIGNALS = True
        bot.SIGNAL_FILTERS = signals_module.Filters()
        bot.PAIR_HISTORY = signals_module.PairHistory()
        bot.SIGNAL_BONUS_WEIGHT = 0.0
        bot.PHASE1_MIN_SCORE = 0
        bot.REQUIRE_TRADEABLE_VENUE = False
        bot.security_cache.clear()

    async def asyncTearDown(self):
        for key, value in self._saved.items():
            setattr(bot, key, value)
        bot.security_cache.clear()

    def _pair(self, **over):
        now_ms = time.time() * 1000
        pair = {
            "chainId": "base", "dexId": "uniswap_v4", "pairAddress": "0x" + "ab" * 20,
            "baseToken": {"address": "0x" + "cd" * 20, "symbol": "MEH", "name": "Meh"},
            "quoteToken": {"symbol": "WETH", "address": "0x" + "ef" * 20},
            "priceUsd": "0.001", "priceNative": "0.000001",
            "liquidity": {"usd": 10_000.0}, "marketCap": 100_000.0,
            "volume": {"m5": 1_000.0, "h1": 5_000.0, "h6": 20_000.0, "h24": 60_000.0},
            "txns": {"m5": {"buys": 10, "sells": 5, "buyers": 8, "sellers": 4},
                     "h1": {"buys": 30, "sells": 20}},
            "priceChange": {"m5": 1.0, "h1": 5.0, "h6": 10.0, "h24": 20.0},
            "pairCreatedAt": now_ms - 60 * 60 * 1000,
            "source": "test",
        }
        pair.update(over)
        return pair

    def _install_scaffold(self, holders=None, cex=(0, False, 0)):
        async def fake_security(session, chain, token):
            sec = bot._security_placeholder("goplus")
            sec.update({"is_open_source": True, "lp_locked": True, "source": "goplus"})
            return sec

        async def fake_holders(session, chain, token):
            return holders if holders is not None else bot.HolderData()

        async def fake_cex(session, chain, token):
            return cex

        bot.get_token_security = fake_security
        bot.get_holder_concentration = fake_holders
        bot.get_cex_listings = fake_cex

    async def test_floor_reject_still_carries_the_raw_metrics(self):
        """A thin-liquidity reject is still a usable training row."""
        self._install_scaffold()
        pair = self._pair(liquidity={"usd": 500.0}, marketCap=90_000.0,
                          volume={"m5": 400.0, "h1": 900.0, "h6": 1_200.0, "h24": 2_000.0})

        self.assertIsNone(await bot.evaluate_token(None, pair))

        self.assertEqual(len(self.recorder.rows), 1)
        row = self.recorder.rows[0]
        self.assertEqual(row["rejected"], 1)
        self.assertEqual(row["reject_reasons"], "liquidity")
        # The metrics that used to be lost:
        self.assertEqual(row["liquidity_usd"], 500.0)
        self.assertEqual(row["market_cap_usd"], 90_000.0)
        self.assertEqual(row["vol_5m"], 400.0)
        self.assertEqual(row["vol_5m_1h"], 400.0 / 900.0)
        self.assertEqual(row["buys_5m"], 10)
        self.assertIsNotNone(row["age_minutes"])
        self.assertGreater(row["ceiling_score"], 0)
        # Not reached, so legitimately absent rather than a wrong zero.
        self.assertIsNone(row["hand_score"])

    async def test_components_reconstruct_the_hand_score(self):
        """Sum of the logged components minus penalties must equal hand_score."""
        self._install_scaffold()
        bot.ALERT_THRESHOLD = 0  # exercise the scored path, not the gate
        pair = self._pair()

        result = await bot.evaluate_token(None, pair)
        self.assertIsNotNone(result, "sanity: the fixture should clear the gate")

        row = self.recorder.rows[0]
        components = [
            "score_vol_liq", "score_vol_5m_1h", "score_vol_1h_6h", "score_vol_6h_24h",
            "score_buy_5m", "score_buy_1h", "score_price",
            "score_holder", "score_security", "score_cex",
        ]
        for name in components:
            self.assertIsNotNone(row[name], f"{name} must be logged on a scored row")

        total = sum(float(row[name]) for name in components)
        rebuilt = max(0.0, total - float(row["penalties_total"]))
        self.assertAlmostEqual(rebuilt, float(row["hand_score"]), places=6)
        # The bar is a fraction of the ceiling; storing both makes the margin
        # interpretable ("48 of a reachable 98", not "48 of 64").
        self.assertGreater(row["ceiling_score"], 0)
        self.assertLessEqual(row["alert_threshold"], row["ceiling_score"])

    async def test_signal_veto_still_logs_the_components(self):
        """The deep-pool/thin-5m case (boar): vetoed, but fully attributed."""
        self._install_scaffold()
        pair = self._pair(liquidity={"usd": 25_000.0},
                          volume={"m5": 1_000.0, "h1": 40_000.0,
                                  "h6": 120_000.0, "h24": 300_000.0})

        self.assertIsNone(await bot.evaluate_token(None, pair))

        row = self.recorder.rows[0]
        self.assertIn("low_activity", row["reject_reasons"])
        self.assertIn("score_vol_liq", row)
        self.assertIsNotNone(row["score_vol_liq"])
        self.assertIsNotNone(row["penalties_total"])
        self.assertEqual(row["hand_score"], None,
                         "a vetoed token has no hand score; components explain why")


if __name__ == "__main__":
    unittest.main()



class TestFirstSightLatency(unittest.IsolatedAsyncioTestCase):
    """The log must answer "was it seen early?" independently of the gate.

    Detection latency and score are separate failures that look identical in a
    reject-only log. First sight is therefore logged *before* the floors, so a
    token that is instantly rejected still records the age/mcap at which the
    scanner first laid eyes on it.
    """

    async def asyncSetUp(self):
        self._saved_first_sight = dict(bot._first_sight)
        bot._first_sight.clear()
        self._saved_logger = bot.FEATURE_LOGGER

    async def asyncTearDown(self):
        bot._first_sight.clear()
        bot._first_sight.update(self._saved_first_sight)
        bot.FEATURE_LOGGER = self._saved_logger

    def _young_pair(self, **over):
        now_ms = time.time() * 1000
        pair = {
            "chainId": "robinhood", "dexId": "uniswap", "pairAddress": "0x" + "ab" * 20,
            "baseToken": {"address": "0x" + "cd" * 20, "symbol": "CATTO", "name": "Catto"},
            "quoteToken": {"symbol": "WETH", "address": "0x" + "ef" * 20},
            "priceUsd": "0.00001", "priceNative": "0.000000001",
            # Deliberately below the liquidity floor: the sighting must still log.
            "liquidity": {"usd": 4_500.0}, "marketCap": 6_000.0,
            "volume": {"m5": 520.0, "h1": 900.0, "h6": 900.0, "h24": 900.0},
            "txns": {"m5": {"buys": 6, "sells": 1, "buyers": 5, "sellers": 1},
                     "h1": {"buys": 6, "sells": 1}},
            "priceChange": {"m5": 3.0, "h1": 3.0, "h6": 3.0, "h24": 3.0},
            "pairCreatedAt": now_ms - 3 * 60 * 1000,   # three minutes old
            "source": "geckoterminal:new_pools",
        }
        pair.update(over)
        return pair

    async def test_first_sight_logs_age_and_mcap_before_floors(self):
        with self.assertLogs("pump_bot_v5", level="INFO") as cm:
            result = await bot.evaluate_token(None, self._young_pair())

        self.assertIsNone(result, "sanity: this fixture must fail a floor")
        text = "\n".join(cm.output)
        self.assertIn("FIRST SIGHT CATTO@robinhood", text)
        self.assertIn("mcap=$6,000", text)
        self.assertIn("liq=$4,500", text)
        self.assertIn("geckoterminal:new_pools", text)
        # ~3 minutes old: proves the sighting is timestamped, not just counted.
        self.assertIn("age=3.0m", text)

    async def test_first_sight_logged_only_once_per_token(self):
        pair = self._young_pair()
        with self.assertLogs("pump_bot_v5", level="INFO") as first:
            await bot.evaluate_token(None, pair)
        self.assertIn("FIRST SIGHT", "\n".join(first.output))

        # A second evaluation of the same token must not re-log the sighting.
        with self.assertNoLogs("pump_bot_v5", level="INFO"):
            await bot.evaluate_token(None, pair)


class TestMarketCapCeiling(unittest.IsolatedAsyncioTestCase):
    """A mcap floor without a ceiling alerts on tokens that have already run.

    2026-09-29: the scanner pinged a $3M and a $22M token. Nothing in the code
    said "too big to enter" — MAX_MARKET_CAP_USD defaults to 0 (disabled), so
    these tests pin both the rejection and the no-op default.
    """

    async def asyncSetUp(self):
        self._saved = {
            "FEATURE_LOGGER": bot.FEATURE_LOGGER,
            "USE_SIGNALS": bot.USE_SIGNALS,
            "PHASE1_MIN_SCORE": bot.PHASE1_MIN_SCORE,
            "ALERT_THRESHOLD": bot.ALERT_THRESHOLD,
            "get_token_security": bot.get_token_security,
            "get_holder_concentration": bot.get_holder_concentration,
            "get_cex_listings": bot.get_cex_listings,
        }
        self._saved_env = os.environ.get("MAX_MARKET_CAP_USD")
        # Isolate the gate from the operator's .env: a global/per-chain ceiling
        # or per-chain MIN_SCORE would otherwise decide these ceiling tests.
        self._saved_gate_env = {
            k: os.environ.get(k) for k in (
                "BASE_MAX_MARKET_CAP_USD", "ROBINHOOD_MAX_MARKET_CAP_USD",
                "MIN_SCORE", "BASE_MIN_SCORE", "ROBINHOOD_MIN_SCORE",
                "SCORE_NORMALIZE", "MIN_EFFECTIVE_SCORE",
            )
        }
        for k in self._saved_gate_env:
            os.environ.pop(k, None)
        self.recorder = _RecordingLogger()
        bot.FEATURE_LOGGER = self.recorder
        bot.USE_SIGNALS = False
        bot.PHASE1_MIN_SCORE = 0
        # These tests are about the ceiling, not the score gate.
        bot.ALERT_THRESHOLD = 0

        async def fake_security(session, chain, token):
            sec = bot._security_placeholder("goplus")
            sec.update({"is_open_source": True, "lp_locked": True, "source": "goplus"})
            return sec

        async def fake_holders(session, chain, token):
            return bot.HolderData()

        async def fake_cex(session, chain, token):
            return (0, False, 0)

        bot.get_token_security = fake_security
        bot.get_holder_concentration = fake_holders
        bot.get_cex_listings = fake_cex

    async def asyncTearDown(self):
        for key, value in self._saved.items():
            setattr(bot, key, value)
        if self._saved_env is None:
            os.environ.pop("MAX_MARKET_CAP_USD", None)
        else:
            os.environ["MAX_MARKET_CAP_USD"] = self._saved_env
        for key, value in self._saved_gate_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _pair(self, market_cap):
        now_ms = time.time() * 1000
        return {
            "chainId": "robinhood", "dexId": "uniswap", "pairAddress": "0x" + "ab" * 20,
            "baseToken": {"address": "0x" + "cd" * 20, "symbol": "STKDOG", "name": "Dog"},
            "quoteToken": {"symbol": "WETH", "address": "0x" + "ef" * 20},
            "priceUsd": "0.00002", "priceNative": "0.000000008",
            "liquidity": {"usd": 519_343.0}, "marketCap": market_cap,
            "volume": {"m5": 210_931.0, "h1": 3_236_015.0,
                       "h6": 10_293_411.0, "h24": 10_293_411.0},
            "txns": {"m5": {"buys": 114, "sells": 78, "buyers": 90, "sellers": 60},
                     "h1": {"buys": 1626, "sells": 1201}},
            "priceChange": {"m5": 30.64, "h1": 44.56, "h6": 5824.0, "h24": 5824.0},
            "pairCreatedAt": now_ms - 180 * 60 * 1000,
            "source": "geckoterminal:trending",
        }

    async def test_ceiling_rejects_already_run_token(self):
        os.environ["MAX_MARKET_CAP_USD"] = "200000"
        self.assertIsNone(await bot.evaluate_token(None, self._pair(2_183_213.0)))
        row = self.recorder.rows[-1]
        self.assertEqual(row["reject_reasons"], "mcap_too_high")
        # The metrics are still logged — a ceiling reject stays studyable.
        self.assertEqual(row["market_cap_usd"], 2_183_213.0)

    async def test_ceiling_allows_an_early_sized_token(self):
        os.environ["MAX_MARKET_CAP_USD"] = "200000"
        result = await bot.evaluate_token(None, self._pair(45_000.0))
        self.assertIsNotNone(result, "a $45k mcap must not hit the ceiling")

    async def test_ceiling_disabled_by_default(self):
        os.environ.pop("MAX_MARKET_CAP_USD", None)
        result = await bot.evaluate_token(None, self._pair(2_183_213.0))
        self.assertIsNotNone(result, "0/unset must keep the old permissive behaviour")

    async def test_per_chain_ceiling_override(self):
        os.environ.pop("MAX_MARKET_CAP_USD", None)
        os.environ["ROBINHOOD_MAX_MARKET_CAP_USD"] = "100000"
        try:
            self.assertIsNone(await bot.evaluate_token(None, self._pair(2_183_213.0)))
            self.assertEqual(self.recorder.rows[-1]["reject_reasons"], "mcap_too_high")
        finally:
            os.environ.pop("ROBINHOOD_MAX_MARKET_CAP_USD", None)


class TestConfigSelfAudit(unittest.TestCase):
    """A deployment that relies on code defaults runs a different bot.

    Every guard here is deliberately opt-in, so an unconfigured deployment
    discovers from the lagging DexScreener boost list, has no rug/wash gates, no
    early lane and no alert ceiling. That combination reproduces the exact
    symptoms that prompted this: alerts on $3M/$22M tokens, false positives on
    wash-traded pools, and no early entries.
    """

    def setUp(self):
        self._saved = {k: getattr(bot, k) for k in
                       ("USE_SIGNALS", "USE_GECKOTERMINAL", "EARLY_RUNNER_MODE",
                        "LOG_FEATURES")}
        self._saved_env = os.environ.get("MAX_MARKET_CAP_USD")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(bot, k, v)
        if self._saved_env is None:
            os.environ.pop("MAX_MARKET_CAP_USD", None)
        else:
            os.environ["MAX_MARKET_CAP_USD"] = self._saved_env

    def _all_off(self):
        bot.USE_SIGNALS = False
        bot.USE_GECKOTERMINAL = False
        bot.EARLY_RUNNER_MODE = False
        bot.LOG_FEATURES = False
        os.environ["MAX_MARKET_CAP_USD"] = "0"

    def test_code_defaults_are_reported_as_warnings(self):
        self._all_off()
        warns = " ".join(bot.config_warnings())
        self.assertIn("MAX_MARKET_CAP_USD", warns)
        self.assertIn("USE_SIGNALS", warns)
        self.assertIn("USE_GECKOTERMINAL", warns)
        self.assertIn("EARLY_RUNNER_MODE", warns)
        self.assertIn("LOG_FEATURES", warns)

    def test_no_ceiling_warning_names_the_consequence(self):
        self._all_off()
        warns = " ".join(bot.config_warnings())
        self.assertIn("already ran", warns)

    def test_a_configured_bot_produces_no_warnings(self):
        bot.USE_SIGNALS = True
        bot.USE_GECKOTERMINAL = True
        bot.EARLY_RUNNER_MODE = True
        bot.LOG_FEATURES = True
        os.environ["MAX_MARKET_CAP_USD"] = "100000"
        self.assertEqual(bot.config_warnings(), [])

    def test_health_endpoint_exposes_the_warnings(self):
        """So a misconfigured deployment is visible without reading logs."""
        self._all_off()
        payload = asyncio.run(bot.health_check())
        self.assertIn("config_warnings", payload)
        self.assertTrue(payload["config_warnings"])

    def test_paper_mode_is_reported_as_a_note_not_a_warning(self):
        notes = " ".join(bot.config_notes())
        if bot.PAPER_TRADING:
            self.assertIn("PAPER_TRADING", notes)
        self.assertNotIn("PAPER_TRADING", " ".join(bot.config_warnings()))


class TestFirstSightContext(unittest.TestCase):
    """Alerts must say how early the token was seen and what it was worth then.

    "Why did it ping me at $3M instead of $300k" has two opposite answers —
    discovery never saw it small, or the gate held it back — and telling them
    apart used to require grepping FIRST SIGHT out of the log.
    """

    def setUp(self):
        bot._first_sight.clear()

    def tearDown(self):
        bot._first_sight.clear()

    def _pair(self, mcap):
        return {"chainId": "bsc", "source": "geckoterminal:new_pools",
                "volume": {"m5": 1234.0}, "marketCap": mcap}

    def test_first_sight_is_recorded_once_and_returned(self):
        rec = bot._first_sight_note("bsc", "0xAbC", "BI", self._pair(62000.0),
                                    liquidity=15000.0, market_cap=62000.0,
                                    age_minutes=3.0)
        self.assertEqual(rec["mcap"], 62000.0)
        self.assertEqual(rec["liq"], 15000.0)
        # A later, bigger sighting must not overwrite the original.
        again = bot._first_sight_note("bsc", "0xabc", "BI", self._pair(3_100_000.0),
                                      liquidity=15000.0, market_cap=3_100_000.0,
                                      age_minutes=52.0)
        self.assertEqual(again["mcap"], 62000.0)

    def test_key_is_case_insensitive_across_pools(self):
        bot._first_sight_note("bsc", "0xABC", "BI", self._pair(1.0),
                              liquidity=1.0, market_cap=1.0, age_minutes=1.0)
        self.assertEqual(len(bot._first_sight), 1)

    def test_prune_removes_only_expired_records(self):
        now = time.time()
        bot._first_sight["bsc:0xold"] = {"ts": now - bot.FIRST_SIGHT_TTL - 10}
        bot._first_sight["bsc:0xnew"] = {"ts": now}
        bot.prune_caches()
        self.assertNotIn("bsc:0xold", bot._first_sight)
        self.assertIn("bsc:0xnew", bot._first_sight)
