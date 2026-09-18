"""Tests for Phase 2 feature logging + outcome labelling.

Run with: python -m unittest -v test_features
"""

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
        bot.security_cache.clear()
        bot.USE_SIGNALS = True
        bot.SIGNAL_FILTERS = signals_module.Filters()
        bot.PAIR_HISTORY = signals_module.PairHistory()
        bot.PHASE1_MIN_SCORE = 0

        async def fake_holders(session, chain, token):
            return (0.0, 0.0, 0.0)

        async def fake_cex(session, chain, token):
            return (0, False, 0)

        bot.get_holder_concentration = fake_holders
        bot.get_cex_listings = fake_cex

    async def asyncTearDown(self):
        for key, value in self._saved.items():
            setattr(bot, key, value)
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

    async def test_signal_bonus_cannot_promote_below_threshold(self):
        self._install_security(self._good_security())
        bot.SIGNAL_BONUS_WEIGHT = 1.0  # even at full bonus weight...
        bot.ALERT_THRESHOLD = 0
        promoted = await bot.evaluate_token(None, self._pair())
        self.assertIsNotNone(promoted, "sanity: pair should clear a zero threshold")

        # ...the hand-tuned score is still the gate.
        bot.ALERT_THRESHOLD = 65
        result = await bot.evaluate_token(None, self._pair())
        self.assertIsNone(result, "signal bonus must not promote a sub-threshold token")

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


if __name__ == "__main__":
    unittest.main()

