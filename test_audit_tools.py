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
