#!/usr/bin/env python3
"""
label_outcomes.py — turn the bot's logged feature rows into supervised labels.

The bot writes one `features` row per token evaluation (every scan) when
`LOG_FEATURES=true`. Because the same token is re-evaluated across scans, that
table doubles as a price time-series, so this script can label every row with
the **forward maximum price multiple** it reached within 1h / 6h / 24h.

It then writes convenience binary labels (`hit_2x_1h`, `hit_3x_24h`, ...) and can
export a CSV for scikit-learn linear/logistic regression.

Usage
-----
    python label_outcomes.py                       # label using logged rows only
    python label_outcomes.py --fetch-current       # also use live prices for the
                                                   # newest rows (needs network)
    python label_outcomes.py --export training.csv # dump labeled rows as CSV
    python label_outcomes.py --horizons 1,6,24     # explicit horizons (hours)

Typical sklearn flow (after --export):
    import pandas as pd
    from sklearn.linear_model import LogisticRegression
    df = pd.read_csv("training.csv")
    feat = ["liquidity_usd", "vol_liq_ratio", "buy_ratio_5m", "top10", ...]
    X = df[feat].fillna(0)
    y = df["hit_2x_24h"].fillna(0).astype(int)
    model = LogisticRegression(max_iter=1000).fit(X, y)
"""

import argparse
import csv
import json
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

DEFAULT_HORIZONS = [1, 6, 24]
TEXT_COLUMNS = {"labeled_at"}
INT_COLUMNS = {
    "hit_2x_1h", "hit_2x_6h", "hit_2x_24h", "hit_3x_1h", "hit_3x_6h",
    "hit_3x_24h", "hit_5x_1h", "hit_5x_6h", "hit_5x_24h",
}
FLOAT_COLUMNS = {"max_mult_1h", "max_mult_6h", "max_mult_24h"}


def horizon_columns(hours):
    """Outcome columns implied by the requested horizons."""
    max_cols = {f"max_mult_{h}h": h * 3600 for h in hours}
    hit_cols = {}
    for mult in (2, 3, 5):
        for h in hours:
            hit_cols[f"hit_{mult}x_{h}h"] = (h * 3600, mult)
    return max_cols, hit_cols


def _column_type(name):
    if name in TEXT_COLUMNS or name == "labeled_at":
        return "TEXT"
    if name in INT_COLUMNS or name.startswith("hit_"):
        return "INTEGER"
    return "REAL"


def ensure_columns(conn, max_cols, hit_cols):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(features)")}
    added = 0
    for name in list(max_cols) + list(hit_cols) + ["labeled_at"]:
        if name not in existing:
            conn.execute(f"ALTER TABLE features ADD COLUMN {name} {_column_type(name)}")
            added += 1
    conn.commit()
    return added


def fetch_current_price(chain, token_address, timeout=10):
    """Best-effort live price (USD) for a token via DexScreener."""
    url = f"https://api.dexscreener.com/tokens/v1/{chain}/{token_address}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper-bot-labeler/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list) or not data:
            return None
        best = max(data, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        price = float(best.get("priceUsd") or 0)
        return price if price > 0 else None
    except Exception:
        return None


def label(conn, horizons, fetch_current=False, verbose=True, min_coverage=0.0,
          stats=None):
    """Write forward-multiple labels for every row.

    ``min_coverage`` (0.0 = off, the historical behaviour) refuses to label a
    row whose forward window was not observed to at least that fraction of its
    length. It matters because the price series comes from the bot's own scan
    rows and the bot STOPS logging a token when it leaves the feeds: measured
    span per token is ~1.5h, so a 24h window is usually truncated. Labelling a
    truncated window computes the max from whatever happened to be observed and
    calls every later runner a dud.

    ``stats`` (optional dict) is filled with row/window counters so a caller can
    warn about truncation. Passing ``min_coverage=0`` keeps the old behaviour
    and only *reports* the problem.
    """
    max_cols, hit_cols = horizon_columns(horizons)
    added = ensure_columns(conn, max_cols, hit_cols)
    if verbose:
        print(f"Schema: added {added} outcome column(s)")

    rows = conn.execute(
        "SELECT id, chain, token_address, ts_epoch, price_usd FROM features "
        "WHERE price_usd IS NOT NULL AND price_usd > 0 ORDER BY chain, token_address, ts_epoch"
    ).fetchall()

    groups = defaultdict(list)
    for rid, chain, token, ts, price in rows:
        groups[(chain, token)].append((ts, price, rid))

    if verbose:
        print(f"Labeling {len(rows)} rows across {len(groups)} tokens "
              f"(horizons: {', '.join(f'{h}h' for h in horizons)})")

    now = time.time()
    labeled = 0
    windows = 0
    truncated = 0
    live_cache = {}
    for (chain, token), items in groups.items():
        items.sort()
        timestamps = [it[0] for it in items]
        prices = [it[1] for it in items]
        live_price = None
        if fetch_current and (now - timestamps[-1]) > 60:
            # Only needed when the newest logged row is already stale.
            live_price = live_cache.get((chain, token))
            if live_price is None:
                live_price = fetch_current_price(chain, token)
                live_cache[(chain, token)] = live_price
                time.sleep(0.25)  # be polite to DexScreener

        for idx, (ts, price, rid) in enumerate(items):
            updates = {}
            for col, span in max_cols.items():
                best = None
                last_seen = None
                for j in range(idx + 1, len(items)):
                    if timestamps[j] > ts + span:
                        break
                    last_seen = timestamps[j]
                    if prices[j] > (best or 0):
                        best = prices[j]
                if best is None and live_price and (now - ts) <= span:
                    best = live_price
                    last_seen = now
                if best is None:
                    updates[col] = None
                    continue
                windows += 1
                # How much of the window was actually observed? An open window
                # (still running) is legitimately partial, so only count a
                # finished-but-short window as truncated.
                coverage = min(1.0, max(0.0, (last_seen - ts) / span))
                finished = (now - ts) > span
                if finished and coverage < 0.9:
                    truncated += 1
                if min_coverage > 0 and coverage < min_coverage:
                    updates[col] = None
                else:
                    updates[col] = best / price
            for col, (span, mult) in hit_cols.items():
                base_col = f"max_mult_{span // 3600}h"
                value = updates.get(base_col)
                updates[col] = None if value is None else int(value >= mult)
            if all(updates.get(c) is None for c in max_cols):
                continue
            set_clause = ", ".join(f"{c}=?" for c in updates)
            conn.execute(
                f"UPDATE features SET {set_clause}, labeled_at=? WHERE id=?",
                list(updates.values()) + [datetime.now(timezone.utc).isoformat(timespec="seconds"), rid],
            )
            labeled += 1
        conn.commit()

    if stats is not None:
        stats.update({"rows": len(rows), "tokens": len(groups), "labeled": labeled,
                      "windows": windows, "truncated": truncated})
    if verbose:
        print(f"Labeled {labeled} rows")
    return labeled


def export_csv(conn, path):
    cur = conn.execute("SELECT * FROM features ORDER BY ts_epoch")
    columns = [d[0] for d in cur.description]
    n = 0
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in cur:
            writer.writerow(row)
            n += 1
    print(f"Exported {n} rows -> {path}")
    return n


def summarize(conn, horizons):
    print("\nForward-multiple summary (rows with a label):")
    for h in horizons:
        col = f"max_mult_{h}h"
        if col not in {r[1] for r in conn.execute("PRAGMA table_info(features)")}:
            continue
        row = conn.execute(
            f"SELECT COUNT(*), AVG({col}), MAX({col}), "
            f"SUM(CASE WHEN {col} >= 2 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {col} >= 3 THEN 1 ELSE 0 END) FROM features WHERE {col} IS NOT NULL"
        ).fetchone()
        if row and row[0]:
            print(f"  {col:14} n={row[0]:<7} avg={row[1]:.3f} max={row[2]:.2f} "
                  f"2x={row[3]} 3x={row[4]}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="pump_bot_v5.db", help="SQLite DB written by the bot")
    parser.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS),
                        help="comma-separated horizons in hours (default 1,6,24)")
    parser.add_argument("--fetch-current", action="store_true",
                        help="use live DexScreener prices to label the newest rows")
    parser.add_argument("--min-coverage", type=float, default=0.0,
                        help="refuse to label a row whose forward window was "
                             "observed for less than this fraction of its length "
                             "(0.0 = label anyway, the historical behaviour). The "
                             "bot stops logging a token when it leaves the feeds, "
                             "so long horizons are usually truncated — 0.9 is the "
                             "honest setting for training data.")
    parser.add_argument("--export", metavar="CSV", help="export all feature rows to CSV")
    parser.add_argument("--no-summary", action="store_true")
    args = parser.parse_args(argv)

    try:
        horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    except ValueError:
        parser.error("--horizons must be comma-separated integers, e.g. 1,6,24")

    try:
        conn = sqlite3.connect(args.db)
    except sqlite3.Error as e:
        print(f"Cannot open {args.db}: {e}", file=sys.stderr)
        return 1

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "features" not in tables:
        print("No 'features' table found. Run the bot with LOG_FEATURES=true first.", file=sys.stderr)
        return 1

    stats = {}
    label(conn, horizons, fetch_current=args.fetch_current,
          min_coverage=args.min_coverage, stats=stats)
    # Truncation is the failure mode that silently poisons a training set, so it
    # is reported even when min_coverage is off.
    if stats.get("windows") and stats["truncated"]:
        pct = 100.0 * stats["truncated"] / stats["windows"]
        print(f"\n! {stats['truncated']:,} of {stats['windows']:,} windows "
              f"({pct:.0f}%) were TRUNCATED: the bot stopped scanning the token "
              f"before the horizon elapsed, so their max is computed from a short "
              f"slice of time.")
        print("  Those rows understate the runner rate. Re-run with "
              "--min-coverage 0.9 to leave them unlabeled, use a shorter "
              "--horizons, or label from OHLCV instead (colab_runner_analysis.py).")
    if not args.no_summary:
        summarize(conn, horizons)
    if args.export:
        export_csv(conn, args.export)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
