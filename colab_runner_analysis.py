#!/usr/bin/env python3
"""
colab_runner_analysis.py — find what runners have in common, and how to reweight.

Paste this whole file into ONE Google Colab cell (or upload it and run
`!python colab_runner_analysis.py`). It needs no API keys except the optional
cabal section (Etherscan key) and the optional holder-replay section (RPC URL).

WHAT COUNTS AS A RUNNER HERE
────────────────────────────
Your definition, made concrete:

  1. Pumps >2x at any point after the decision row  -> `max_mult_24h >= 2`
  2. Pumps hard then dumps to zero                  -> same label (the max is
     taken before the dump, so a pump-and-rug is a POSITIVE, as you want)
  3. What runners share, tokens AND wallets

(1) and (2) are the same ENTRY label. The difference between riding a wave and
holding a winner is an EXIT question, not an entry one — section 6 measures it
(time-to-peak, give-back) so you can tune trailing stops instead of the score.

WHY THIS DOES NOT JUST USE label_outcomes.py
────────────────────────────────────────────
The repo's labeler builds its forward price series ONLY from logged feature rows
(bot.py writes one row per scan). Two consequences that would poison a model:

  * If the bot stops evaluating a token (it leaves the feeds, or the gate
    rejects it later), the series simply ends. The 24h max is then computed
    from however many minutes of data exist — a token that 5x'd six hours later
    is labeled a DUD. That is a false-negative machine for exactly the runner
    class you care about.
  * A row whose entire window was never observed gets `None` (dropped), which is
    honest, but rows with 10 minutes of coverage are treated the same as rows
    with 24 hours.

So this script labels from GeckoTerminal 5-minute OHLCV candles (the same
source `diag/component_audit.py` uses), keyed by the `pair_address` already in
every feature row, and only falls back to the scan series. It also applies a
COVERAGE GATE: a row is labeled only if its forward window is actually covered,
and is otherwise left NaN and excluded — no silent false negatives.

Usage
─────
    # locally
    python colab_runner_analysis.py --csv 'features_*.csv' --outdir results

    # colab (after uploading your CSVs)
    !python colab_runner_analysis.py --csv '/content/features_*.csv'

Outputs (in --outdir): tokens_labeled.csv, univariate.csv,
model_comparison.csv, coefficients.csv, threshold_picker.csv,
creator_clusters.csv, and a printed report.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── config ───────────────────────────────────────────────────────────────────
HORIZON_H = 24.0            # label window (hours)
RUNNER_MULT = 2.0           # "runner" = reached this multiple within the window
MIN_WINDOW_COVERAGE = 0.90  # forward window must be >= this fraction observed
GT_OHLCV_AGGREGATE = 5      # minutes per candle
GT_SLEEP_S = 2.0            # polite delay between GeckoTerminal calls
GT_NETWORK = {"ethereum": "eth", "bsc": "bsc", "base": "base", "robinhood": "robinhood"}

# Features that are outcomes, identifiers, or leak the hand score. Never model these.
LEAKY = {
    "id", "ts_utc", "ts_epoch", "labeled_at", "chain", "token_address", "pair_address",
    "symbol", "source", "reject_reasons", "signal_notes", "holder_source",
    "security_source", "venue_requirement", "dex_id",
    "max_mult_1h", "max_mult_6h", "max_mult_24h",
    "hit_2x_1h", "hit_2x_6h", "hit_2x_24h", "hit_3x_1h", "hit_3x_6h", "hit_3x_24h",
    "hit_5x_1h", "hit_5x_6h", "hit_5x_24h",
    # alert_sent is the bot's own decision (a consequence of the gate), and the
    # score components sum to hand_score — including both sides double counts.
    "alert_sent", "passed_threshold", "rejected", "phase1_pass",
    "hand_score", "base_score",
    "score_vol_liq", "score_vol_5m_1h", "score_vol_1h_6h", "score_vol_6h_24h",
    "score_buy_5m", "score_buy_1h", "score_price", "score_holder",
    "score_security", "score_cex", "penalties_total",
    "price_usd", "price_native",   # levels, not shape; market_cap covers size
    # Derived by this script: they ARE the label (or leak the label window),
    # so they must never enter the feature matrix.
    "runner", "max_mult", "window_coverage", "label_source",
    "first_sight_ts",   # absolute timestamp, not a shape feature
}


def log(msg: str) -> None:
    print(msg, flush=True)


# ═════════════════════════════════════════════════════════════════════════════
# 0. LOAD
# ═════════════════════════════════════════════════════════════════════════════

def load_exports(pattern: str) -> pd.DataFrame:
    """Load one or many /export chunks and collapse repeated headers.

    The bot chunks at EXPORT_CHUNK_ROWS, so each file carries its own header.
    Files are also written newest-row-first; everything downstream sorts by
    ts_epoch, so that does not matter here.
    """
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise SystemExit(f"no files matched {pattern!r}")
    frames = []
    for p in paths:
        df = pd.read_csv(p, low_memory=False)
        df = df[df.get("ts_epoch").notna()] if "ts_epoch" in df.columns else df
        frames.append(df)
        log(f"  loaded {len(df):>7,} rows  {os.path.basename(p)}")
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=[c for c in ("id", "ts_epoch", "chain",
                                                "token_address") if c in df.columns])
    for col in ("ts_epoch", "price_usd", "market_cap_usd", "liquidity_usd"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["ts_epoch"].notna()].sort_values(["chain", "token_address", "ts_epoch"])
    log(f"  total {len(df):,} rows, {df.groupby(['chain','token_address']).ngroups:,} tokens")
    return df.reset_index(drop=True)


# ═════════════════════════════════════════════════════════════════════════════
# 1. LABEL (GeckoTerminal OHLCV, coverage-gated)
# ═════════════════════════════════════════════════════════════════════════════

def _get_json(url: str, timeout: int = 25) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "runner-analysis/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(1 + attempt)
    return None


def fetch_ohlcv(chain: str, pool: str, cache_dir: str) -> Optional[pd.DataFrame]:
    """5-minute OHLCV for one pool, cached on disk. Returns ts/close/high."""
    net = GT_NETWORK.get(chain)
    if not net or not isinstance(pool, str) or not pool:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"ohlcv_{net}_{pool.lower()}.json")
    data = None
    if os.path.exists(cache):
        try:
            with open(cache) as fh:
                data = json.load(fh)
        except Exception:
            data = None
    if data is None:
        url = (f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool}"
               f"/ohlcv/minute?aggregate={GT_OHLCV_AGGREGATE}&limit=1000&currency=usd&token=base")
        data = _get_json(url)
        time.sleep(GT_SLEEP_S)  # ~30 calls/min is the free ceiling
        try:
            with open(cache, "w") as fh:
                json.dump(data or {}, fh)
        except Exception:
            pass
    rows = ((data or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.astype({"ts": "int64", "high": "float64", "close": "float64"}).sort_values("ts")


def label_rows(df: pd.DataFrame, cache_dir: str, use_gt: bool = True) -> pd.DataFrame:
    """Forward max multiple per row, coverage-gated, GT OHLCV first.

    For each row at time t with price p:
        max_mult = max(high in (t, t+HORIZON]) / p
    A row is labeled only when the window is >= MIN_WINDOW_COVERAGE observed.
    """
    span = int(HORIZON_H * 3600)
    now = time.time()
    # Keyed by row index, NOT appended in group order: this function ends with a
    # re-sort, so a positional list would silently misalign labels whenever the
    # group iteration order differs from the sort order.
    max_by_idx: Dict[int, float] = {}
    cov_by_idx: Dict[int, float] = {}
    src_by_idx: Dict[int, str] = {}

    for (chain, token), g in df.groupby(["chain", "token_address"], sort=False):
        g = g.sort_values("ts_epoch")
        pool = g["pair_address"].dropna().iloc[-1] if "pair_address" in g else None
        candles = fetch_ohlcv(chain, pool, cache_dir) if (use_gt and pool) else None
        if candles is not None and len(candles):
            c_ts = candles["ts"].to_numpy()
            c_hi = candles["high"].to_numpy()
        else:
            c_ts = c_hi = None
        s_ts = g["ts_epoch"].to_numpy()
        s_px = pd.to_numeric(g["price_usd"], errors="coerce").to_numpy()

        for idx, row in g.iterrows():
            t = float(row["ts_epoch"])
            p = float(row["price_usd"]) if pd.notna(row.get("price_usd")) else 0.0
            hi, ts, src = None, None, "none"
            if c_hi is not None:
                sel = (c_ts > t) & (c_ts <= t + span)
                if sel.any():
                    hi, ts, src = c_hi[sel], c_ts[sel], "gt_ohlcv"
            if hi is None:  # fall back to the scan series
                sel = (s_ts > t) & (s_ts <= t + span)
                if sel.any():
                    hi, ts, src = s_px[sel], s_ts[sel], "scan"
            if hi is None or not len(hi) or p <= 0:
                max_by_idx[idx] = np.nan
                cov_by_idx[idx] = 0.0
                src_by_idx[idx] = "none"
                continue
            last = float(np.max(ts))
            coverage = min(1.0, max(0.0, (last - t) / span))
            # A window that is still open in the future (censored) or that ends
            # early (the bot stopped evaluating the token) must NOT be labeled:
            # treating a 30-minute series as a 24h outcome labels future runners
            # as duds.
            if last < now and coverage < MIN_WINDOW_COVERAGE:
                max_by_idx[idx] = np.nan
                cov_by_idx[idx] = coverage
                src_by_idx[idx] = "short:" + src
                continue
            valid = hi[np.isfinite(hi)]
            max_by_idx[idx] = float(np.max(valid) / p) if len(valid) else np.nan
            cov_by_idx[idx] = coverage
            src_by_idx[idx] = src

    df = df.copy()
    df["max_mult"] = pd.Series(max_by_idx)
    df["window_coverage"] = pd.Series(cov_by_idx)
    df["label_source"] = pd.Series(src_by_idx).fillna("none")
    df["runner"] = (df["max_mult"] >= RUNNER_MULT).astype("float")
    df.loc[df["max_mult"].isna(), "runner"] = np.nan
    return df


# ═════════════════════════════════════════════════════════════════════════════
# 2. UNIVARIATE: "what do runners have in common"
# ═════════════════════════════════════════════════════════════════════════════

def univariate_table(d: pd.DataFrame, feats: List[str]) -> pd.DataFrame:
    """Median runner vs median dud + effect size + Mann-Whitney p.

    Reported BEFORE any model on purpose: with a few dozen positives, multivariate
    coefficients are unstable, while a per-feature median split is honest and is
    what you will actually act on when editing weights.
    """
    from scipy.stats import mannwhitneyu
    rows = []
    r = d[d["runner"] == 1]
    n = d[d["runner"] == 0]
    for f in feats:
        a = pd.to_numeric(r[f], errors="coerce").dropna()
        b = pd.to_numeric(n[f], errors="coerce").dropna()
        if len(a) < 5 or len(b) < 5:
            continue
        med_a, med_b = float(a.median()), float(b.median())
        sd = float(pd.concat([a, b]).std()) or 1.0
        try:
            p = float(mannwhitneyu(a, b, alternative="two-sided").pvalue)
        except Exception:
            p = np.nan
        rows.append({
            "feature": f, "median_runners": med_a, "median_duds": med_b,
            "diff_over_sd": (med_a - med_b) / sd,
            "ratio": (med_a / med_b) if med_b else np.nan,
            "p_value": p, "n_runners": len(a), "n_duds": len(b),
        })
    t = pd.DataFrame(rows)
    if len(t):
        t["abs_effect"] = t["diff_over_sd"].abs()
        t = t.sort_values("abs_effect", ascending=False).drop(columns="abs_effect")
    return t


# ═════════════════════════════════════════════════════════════════════════════
# 3. MODELS: hand_score baseline vs logistic vs gradient boosting
# ═════════════════════════════════════════════════════════════════════════════

def precision_at_k(y: np.ndarray, scores: np.ndarray, frac: float) -> float:
    k = max(1, int(len(scores) * frac))
    idx = np.argsort(scores)[::-1][:k]
    return float(np.mean(y[idx]))


def evaluate_models(d: pd.DataFrame, feats: List[str]) -> Tuple[pd.DataFrame, pd.Series]:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, roc_auc_score
    from sklearn.model_selection import GroupShuffleSplit
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X = d[feats].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    y = d["runner"].astype(int).to_numpy()
    groups = (d["chain"] + ":" + d["token_address"]).to_numpy()

    # hand_score is the incumbent ranker, kept as its own column for the baseline
    hand = pd.to_numeric(d.get("hand_score", pd.Series(0, index=d.index)),
                         errors="coerce").fillna(0).to_numpy()

    rows, coef_series = [], None
    gss = GroupShuffleSplit(n_splits=5, test_size=0.25, random_state=0)
    for fold, (tr, te) in enumerate(gss.split(X, y, groups=groups)):
        if len(np.unique(y[tr])) < 2 or len(np.unique(y[te])) < 2:
            continue
        models = {
            "hand_score": None,
            "logistic": make_pipeline(
                StandardScaler(),
                LogisticRegression(max_iter=3000, class_weight="balanced", C=1.0)),
            "grad_boost": HistGradientBoostingClassifier(
                max_iter=300, learning_rate=0.05, max_depth=3, random_state=0),
        }
        for name, model in models.items():
            if model is None:
                scores = hand[te]
            else:
                model.fit(X.iloc[tr], y[tr])
                scores = model.predict_proba(X.iloc[te])[:, 1]
            rows.append({
                "fold": fold, "model": name,
                "auc": roc_auc_score(y[te], scores),
                "pr_auc": average_precision_score(y[te], scores),
                "precision@1%": precision_at_k(y[te], scores, 0.01),
                "precision@5%": precision_at_k(y[te], scores, 0.05),
                "precision@10%": precision_at_k(y[te], scores, 0.10),
                "base_rate": float(np.mean(y[te])),
            })
    comp = pd.DataFrame(rows)
    summary = (comp.groupby("model")[["auc", "pr_auc", "precision@1%",
                                      "precision@5%", "precision@10%"]]
               .mean().sort_values("auc", ascending=False))

    # full-data logistic for interpretable coefficients
    try:
        pipe = make_pipeline(StandardScaler(),
                             LogisticRegression(max_iter=3000, class_weight="balanced"))
        pipe.fit(X, y)
        coef_series = pd.Series(pipe.named_steps["logisticregression"].coef_[0],
                                index=feats).sort_values(ascending=False)
    except Exception as e:
        log(f"  coefficient fit failed: {e}")
    return comp, (coef_series if coef_series is not None else pd.Series(dtype=float))


def threshold_picker(d: pd.DataFrame, feats: List[str]) -> pd.DataFrame:
    """For a range of alert volumes, what precision does the model buy you?

    This is the table that actually answers 'what should MIN_SCORE be': pick the
    row whose precision you can live with, then read the score cutoff.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupShuffleSplit
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X = d[feats].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    y = d["runner"].astype(int).to_numpy()
    groups = (d["chain"] + ":" + d["token_address"]).to_numpy()
    tr, te = next(GroupShuffleSplit(n_splits=1, test_size=0.25,
                                    random_state=0).split(X, y, groups=groups))
    if len(np.unique(y[tr])) < 2:
        return pd.DataFrame()
    pipe = make_pipeline(StandardScaler(),
                         LogisticRegression(max_iter=3000, class_weight="balanced"))
    pipe.fit(X.iloc[tr], y[tr])
    s = pipe.predict_proba(X.iloc[te])[:, 1]
    yte = y[te]
    rows = []
    for vol in (0.01, 0.02, 0.05, 0.10, 0.20, 0.50):
        k = max(1, int(len(s) * vol))
        idx = np.argsort(s)[::-1][:k]
        rows.append({
            "alert_top_frac": vol, "rows_alerted": k,
            "precision": float(np.mean(yte[idx])),
            "runners_caught": int(np.sum(yte[idx])),
            "runners_total": int(np.sum(yte)),
            "model_score_cutoff": float(s[idx][-1]),
        })
    return pd.DataFrame(rows)


# ═════════════════════════════════════════════════════════════════════════════
# 4. CABAL: creator / factory clustering (Etherscan v2, free, works on 4663)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_creators(tokens: List[Tuple[str, str]], api_key: str,
                   cache_path: str) -> pd.DataFrame:
    """contractCreator + contractFactory per token via Etherscan v2.

    Verified to work on Robinhood Chain (chainid 4663) with a normal Etherscan
    key: returns contractCreator, contractFactory and the creation timestamp.
    That pair is the cheapest, strongest cabal signal available — the same
    deployer or the same launchpad factory behind many runners.
    """
    cache: Dict[str, dict] = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}
    rows = []
    for chain, token in tokens:
        cid = {"ethereum": "1", "bsc": "56", "base": "8453", "robinhood": "4663"}.get(chain)
        if not cid:
            continue
        ck = f"{cid}:{token.lower()}"
        if ck not in cache:
            url = ("https://api.etherscan.io/v2/api?"
                   + urllib.parse.urlencode({
                       "chainid": cid, "module": "contract",
                       "action": "getcontractcreation",
                       "contractaddresses": token, "apikey": api_key}))
            data = _get_json(url) or {}
            res = (data.get("result") or [{}])
            cache[ck] = res[0] if isinstance(res, list) and res else {}
            time.sleep(0.3)  # free tier is 5 req/s
        info = cache.get(ck) or {}
        rows.append({
            "chain": chain, "token_address": token,
            "creator": (info.get("contractCreator") or "").lower() or None,
            "factory": (info.get("contractFactory") or "").lower() or None,
            "created_at": info.get("timestamp"),
        })
    try:
        with open(cache_path, "w") as fh:
            json.dump(cache, fh)
    except Exception:
        pass
    return pd.DataFrame(rows)


def cluster_creators(tokens_df: pd.DataFrame, links: pd.DataFrame,
                     min_tokens: int = 2) -> pd.DataFrame:
    """Which creators/factories are behind multiple tokens, and how they ran."""
    m = tokens_df.merge(links, on=["chain", "token_address"], how="left")
    out = []
    for col in ("creator", "factory"):
        for key, g in m.dropna(subset=[col]).groupby(col):
            if len(g) < min_tokens:
                continue
            out.append({
                "cluster_by": col, "address": key, "tokens": len(g),
                "runners": int(g["is_runner"].sum()),
                "runner_rate": float(g["is_runner"].mean()),
                "symbols": ", ".join(g["symbol"].astype(str).head(8)),
            })
    t = pd.DataFrame(out)
    return t.sort_values(["runner_rate", "tokens"], ascending=False) if len(t) else t


# ═════════════════════════════════════════════════════════════════════════════
# 5. EXIT SHAPE (your definition #2: pumped then dumped)
# ═════════════════════════════════════════════════════════════════════════════

def exit_shape(all_rows: pd.DataFrame) -> pd.DataFrame:
    """Time-to-peak and how much of the peak was kept at window end.

    Peak-only labeling makes pump-and-rug a positive (correct for entry). This
    table is what tells you whether that positive wanted a fast trailing stop
    ('wave') or a held position ('holder').

    Takes the FULL scan history, deliberately: the labeled subset is biased to
    early rows (only rows whose forward window is fully observed can be labeled),
    so measuring the dump on it would report every runner as "held" — the dump
    happens after the labeled rows end.
    """
    rows = []
    for (chain, token), g in all_rows.groupby(["chain", "token_address"], sort=False):
        g = g.sort_values("ts_epoch")
        px = pd.to_numeric(g["price_usd"], errors="coerce").dropna()
        if len(px) < 3:
            continue
        base = float(px.iloc[0])
        if base <= 0:
            continue
        peak = float(px.max())
        end = float(px.iloc[-1])
        rows.append({
            "chain": chain, "token_address": token,
            "symbol": g["symbol"].iloc[-1] if "symbol" in g else None,
            "scans": len(g),
            "peak_mult": peak / base,
            "end_mult": end / base,
            "peak_kept": (end / peak) if peak else np.nan,
            "is_runner": bool(peak / base >= RUNNER_MULT),
        })
    t = pd.DataFrame(rows)
    if len(t):
        t["gave_back"] = (t["peak_mult"] >= RUNNER_MULT) & (t["peak_kept"] < 0.5)
    return t


# ═════════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="features_*.csv", help="glob of /export files")
    ap.add_argument("--outdir", default="runner_results")
    ap.add_argument("--no-gt", action="store_true",
                    help="skip GeckoTerminal OHLCV and label from the scan series only")
    ap.add_argument("--etherscan-key", default=os.getenv("ETHERSCAN_API_KEY", ""),
                    help="for the cabal section (contractCreator/contractFactory)")
    ap.add_argument("--token-level", action="store_true",
                    help="model tokens (best row per token) instead of every scan row")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    cache_dir = os.path.join(args.outdir, ".cache")

    log("── 0. loading exports " + "─" * 50)
    df = load_exports(args.csv)

    log("── 1. labeling (GT OHLCV, coverage-gated) " + "─" * 28)
    df = label_rows(df, cache_dir, use_gt=not args.no_gt)
    have = df["max_mult"].notna()
    log(f"  labeled {have.sum():,} / {len(df):,} rows "
        f"({100 * have.mean():.1f}%)")
    if len(df):
        log("  label source:\n" + df["label_source"].value_counts().to_string())
    d = df[have].copy()
    if d.empty:
        log("  no labeled rows — widen the export or enable OHLCV; stopping")
        return 1
    log(f"  runner rate among labeled rows: {d['runner'].mean():.3f} "
        f"({int(d['runner'].sum()):,} positives)")
    if have.mean() < 0.5:
        log(f"  NOTE: only {100 * have.mean():.0f}% of rows are labelable at a "
            f"{HORIZON_H:.0f}h horizon — the rest are censored (window not yet "
            f"elapsed) or truncated (the bot stopped scanning that token).")
        log("  Lower HORIZON_H (e.g. 4 or 6) to label far more rows, at the cost "
            "of measuring a shorter pump window.")

    # Optional collapse to one row per token (best hand_score) — the entry
    # decision, rather than the per-scan series.
    if args.token_level:
        d = (d.sort_values("hand_score", ascending=False)
               .groupby(["chain", "token_address"], as_index=False).head(1))
        log(f"  token-level: {len(d):,} tokens, {int(d['runner'].sum()):,} runners")

    feats = [c for c in d.columns
             if c not in LEAKY and pd.api.types.is_numeric_dtype(d[c])]
    # drop columns that are constant or nearly all null
    feats = [c for c in feats if d[c].notna().mean() > 0.3 and d[c].nunique() > 1]
    log(f"  {len(feats)} candidate features")

    log("\n── 2. what runners have in common (univariate) " + "─" * 20)
    uni = univariate_table(d, feats)
    uni.to_csv(os.path.join(args.outdir, "univariate.csv"), index=False)
    if len(uni):
        log(uni.head(20).to_string(index=False))
        log("\n  read: diff_over_sd > 0 means runners are HIGHER on that feature.")
        log("  p_value is per-feature and unadjusted — treat it as a hint, not proof.")

    log("\n── 3. models: hand_score vs logistic vs grad_boost " + "─" * 17)
    comp, coef = evaluate_models(d, feats)
    comp.to_csv(os.path.join(args.outdir, "model_comparison.csv"), index=False)
    if len(comp):
        summary = (comp.groupby("model")[["auc", "pr_auc", "precision@1%",
                                          "precision@5%", "precision@10%"]].mean()
                   .sort_values("auc", ascending=False))
        log(summary.round(4).to_string())
        base = float(d["runner"].mean())
        log(f"  base rate = {base:.3f}; precision@k below this is worse than "
            f"alerting at random")
        log("  logistic/grad_boost must beat hand_score at the SAME k to be worth "
            "deploying")
    if len(coef):
        coef.to_csv(os.path.join(args.outdir, "coefficients.csv"))
        log("\n  standardized logistic coefficients (per +1 SD -> log-odds of a runner):")
        if len(coef) <= 25:
            log(coef.round(3).to_string())
        else:
            log(coef.round(3).head(15).to_string())
            log("  ...")
            log(coef.round(3).tail(10).to_string())

    log("\n── 4. threshold picker " + "─" * 44)
    tp = threshold_picker(d, feats)
    if len(tp):
        tp.to_csv(os.path.join(args.outdir, "threshold_picker.csv"), index=False)
        log(tp.round(4).to_string(index=False))

    log("\n── 5. exit shape (pump-then-dump vs hold) " + "─" * 25)
    ex = exit_shape(df)   # full scan history, not the labeled subset
    if len(ex):
        ex.to_csv(os.path.join(args.outdir, "exit_shape.csv"), index=False)
        r = ex[ex["is_runner"]]
        if len(r):
            log(f"  runners: {len(r)}, of which gave back >50% of peak: "
                f"{int(r['gave_back'].sum())} ({100 * r['gave_back'].mean():.0f}%)")
            log(f"  median peak kept at window end: {r['peak_kept'].median():.2f}")

    if args.etherscan_key:
        log("\n── 6. cabal: creator / factory clustering " + "─" * 27)
        tok = (d.sort_values("max_mult", ascending=False)
                 .groupby(["chain", "token_address"], as_index=False)
                 .agg(symbol=("symbol", "last"),
                      max_mult=("max_mult", "max"),
                      is_runner=("runner", "max")))
        links = fetch_creators(list(zip(tok["chain"], tok["token_address"])),
                               args.etherscan_key,
                               os.path.join(args.outdir, "creators_cache.json"))
        tok = tok.merge(links, on=["chain", "token_address"], how="left")
        tok.to_csv(os.path.join(args.outdir, "tokens_labeled.csv"), index=False)
        cl = cluster_creators(tok, links, min_tokens=2)
        if len(cl):
            cl.to_csv(os.path.join(args.outdir, "creator_clusters.csv"), index=False)
            log(cl.head(25).to_string(index=False))
            log("\n  read: a creator/factory with tokens>=2 and runner_rate=1.0 is a "
                "strong 'this deployer makes runners' signal.")
            log("  Caveat: this is in-sample. To use it live you must compute the "
                "creator's PAST runner rate only (walk-forward), not their total.")
        else:
            log(f"  no creator/factory behind >=2 tokens in this sample "
                f"({links['creator'].notna().sum()}/{len(links)} resolved)")
    else:
        log("\n── 6. cabal section skipped (pass --etherscan-key) " + "─" * 18)

    log("\n── done " + "─" * 60)
    log(f"artifacts in {args.outdir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
