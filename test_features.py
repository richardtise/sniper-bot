"""Tests for Phase 2 feature logging + outcome labelling.

Run with: python -m unittest -v test_features
"""

import os
import sqlite3
import tempfile
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


if __name__ == "__main__":
    unittest.main()
