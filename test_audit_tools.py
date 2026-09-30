"""Tests for the research tooling in diag/ — statistics and window building.

These pin the mechanics that decide whether an audit number is trustworthy:

* the rank/AUC/sign-test helpers return known values on known input;
* windows are real *time* windows — GeckoTerminal omits empty 5-minute bars, so
  an index-based window would silently measure 70 minutes instead of 60 and
  quietly inflate every ratio;
* partial windows clamp to available history only when asked.

Run with: python -m unittest -v test_audit_tools
"""

import math
import os
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "1:TEST")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")

from diag import component_audit as audit  # noqa: E402


def _candle(ts, close, volume, high=None):
    high = close if high is None else high
    return [ts, close, high, close, close, volume]


class TestStatistics(unittest.TestCase):
    def test_spearman_perfect_and_inverted(self):
        self.assertAlmostEqual(audit.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(audit.spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)

    def test_spearman_is_rank_based(self):
        # Monotone but wildly non-linear: still a perfect rank correlation.
        self.assertAlmostEqual(audit.spearman([1, 2, 3, 4], [1, 10, 1000, 10**9]), 1.0)

    def test_spearman_constant_series_is_none(self):
        self.assertIsNone(audit.spearman([1, 1, 1, 1], [1, 2, 3, 4]))

    def test_spearman_handles_ties(self):
        rho = audit.spearman([1, 1, 2, 2], [1, 2, 3, 4])
        self.assertGreater(rho, 0.7)

    def test_auc_perfect_separation(self):
        self.assertEqual(audit.auc([0, 0, 1, 1], [0, 0, 1, 1]), 1.0)
        self.assertEqual(audit.auc([1, 1, 0, 0], [0, 0, 1, 1]), 0.0)

    def test_auc_is_half_on_noise(self):
        # Every positive ties with every negative.
        self.assertEqual(audit.auc([1, 1, 1, 1], [0, 1, 0, 1]), 0.5)

    def test_auc_needs_both_classes(self):
        self.assertIsNone(audit.auc([1, 2, 3], [1, 1, 1]))

    def test_binom_two_sided_known_values(self):
        self.assertAlmostEqual(audit.binom_two_sided(5, 5), 2 * (1 / 32))
        self.assertAlmostEqual(audit.binom_two_sided(5, 10), 1.0)
        self.assertIsNone(audit.binom_two_sided(0, 0))


class TestGridAlignment(unittest.TestCase):
    def test_missing_bars_are_filled_with_zero_volume(self):
        # Two bars 15 minutes apart with a 5-minute grid: the gap is 2 empty bars.
        candles = [_candle(1000, 1.0, 10.0), _candle(1900, 1.0, 10.0)]
        grid = audit.to_grid(candles)
        self.assertEqual([g[0] for g in grid], [1000, 1300, 1600, 1900])
        self.assertEqual([g[5] for g in grid], [10.0, 0.0, 0.0, 10.0])
        # Price is carried forward across the empty slots.
        self.assertEqual([g[4] for g in grid], [1.0, 1.0, 1.0, 1.0])

    def test_windows_are_time_windows_not_index_windows(self):
        """The regression this guards: 426 sparse candles spanning 467 slots.

        With a 60-minute window, only the bars inside the hour may be summed —
        an index-based slice would reach further back in time whenever the feed
        omitted empty bars.
        """
        base = 1_000_000
        candles = []
        for i in range(80):
            ts = base + i * 300
            if 20 <= i < 40:
                continue  # feed omitted these bars
            candles.append(_candle(ts, 1.0 + i * 0.001, 5.0))
        grid = audit.to_grid(candles)
        self.assertEqual(len(grid), 80)

        obs = audit.build_observations(candles, created_ts=base - 86400,
                                       step=1, lookback=36, horizon=12,
                                       allow_partial=True)
        self.assertTrue(obs)
        for o in obs:
            # vol_1h is exactly the sum of the 12 grid bars ending at this bar.
            idx = next(i for i, g in enumerate(grid) if g[0] + 300 == o["ts"])
            expected = sum(g[5] for g in grid[idx - 11:idx + 1])
            self.assertAlmostEqual(o["vol_1h"], expected)
            # Zero-volume bars inside the omitted stretch must reduce the sum.
            self.assertLessEqual(o["vol_1h"], 12 * 5.0 + 1e-9)

    def test_forward_return_uses_bars_after_the_observation(self):
        base = 1_000_000
        # Flat at 1.0, then a permanent step to 3.0 from bar 40 onward.
        candles = []
        for i in range(60):
            close = 3.0 if i >= 40 else 1.0
            candles.append(_candle(base + i * 300, close, 5.0))
        obs = audit.build_observations(candles, created_ts=base - 86400,
                                       step=1, lookback=30, horizon=12,
                                       allow_partial=True)
        self.assertTrue(obs)
        first = obs[0]
        self.assertAlmostEqual(first["fwd_max_1h"], 200.0)  # 1.0 -> 3.0
        # The bar sitting on the step itself cannot use its own high.
        at_step = [o for o in obs if abs(o["ts"] - (base + 40 * 300 + 300)) < 1][0]
        self.assertAlmostEqual(at_step["fwd_max_1h"], 0.0)


class TestPartialWindows(unittest.TestCase):
    def test_partial_is_opt_in(self):
        base = 1_000_000
        candles = [_candle(base + i * 300, 1.0 + i * 0.001, 5.0) for i in range(40)]
        strict = audit.build_observations(candles, created_ts=base,
                                          step=1, lookback=24, horizon=12)
        # The bar needs 288 bars of history for a full 24 h window.
        self.assertEqual(strict, [])

        partial = audit.build_observations(candles, created_ts=base,
                                           step=1, lookback=24, horizon=12,
                                           allow_partial=True)
        self.assertTrue(partial)
        self.assertGreater(partial[0]["vol_24h"], 0)


if __name__ == "__main__":
    unittest.main()



def _now_iso():
    """A fresh pool timestamp, so age-based cohort filtering is exercised."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

class TestPopulationFloors(unittest.TestCase):
    """The floors must be a reproducible function of the population.

    These tests exist because the previous floors were set from two tokens whose
    outcome was already known — a selection error, not a small-sample one. What
    is pinned here is that the derivation is outcome-blind (nothing in the
    selection path reads price or performance) and that its uncertainty is
    reported rather than hidden.
    """

    def setUp(self):
        from diag import population_floors as pf
        self.pf = pf

    def test_quantile_matches_linear_interpolation(self):
        self.assertEqual(self.pf.quantile([1, 2, 3, 4, 5], 0.0), 1)
        self.assertEqual(self.pf.quantile([1, 2, 3, 4, 5], 1.0), 5)
        self.assertEqual(self.pf.quantile([1, 2, 3, 4, 5], 0.5), 3)

    def test_quantile_is_defined_for_tiny_samples(self):
        """statistics.quantiles needs n>=2 and drops the extremes; a floor does not."""
        self.assertEqual(self.pf.quantile([7.0], 0.9), 7.0)
        self.assertIsNone(self.pf.quantile([], 0.5))

    def test_bootstrap_ci_brackets_the_point_estimate(self):
        values = [float(i) for i in range(1, 51)]
        lo, hi = self.pf.bootstrap_quantile_ci(values, 0.75, iterations=200)
        point = self.pf.quantile(values, 0.75)
        self.assertLessEqual(lo, point)
        self.assertGreaterEqual(hi, point)

    def test_bootstrap_ci_needs_enough_points(self):
        self.assertEqual(self.pf.bootstrap_quantile_ci([1.0, 2.0], 0.5), (None, None))

    def test_majors_are_excluded_because_the_scanner_excludes_them(self):
        pools = [
            {"attributes": {"address": "0xa", "name": "WETH / USDC",
                            "reserve_in_usd": "5000000", "pool_created_at": _now_iso(),
                            "volume_usd": {"m5": "100"}, "transactions": {"m5": {"buys": 5, "sells": 1}}}},
            {"attributes": {"address": "0xb", "name": "NEW / WETH",
                            "reserve_in_usd": "5000", "pool_created_at": _now_iso(),
                            "volume_usd": {"m5": "100"}, "transactions": {"m5": {"buys": 5, "sells": 1}}}},
        ]
        cohort = self.pf.select_cohort(pools, max_age_minutes=30.0,
                                       chain_of={"0xa": "base", "0xb": "base"})
        self.assertEqual([r["liquidity_usd"] for r in cohort], [5000.0])

    def test_cohort_selection_reads_no_outcome_fields(self):
        """Outcome-blindness is the property that removes survivorship bias.

        Checked against the parsed code with the docstring stripped, so a comment
        that merely *mentions* price can neither satisfy nor fail this test.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(self.pf.select_cohort))
        func = tree.body[0]
        if (func.body and isinstance(func.body[0], ast.Expr)
                and isinstance(func.body[0].value, ast.Constant)):
            func.body = func.body[1:]          # drop the docstring
        code = ast.unparse(func).lower()
        # Outcome/look-ahead identifiers only. Plain words like "return" are
        # Python keywords and would match the function's own return statement.
        for forbidden in ("max_mult", "hit_", "chg_", "priceusd", "price_usd",
                          "pricechange", "ath", "forward", "profit", "pnl"):
            self.assertNotIn(forbidden, code)

    def test_selection_does_not_hardcode_token_names(self):
        import inspect
        src = inspect.getsource(self.pf)
        for token in ("boar", "CATTO", "PSF", "Tbook", "WALLET", "SHRUB"):
            self.assertNotIn(token, src)

    def test_age_is_flagged_censored_and_not_derived(self):
        rows = [
            {"age_minutes": 10.0, "liquidity_usd": 100.0, "vol_liq_ratio": 0.1,
             "txns_5m": 5, "buy_ratio_5m": 0.5, "buyers_5m": 3},
            {"age_minutes": 20.0, "liquidity_usd": 200.0, "vol_liq_ratio": 0.2,
             "txns_5m": 10, "buy_ratio_5m": 0.6, "buyers_5m": 6},
        ]
        summary = self.pf.summarise(rows, quantile_map={"_default": 0.9})
        self.assertTrue(summary["age_minutes"]["censored"])
        self.assertFalse(summary["liquidity_usd"]["censored"])
        block = self.pf.format_env_block(summary, chain="test", n=2,
                                         date="2026-09-30", command="cmd")
        self.assertIn("NOT derived", block)
        self.assertNotIn("SIG_EARLY_MAX_AGE_MINUTES=", block)

    def test_env_block_reports_provenance_and_sample_size(self):
        rows = [{"liquidity_usd": float(i), "vol_liq_ratio": 0.01 * i,
                 "txns_5m": i, "buy_ratio_5m": 0.1 * i, "buyers_5m": i}
                for i in range(1, 21)]
        summary = self.pf.summarise(rows, quantile_map={"_default": 0.75})
        block = self.pf.format_env_block(summary, chain="robinhood", n=20,
                                         date="2026-09-30", command="the-cmd")
        self.assertIn("robinhood", block)
        self.assertIn("the-cmd", block)
        self.assertIn("n=20", block)

    def test_pool_metrics_returns_none_without_age(self):
        pool = {"attributes": {"address": "0xa", "reserve_in_usd": "1000"}}
        self.assertIsNone(self.pf.pool_metrics(pool))

    def test_pool_metrics_computes_ratios_safely(self):
        pool = {"attributes": {
            "address": "0xa", "reserve_in_usd": "0", "pool_created_at": _now_iso(),
            "volume_usd": {"m5": "50"}, "transactions": {"m5": {"buys": 0, "sells": 0}}}}
        m = self.pf.pool_metrics(pool)
        self.assertEqual(m["vol_liq_ratio"], 0.0)   # no ZeroDivisionError
        self.assertEqual(m["buy_ratio_5m"], 0.0)


class TestThresholdFitting(unittest.TestCase):
    """The fitter must refuse thin samples and never leak across tokens."""

    def setUp(self):
        from diag import fit_thresholds as ft
        self.ft = ft

    def test_base_rate_and_lift(self):
        self.assertAlmostEqual(self.ft.base_rate([1, 0, 0, 0]), 0.25)
        self.assertAlmostEqual(self.ft.lift(0.5, 0.25), 2.0)
        self.assertIsNone(self.ft.lift(0.5, 0.0))

    def test_precision_at_threshold(self):
        alerts, precision = self.ft.precision_at_threshold([0.1, 0.5, 0.9], [0, 1, 1], 0.5)
        self.assertEqual(alerts, 2)
        self.assertAlmostEqual(precision, 1.0)

    def test_threshold_for_alert_volume_matches_volume(self):
        cut = self.ft.threshold_for_alert_volume([0.1, 0.4, 0.6, 0.9], 2)
        self.assertAlmostEqual(cut, 0.6)

    def test_auc_both_classes(self):
        self.assertEqual(self.ft.roc_auc([0.0, 1.0], [0, 1]), 1.0)
        self.assertIsNone(self.ft.roc_auc([1.0, 1.0], [1, 1]))

    def test_grouped_split_never_shares_a_token(self):
        rows = [{"chain": "base", "token_address": f"0x{i}", "ts_epoch": i} for i in range(20)]
        train, test = self.ft.grouped_split(rows)
        tr = {(r["chain"], r["token_address"]) for r in train}
        te = {(r["chain"], r["token_address"]) for r in test}
        self.assertEqual(tr & te, set(), "a token must not appear on both sides")

    def test_time_split_is_ordered(self):
        rows = [{"ts_epoch": i} for i in range(10)]
        train, test = self.ft.time_split(rows)
        self.assertLessEqual(max(r["ts_epoch"] for r in train),
                             min(r["ts_epoch"] for r in test))

    def test_gate_refuses_a_two_token_sample(self):
        """The exact failure this exists to prevent."""
        rows = [{"chain": "base", "token_address": "0xA"}, {"chain": "base", "token_address": "0xB"}]
        reasons = self.ft.sample_gate(rows, [1, 1], min_positives=30, min_tokens=30)
        self.assertTrue(reasons)
        self.assertTrue(any("distinct tokens" in r for r in reasons))

    def test_gate_allows_a_sufficient_sample(self):
        rows = [{"chain": "base", "token_address": f"0x{i}"} for i in range(40)]
        labels = [1] * 35 + [0] * 5
        self.assertEqual(self.ft.sample_gate(rows, labels, min_positives=30, min_tokens=30), [])

    def test_selection_report_states_coverage_limits(self):
        text = " ".join(self.ft.selection_report(
            [{"chain": "base", "token_address": "0xA"}]))
        self.assertIn("not every pool", text)
        self.assertIn("not", text.lower())
