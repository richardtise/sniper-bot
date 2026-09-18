"""Unit tests for signals.py — run with: python -m unittest -v test_signals"""

import time
import unittest

import signals
from signals import Filters, PairHistory, evaluate, normalize_security


def make_pair(**over):
    now_ms = time.time() * 1000
    pair = {
        "chainId": "bsc",
        "dexId": "pancakeswap_v3",
        "pairAddress": "0x" + "ab" * 20,
        "baseToken": {"address": "0x" + "cd" * 20, "symbol": "RUN", "name": "Runner"},
        "quoteToken": {"address": "0x" + "ef" * 20, "symbol": "WBNB", "name": "WBNB"},
        "priceUsd": "0.001",
        "priceNative": "0.000001",
        "liquidity": {"usd": 60_000.0},
        "marketCap": 600_000.0,
        "fdv": 700_000.0,
        "volume": {"m5": 20_000.0, "h1": 90_000.0, "h6": 300_000.0, "h24": 900_000.0},
        "txns": {
            "m5": {"buys": 40, "sells": 20, "buyers": 30, "sellers": 15},
            "h1": {"buys": 200, "sells": 150},
        },
        "priceChange": {"m5": 8.0, "h1": 40.0, "h6": 120.0, "h24": 200.0},
        "pairCreatedAt": now_ms - 60 * 60 * 1000,  # 1 hour old
        "source": "geckoterminal:new_pools",
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(pair.get(key), dict):
            merged = dict(pair[key])
            merged.update(value)
            pair[key] = merged
        else:
            pair[key] = value
    return pair


GOOD_GT = {
    "gt_score": 72.0,
    "gt_verified": True,
    "websites": ["https://runner.example"],
    "twitter_handle": "runner",
    "holders": {"count": 800, "distribution_percentage": {"top_10": "18.0"}},
    "developer_holding_percentage": 0.5,
    "is_honeypot": False,
}

GOOD_SEC = {
    "is_honeypot": False, "buy_tax": 1.0, "sell_tax": 1.0,
    "is_open_source": True, "is_proxy": False, "is_mintable": False,
    "owner_change_balance": False, "transfer_pausable": False,
    "slippage_modifiable": False, "can_take_back_ownership": False,
    "is_whitelisted": False, "is_blacklisted": False, "lp_locked": True,
    "holder_count": 800, "source": "goplus",
}


class TestHardFilters(unittest.TestCase):
    def test_healthy_pair_passes(self):
        v = evaluate(make_pair(), security=GOOD_SEC, gt=GOOD_GT)
        self.assertFalse(v.rejected, v.reject_reasons)
        self.assertGreater(v.bonus, v.penalty)

    def test_honeypot_rejected(self):
        sec = dict(GOOD_SEC, is_honeypot=True)
        v = evaluate(make_pair(), security=sec, gt=GOOD_GT)
        self.assertTrue(v.rejected)
        self.assertIn("honeypot", v.reject_reasons)

    def test_tax_rejected(self):
        sec = dict(GOOD_SEC, buy_tax=15.0)
        v = evaluate(make_pair(), security=sec)
        self.assertIn("tax", v.reject_reasons)

    def test_hidden_owner_rejected(self):
        # GoPlus returns "1" as a string — must be coerced.
        sec = dict(GOOD_SEC, hidden_owner="1")
        v = evaluate(make_pair(), security=sec)
        self.assertIn("hidden_owner", v.reject_reasons)

    def test_not_open_source_rejected(self):
        sec = dict(GOOD_SEC, is_open_source=False)
        v = evaluate(make_pair(), security=sec)
        self.assertIn("not_open_source", v.reject_reasons)

    def test_top10_concentration_rejected_in_rug_mode(self):
        gt = dict(GOOD_GT, holders={"count": 800, "distribution_percentage": {"top_10": "75.0"}})
        v = evaluate(make_pair(), security=GOOD_SEC, gt=gt,
                     filters=Filters(holder_stance="rug"))
        self.assertIn("top10_concentrated", v.reject_reasons)

    def test_concentrated_supply_rewarded_in_pump_mode(self):
        # Default stance: early runners are concentrated, that is the point.
        gt = dict(GOOD_GT, holders={"count": 200, "distribution_percentage": {"top_10": "72.0"}})
        v = evaluate(make_pair(), security=GOOD_SEC, gt=gt)
        self.assertFalse(v.rejected, v.reject_reasons)
        self.assertTrue(any("concentrated_supply" in n for n in v.notes))

    def test_low_liquidity_rejected(self):
        v = evaluate(make_pair(liquidity={"usd": 1000.0}), security=GOOD_SEC)
        self.assertTrue(any(r.startswith("liq<") for r in v.reject_reasons))

    def test_sell_dominated_rejected(self):
        v = evaluate(
            make_pair(txns={"m5": {"buys": 2, "sells": 30, "buyers": 2, "sellers": 20}}),
            security=GOOD_SEC,
        )
        self.assertIn("sell_dominated_5m", v.reject_reasons)

    def test_avg_trade_dominates_pool_rejected(self):
        v = evaluate(
            make_pair(
                liquidity={"usd": 20_000.0},
                volume={"m5": 60_000.0, "h1": 90_000.0},
                txns={"m5": {"buys": 5, "sells": 5, "buyers": 5, "sellers": 5}},
            ),
            security=GOOD_SEC,
        )
        self.assertIn("avg_trade>10%liq", v.reject_reasons)

    def test_exotic_quote_rejected(self):
        v = evaluate(
            make_pair(quoteToken={"symbol": "SCAM2", "address": "0x" + "11" * 20}),
            security=GOOD_SEC,
        )
        self.assertIn("exotic_quote", v.reject_reasons)

    def test_major_asset_rejected(self):
        v = evaluate(
            make_pair(baseToken={"address": "0x" + "cd" * 20, "symbol": "WBNB", "name": "WBNB"}),
            security=GOOD_SEC,
        )
        self.assertIn("major_asset", v.reject_reasons)

    def test_late_blowoff_rejected(self):
        v = evaluate(
            make_pair(priceChange={"m5": 2.0, "h1": 1500.0}),
            security=GOOD_SEC,
        )
        self.assertIn("late_blowoff", v.reject_reasons)

    def test_too_new_rejected(self):
        pair = make_pair(pairCreatedAt=(time.time() - 30) * 1000)  # 30 seconds old
        v = evaluate(pair, security=GOOD_SEC, gt=GOOD_GT)
        self.assertIn("too_new", v.reject_reasons)

    def test_liq_mcap_ratio_rejected(self):
        v = evaluate(
            make_pair(liquidity={"usd": 9_000.0}, marketCap=5_000_000.0),
            security=GOOD_SEC,
        )
        self.assertIn("liq/mcap_too_low", v.reject_reasons)


class TestRunnerSignals(unittest.TestCase):
    def test_boost_penalised(self):
        plain = evaluate(make_pair(), security=GOOD_SEC, gt=GOOD_GT)
        boosted = evaluate(make_pair(boosts={"amount": 500}), security=GOOD_SEC, gt=GOOD_GT)
        self.assertGreater(boosted.penalty, plain.penalty)
        self.assertIn("paid_boost", boosted.notes)

    def test_unlocked_lp_penalised(self):
        sec = dict(GOOD_SEC, lp_locked=False)
        v = evaluate(make_pair(), security=sec, gt=GOOD_GT)
        self.assertIn("lp_unlocked", v.notes)

    def test_unique_buyers_rewarded(self):
        strong = evaluate(
            make_pair(txns={"m5": {"buys": 60, "sells": 20, "buyers": 55, "sellers": 12}}),
            security=GOOD_SEC, gt=GOOD_GT,
        )
        self.assertIn("buy_pressure_5m", strong.notes)
        self.assertTrue(any("unique buyers" in n for n in strong.notes))

    def test_wash_trading_rejected(self):
        # With unique-buyer data present, a few wallets cycling funds is now a
        # hard reject (AND-gate), not merely a penalty.
        v = evaluate(
            make_pair(txns={"m5": {"buys": 30, "sells": 10, "buyers": 4, "sellers": 3}}),
            security=GOOD_SEC, gt=GOOD_GT,
        )
        self.assertTrue(v.rejected)
        self.assertIn("wash:few_unique_buyers", v.reject_reasons)

    def test_low_activity_rejected(self):
        # 5m volume far below the liquidity floor -> dead/fake pool.
        v = evaluate(
            make_pair(liquidity={"usd": 100_000.0}, volume={"m5": 500.0, "h1": 5000.0}),
            security=GOOD_SEC, gt=GOOD_GT,
        )
        self.assertIn("low_activity", v.reject_reasons)

    def test_unique_buyer_gate_is_noop_without_gt_data(self):
        # DexScreener pairs carry no `buyers`; the gate must not fire for them.
        pair = make_pair()
        pair["txns"]["m5"].pop("buyers", None)
        pair["txns"]["m5"].pop("sellers", None)
        v = evaluate(pair, security=GOOD_SEC)
        self.assertNotIn("wash:few_unique_buyers", v.reject_reasons)
        self.assertNotIn("buyers5m_too_low", v.reject_reasons)

    def test_history_acceleration_bonus(self):
        hist = PairHistory()
        pair = make_pair()
        for i in range(4):
            snap = make_pair(volume={"m5": 1_000.0 * (i + 1), "h1": 90_000.0})
            hist.observe(snap, security=GOOD_SEC, gt=GOOD_GT, ts=time.time() - (4 - i) * 30)
        v = evaluate(pair, security=GOOD_SEC, gt=GOOD_GT, history=hist)
        self.assertGreater(v.signals.get("observations", 0), 0)
        self.assertTrue(any("vol_accel" in n for n in v.notes))

    def test_stricter_filters_change_verdict(self):
        f = Filters(min_liquidity_usd=100_000.0)
        v = evaluate(make_pair(), security=GOOD_SEC, gt=GOOD_GT, filters=f)
        self.assertTrue(v.rejected)

    def test_filters_from_env(self):
        import os
        os.environ["SIG_MIN_TXNS_5M"] = "999"
        try:
            f = Filters.from_env()
            self.assertEqual(f.min_txns_5m, 999)
        finally:
            del os.environ["SIG_MIN_TXNS_5M"]


class TestNormalizeSecurity(unittest.TestCase):
    def test_goplus_strings(self):
        sec = normalize_security({
            "is_honeypot": "1", "buy_tax": "2.5", "is_lp_locked": "1",
            "hidden_owner": "0", "creator_percent": "0.03",
        })
        self.assertTrue(sec["honeypot"])
        self.assertEqual(sec["buy_tax"], 2.5)
        self.assertTrue(signals._truthy(sec["lp_locked"]))
        self.assertFalse(sec["hidden_owner"])
        self.assertAlmostEqual(sec["creator_pct"], 3.0)

    def test_gt_holders(self):
        sec = normalize_security(None, {
            "holders": {"count": 123, "distribution_percentage": {"top_10": "22.5"}},
            "gt_score": 65, "websites": ["https://x.io"], "twitter_handle": "x",
        })
        self.assertEqual(sec["holder_count"], 123)
        self.assertEqual(sec["top10_pct"], 22.5)
        self.assertEqual(sec["gt_score"], 65)
        self.assertIn("twitter", sec["socials"])


class TestEarlyRunnerLane(unittest.TestCase):
    """The AND-gated fast lane for young pools (EARLY_RUNNER_MODE)."""

    def _young_pair(self, **over):
        now_ms = time.time() * 1000
        pair = make_pair(
            liquidity={"usd": 30_000.0},
            marketCap=1_500_000.0,
            volume={"m5": 9_000.0, "h1": 12_000.0, "h6": 12_000.0, "h24": 12_000.0},
            txns={"m5": {"buys": 40, "sells": 15, "buyers": 30, "sellers": 10},
                  "h1": {"buys": 40, "sells": 15}},
            priceChange={"m5": 20.0, "h1": 20.0, "h6": 20.0, "h24": 20.0},
            pairCreatedAt=now_ms - 10 * 60 * 1000,  # 10 minutes old
        )
        for key, value in over.items():
            pair[key] = value
        return pair

    def test_strong_young_pool_qualifies(self):
        reasons = signals.early_runner_reasons(self._young_pair(), security=GOOD_SEC)
        self.assertEqual(reasons, [])

    def test_weak_unique_buyers_rejected(self):
        pair = self._young_pair(
            txns={"m5": {"buys": 40, "sells": 15, "buyers": 3, "sellers": 2}}
        )
        self.assertIn("early_unique_buyers", signals.early_runner_reasons(pair, security=GOOD_SEC))

    def test_low_activity_rejected(self):
        pair = self._young_pair(volume={"m5": 300.0, "h1": 300.0, "h6": 300.0, "h24": 300.0})
        self.assertIn("early_activity", signals.early_runner_reasons(pair, security=GOOD_SEC))

    def test_old_pool_rejected(self):
        pair = self._young_pair(pairCreatedAt=(time.time() - 3600) * 1000)
        self.assertIn("not_early", signals.early_runner_reasons(pair, security=GOOD_SEC))

    def test_already_vertical_rejected(self):
        pair = self._young_pair(priceChange={"m5": 400.0, "h1": 400.0, "h6": 400.0, "h24": 400.0})
        self.assertIn("early_already_vertical", signals.early_runner_reasons(pair, security=GOOD_SEC))

    def test_missing_unique_buyer_data_rejected(self):
        pair = self._young_pair(txns={"m5": {"buys": 40, "sells": 15}})
        self.assertIn("early_no_unique_buyer_data", signals.early_runner_reasons(pair, security=GOOD_SEC))

    def test_qualifies_helper(self):
        self.assertTrue(signals.qualifies_as_early_runner(self._young_pair(), security=GOOD_SEC))


class TestPairHistory(unittest.TestCase):
    def test_prune(self):
        hist = PairHistory(ttl_seconds=10)
        hist.observe(make_pair(), ts=time.time() - 100)
        hist.prune()
        pair = make_pair()
        key = PairHistory.key("bsc", pair["baseToken"]["address"])
        self.assertNotIn(key, hist._data)

    def test_empty_trend_is_zero(self):
        hist = PairHistory()
        tr = hist.trend("bsc", "0xdead")
        self.assertEqual(tr["observations"], 0)
        self.assertEqual(tr["vol_accel"], 0.0)


if __name__ == "__main__":
    unittest.main()
