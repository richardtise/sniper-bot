#!/usr/bin/env python3
"""Fit alert thresholds on outcomes — or refuse to, if the sample cannot support it.

What this is for
----------------
``diag/population_floors.py`` sets floors from a population quantile: objective
and outcome-blind, but it says nothing about whether those floors select tokens
that go up. This script is the other half — it fits thresholds against **realised
forward returns**, which is the only kind of threshold that can claim to predict
anything.

Why it refuses to run on thin data
----------------------------------
The previous thresholds were set by replaying two tokens already known to have
run. That is not a small-sample problem you can fix with a confidence interval;
it is a selection problem: the tokens were chosen *because* their outcome was
known, so the filter is fitted on the answer. This script is built to make that
failure mode impossible to repeat:

* it fits only on rows the bot logged **before** the outcome, from the
  ``features`` table (which records rejects too, so it is not a winners-only set);
* it **refuses to emit any recommendation** below ``--min-positives`` positive
  outcomes and ``--min-tokens`` distinct tokens, and says so loudly;
* it validates on a **time split** (train on earlier rows, test on later ones)
  *and* reports a **grouped-by-token** split, because rows of the same token are
  not independent;
* it reports the **base rate** next to every precision figure, so "80% of alerts
  were winners" cannot be quoted without the "and 60% of everything was" that
  makes it meaningless;
* it compares against the **same alert volume** as the incumbent hand score,
  which is the only honest way to say "better";
* it prints the **selection status** of the sample, including what share of the
  population was never logged at all.

Usage
-----
    python label_outcomes.py                       # build the labels first
    bot-env/bin/python diag/fit_thresholds.py      # then fit (or refuse)
    bot-env/bin/python diag/fit_thresholds.py --min-positives 30 --csv training.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from typing import Dict, List, Optional, Sequence, Tuple

# Feature columns that exist before the outcome is known. Anything derived from
# forward price (max_mult_*, hit_*) is a label, never an input.
CANDIDATE_FEATURES = [
    "liquidity_usd", "market_cap_usd", "vol_liq_ratio", "vol_5m_1h",
    "vol_1h_6h", "vol_6h_24h", "buy_ratio_5m", "buy_ratio_1h",
    "buyers_5m", "sellers_5m", "buys_5m", "sells_5m", "txns_5m_ratio_proxy",
    "chg_5m", "chg_1h", "chg_6h", "top10", "top50", "holder_count",
    "age_minutes", "signal_bonus", "signal_penalty",
]

# Default target: the outcome the bot exists to catch.
DEFAULT_LABEL = "hit_2x_24h"


# ─────────────────────────────────────────────────────────────────────────────
# pure statistics (dependency-free, unit-testable offline)
# ─────────────────────────────────────────────────────────────────────────────


def base_rate(labels: Sequence[int]) -> Optional[float]:
    if not labels:
        return None
    return sum(1 for v in labels if v) / len(labels)


def precision_at_threshold(
    scores: Sequence[float], labels: Sequence[int], threshold: float
) -> Tuple[int, float]:
    """(alerts, precision) if everything at or above ``threshold`` alerts."""
    picked = [y for s, y in zip(scores, labels) if s >= threshold]
    if not picked:
        return (0, 0.0)
    return (len(picked), sum(1 for y in picked if y) / len(picked))


def lift(precision: float, base: Optional[float]) -> Optional[float]:
    """Precision over base rate. The number that makes precision interpretable."""
    if not base:
        return None
    return precision / base


def threshold_for_alert_volume(
    scores: Sequence[float], volume: int
) -> Optional[float]:
    """The score cut that produces at most ``volume`` alerts (incumbent-matched).

    Comparing two rankers at different alert volumes is the standard way to make
    a weak model look strong; this is how the comparison is held fixed.
    """
    if volume <= 0 or not scores:
        return None
    ordered = sorted(scores, reverse=True)
    if volume >= len(ordered):
        return ordered[-1] if ordered else None
    return ordered[volume - 1]


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Rank quality, ties averaged. None when only one class is present."""
    pairs = [(s, y) for s, y in zip(scores, labels) if s is not None]
    pos = [s for s, y in pairs if y]
    neg = [s for s, y in pairs if not y]
    if not pos or not neg:
        return None
    # Mann-Whitney U with tie handling.
    combined = sorted(pairs, key=lambda p: p[0])
    ranks: Dict[int, float] = {}
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    rank_sum_pos = sum(ranks[idx] for idx, (s, y) in enumerate(combined) if y)
    n1, n0 = len(pos), len(neg)
    return (rank_sum_pos - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def time_split(
    rows: Sequence[dict], *, train_frac: float = 0.6
) -> Tuple[List[dict], List[dict]]:
    """Split by evaluation time so the test set is strictly later than train.

    A random row split leaks the future: the same token appears on both sides.
    """
    ordered = sorted(rows, key=lambda r: r.get("ts_epoch") or 0)
    cut = int(len(ordered) * train_frac)
    return ordered[:cut], ordered[cut:]


def grouped_split(
    rows: Sequence[dict], *, train_frac: float = 0.6, seed: int = 7
) -> Tuple[List[dict], List[dict]]:
    """Split by token, so no token is in both halves.

    Rows for one token are the same asset at different times: they are not
    independent observations, and splitting by row would let the model memorise
    a token and be scored on it.
    """
    by_token: Dict[Tuple[str, str], List[dict]] = {}
    for row in rows:
        key = (row.get("chain") or "", row.get("token_address") or "")
        by_token.setdefault(key, []).append(row)
    keys = sorted(by_token)
    rng = random.Random(seed)
    rng.shuffle(keys)
    cut = int(len(keys) * train_frac)
    train_keys, test_keys = set(keys[:cut]), set(keys[cut:])
    train = [r for k in train_keys for r in by_token[k]]
    test = [r for k in test_keys for r in by_token[k]]
    return train, test


def sample_gate(
    rows: Sequence[dict], labels: Sequence[int], *, min_positives: int, min_tokens: int
) -> List[str]:
    """Reasons the sample cannot support a fit. Empty list means it can."""
    reasons: List[str] = []
    positives = sum(1 for y in labels if y)
    tokens = {(r.get("chain") or "", r.get("token_address") or "") for r in rows}
    if positives < min_positives:
        reasons.append(
            f"only {positives} positive outcomes (need >= {min_positives}); "
            f"a threshold fitted on this would be memorising the sample"
        )
    if len(tokens) < min_tokens:
        reasons.append(
            f"only {len(tokens)} distinct tokens (need >= {min_tokens}); "
            f"a per-token split cannot be validated"
        )
    if not rows:
        reasons.append("no labelled rows at all")
    return reasons


def evaluate_feature(
    train: Sequence[dict],
    test: Sequence[dict],
    feature: str,
    *,
    label: str,
    quantiles: Sequence[float] = (0.5, 0.6, 0.7, 0.75, 0.8, 0.9),
) -> Optional[dict]:
    """Pick the feature's threshold on train, report it on test.

    Both a >= cut and (for features where low is good) a <= cut are tried; the
    better train-side lift wins, and only the test number is reported back.
    """
    def values(rows):
        return [r.get(feature) for r in rows]

    tr_x = [v for v in values(train)]
    tr_y = [int(r.get(label) or 0) for r in train]
    te_x = values(test)
    te_y = [int(r.get(label) or 0) for r in test]
    if not train or not test or all(v is None for v in tr_x):
        return None

    numeric = [(float(v), y) for v, y in zip(tr_x, tr_y) if v is not None]
    if len(numeric) < 5:
        return None

    base_train = base_rate([y for _, y in numeric])
    best = None
    for q in quantiles:
        for direction in (">=", "<="):
            cut = _quantile([v for v, _ in numeric], q)
            if cut is None:
                continue
            if direction == ">=":
                picked = [y for v, y in numeric if v >= cut]
            else:
                picked = [y for v, y in numeric if v <= cut]
            if not picked:
                continue
            p = sum(1 for y in picked if y) / len(picked)
            score = lift(p, base_train)
            if score is None:
                continue
            if best is None or score > best["train_lift"]:
                best = {"direction": direction, "cut": cut, "train_lift": score}

    if best is None:
        return None

    cut, direction = best["cut"], best["direction"]
    te_pairs = [(float(v), y) for v, y in zip(te_x, te_y) if v is not None]
    if direction == ">=":
        picked = [y for v, y in te_pairs if v >= cut]
    else:
        picked = [y for v, y in te_pairs if v <= cut]

    test_base = base_rate([y for _, y in te_pairs])
    alerts = len(picked)
    precision = (sum(1 for y in picked if y) / alerts) if alerts else 0.0
    return {
        "feature": feature,
        "direction": direction,
        "cut": cut,
        "test_alerts": alerts,
        "test_precision": precision,
        "test_base_rate": test_base,
        "test_lift": lift(precision, test_base),
    }


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = min(1.0, max(0.0, q)) * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


# ─────────────────────────────────────────────────────────────────────────────
# data
# ─────────────────────────────────────────────────────────────────────────────


def load_rows(db_path: str, label: str) -> List[dict]:
    """Labelled rows from the features table, with the outcome columns joined."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
        if "token_address" not in cols:
            return []
        if label not in cols:
            print(f"! {db_path} has no '{label}' column — run label_outcomes.py first",
                  file=sys.stderr)
            return []
        wanted = [c for c in (["ts_epoch", "chain", "token_address", "symbol", label]
                              + CANDIDATE_FEATURES) if c in cols]
        rows = [dict(r) for r in conn.execute(
            f"SELECT {','.join(wanted)} FROM features WHERE {label} IS NOT NULL")]
        return rows
    finally:
        conn.close()


def load_csv(path: str, label: str) -> List[dict]:
    with open(path, newline="") as fh:
        return [row for row in csv.DictReader(fh) if row.get(label) not in (None, "")]


def selection_report(rows: Sequence[dict]) -> List[str]:
    """State what the sample can and cannot represent.

    The features table only contains what discovery offered, so it cannot speak
    for pools the feeds never listed. Saying so prevents the table being read as
    a census of the chain.
    """
    tokens = {(r.get("chain"), r.get("token_address")) for r in rows}
    chains: Dict[str, int] = {}
    for row in rows:
        chains[str(row.get("chain"))] = chains.get(str(row.get("chain")), 0) + 1
    lines = [
        f"rows={len(rows)}  distinct tokens={len(tokens)}  chains={chains}",
        "coverage: only pools the discovery feeds offered and the scanner "
        "evaluated — not every pool created on these chains",
        "rejects are included (the table logs every evaluation), so this is not "
        "a winners-only sample",
    ]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="pump_bot_v5.db")
    parser.add_argument("--csv", default=None, help="use an exported CSV instead of the DB")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--min-positives", type=int, default=30,
                        help="refuse to fit below this many positive outcomes")
    parser.add_argument("--min-tokens", type=int, default=30,
                        help="refuse to fit below this many distinct tokens")
    parser.add_argument("--incumbent-threshold", type=float, default=None,
                        help="hand-score bar to compare against at equal volume")
    args = parser.parse_args()

    rows = load_csv(args.csv, args.label) if args.csv else load_rows(args.db, args.label)
    print("=" * 72)
    print("Sample")
    print("=" * 72)
    for line in selection_report(rows):
        print("  " + line)

    labels = [int(float(r.get(args.label) or 0)) for r in rows]
    base = base_rate(labels)
    print(f"  label={args.label}  base rate={base:.4f}" if base is not None
          else "  no labelled rows")

    reasons = sample_gate(rows, labels, min_positives=args.min_positives,
                          min_tokens=args.min_tokens)
    if reasons:
        print("\n" + "=" * 72)
        print("REFUSING TO FIT — the sample cannot support a threshold")
        print("=" * 72)
        for reason in reasons:
            print(f"  * {reason}")
        print("\nThis is the intended behaviour. The previous thresholds were set")
        print("from two known-runner tokens; that is a selection error, not a")
        print("small-sample one, and shipping another fitted-looking number would")
        print("repeat it with more decimal places.")
        print("\nWhat to do instead:")
        print("  1. set LOG_FEATURES=true and let the bot run; the features table")
        print("     logs every evaluation including rejects, which is the unbiased")
        print("     decision-time population;")
        print("  2. python label_outcomes.py --fetch-current   # attach outcomes")
        print(f"  3. re-run this script until there are >= {args.min_positives} "
              f"positives across >= {args.min_tokens} tokens;")
        print("  4. meanwhile use diag/population_floors.py, whose output is")
        print("     outcome-blind and carries its own sample size and CI.")
        return 2

    train_t, test_t = time_split(rows)
    train_g, test_g = grouped_split(rows)
    print(f"\ntime split:    train={len(train_t)} test={len(test_t)}")
    print(f"token split:   train={len(train_g)} test={len(test_g)}")

    print("\n" + "=" * 72)
    print(f"Per-feature thresholds (picked on train, reported on later/holdout)")
    print("=" * 72)
    print(f"{'feature':22s} {'dir':3s} {'cut':>12s} {'alerts':>7s} "
          f"{'precision':>10s} {'base':>8s} {'lift':>6s}")
    results = []
    for feature in CANDIDATE_FEATURES:
        res = evaluate_feature(train_t, test_t, feature, label=args.label)
        if not res or not res["test_alerts"]:
            continue
        results.append(res)
        print(f"{res['feature']:22s} {res['direction']:3s} {res['cut']:>12.4g} "
              f"{res['test_alerts']:>7d} {res['test_precision']:>10.3f} "
              f"{(res['test_base_rate'] or 0):>8.3f} "
              f"{(res['test_lift'] or 0):>6.2f}")

    if not results:
        print("  no feature produced a usable threshold on this sample")
        return 3

    print("\n" + "=" * 72)
    print("Rank quality (pooled, all rows)")
    print("=" * 72)
    for feature in ("hand_score", "signal_bonus"):
        scores = [r.get(feature) for r in rows]
        if any(s is not None for s in scores):
            auc = roc_auc(scores, labels)
            print(f"  {feature:22s} AUC={auc:.3f}" if auc is not None
                  else f"  {feature:22s} AUC=n/a (single class)")

    print("\nReminder: compare any candidate against the incumbent hand score at")
    print("the SAME alert volume, and split by token. A higher AUC at 10x the")
    print("alert count is not an improvement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
