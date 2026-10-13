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

    # optional: enrich labels from GeckoTerminal candles (SLOW, rate-limited)
    !python colab_runner_analysis.py --csv '/content/features_*.csv' --gt-ohlcv \
        --max-tokens 200

Speed: the default path is entirely local (no network) and finishes in seconds
even on a large export. `--gt-ohlcv` adds one rate-limited GeckoTerminal call per
token (~2.2s each, 30 calls/min ceiling), so it is capped by `--max-tokens` —
enriching 5,000 tokens would take about three hours.

Running this as a Colab `.py` file works too: it parses with `parse_known_args`,
so the kernel's own `-f .../kernel-*.json` argument does not abort it.

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
HORIZON_H = 24.0            # label window (hours); overridden by --horizon / auto
HORIZON_SWEEP = (0.5, 1, 2, 3, 6, 12, 24)   # candidates for the automatic pick
TARGET_LABELED = 0.30       # stop shortening once this share of rows labels
RUNNER_MULT = 2.0           # "runner" = reached this multiple within the window
MIN_WINDOW_COVERAGE = 0.90  # forward window must be >= this fraction observed
GT_OHLCV_AGGREGATE = 5      # minutes per candle
GT_SLEEP_S = 2.0            # polite delay between GeckoTerminal calls
GT_MAX_TOKENS_DEFAULT = 200  # cap on OHLCV enrichment (see --gt-ohlcv)
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


def fetch_ohlcv(chain: str, pool: str, cache_dir: str,
                cache_only: bool = False) -> Optional[pd.DataFrame]:
    """5-minute OHLCV for one pool, cached on disk. Returns ts/close/high.

    ``cache_only=True`` never touches the network: it returns the cached frame
    or None. Labeling uses that mode so the enrichment cap in
    :func:`prefetch_ohlcv` actually bounds the work — otherwise every token in
    the export triggers its own rate-limited call, which is what turned a large
    export into a multi-hour cell.
    """
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
        if cache_only:
            return None
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


def prefetch_ohlcv(df: pd.DataFrame, cache_dir: str, max_tokens: int,
                   sort_by: str = "scans") -> int:
    """Warm the OHLCV cache for the most-observed tokens, with progress + ETA.

    GeckoTerminal's free tier is ~30 calls/min, so this is a strictly serial,
    rate-limited loop: N tokens costs about 2.2*N seconds. Enriching every token
    in a long export is therefore an overnight job (5,000 tokens ~ 3 hours),
    which is why it is capped and opt-in. Cached pools are free on re-run.
    """
    have = (df.groupby(["chain", "token_address"])
              .agg(pool=("pair_address", "last"), scans=("ts_epoch", "size"))
              .reset_index())
    have = have[have["pool"].notna()]
    if not len(have):
        log("  no pair_address values — cannot enrich")
        return 0
    have = have.sort_values(sort_by, ascending=False).head(max_tokens)
    total = len(have)
    log(f"  OHLCV enrichment for {total} token(s) "
        f"(~{total * (GT_SLEEP_S + 0.2) / 60:.1f} min at 30 calls/min)")
    t0 = time.time()
    for i, (_, r) in enumerate(have.iterrows(), 1):
        fetch_ohlcv(r["chain"], r["pool"], cache_dir)
        if i % 25 == 0 or i == total:
            el = time.time() - t0
            log(f"    {i}/{total}  elapsed {el / 60:.1f}m  "
                f"eta {(el / i) * (total - i) / 60:.1f}m")
    return total


def label_rows(df: pd.DataFrame, cache_dir: str, use_gt: bool = False,
               horizon_h: Optional[float] = None,
               min_coverage: Optional[float] = None) -> pd.DataFrame:
    """Forward max multiple per row, coverage-gated, scan series by default.

    For each row at time t with price p:
        max_mult = max(high in (t, t+horizon]) / p
    A row is labeled only when the window is >= ``min_coverage`` observed.

    ``use_gt=False`` labels from the bot's own scan series: no network, instant,
    and still coverage-gated. ``use_gt=True`` prefers GeckoTerminal candles for
    the tokens already warmed by :func:`prefetch_ohlcv` and falls back to the
    scan series for the rest, so it never *loses* rows — it only improves the
    ones it covers.

    WHY THE COVERAGE GATE IS THE WHOLE BALLGAME
    -------------------------------------------
    The bot only logs a row while it is scanning a token, and it stops when the
    token leaves the feeds. Measured span per token is typically ~1.5h. So a
    6h/24h window is *never* observed for most rows, and labeling them anyway
    would call every future runner a dud. The horizon must fit the data, which
    is what :func:`horizon_sweep` decides.
    """
    span = int((horizon_h if horizon_h is not None else HORIZON_H) * 3600)
    min_cov = MIN_WINDOW_COVERAGE if min_coverage is None else min_coverage
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
        # cache_only: the network work happens once, in prefetch_ohlcv, bounded
        # by --max-tokens. Fetching here would ignore that cap entirely.
        candles = (fetch_ohlcv(chain, pool, cache_dir, cache_only=True)
                   if (use_gt and pool) else None)
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
            # searchsorted, not a boolean mask per row: masking the whole candle
            # array (and the group's own scan array) for every row made this
            # O(rows^2) per token, which dominated on large exports.
            if c_hi is not None:
                lo = int(np.searchsorted(c_ts, t, side="right"))
                hi_i = int(np.searchsorted(c_ts, t + span, side="right"))
                if hi_i > lo:
                    hi, ts, src = c_hi[lo:hi_i], c_ts[lo:hi_i], "gt_ohlcv"
            if hi is None:  # fall back to the scan series
                lo = int(np.searchsorted(s_ts, t, side="right"))
                hi_i = int(np.searchsorted(s_ts, t + span, side="right"))
                if hi_i > lo:
                    hi, ts, src = s_px[lo:hi_i], s_ts[lo:hi_i], "scan"
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
    total = len(tokens)
    t0 = time.time()
    for n, (chain, token) in enumerate(tokens, 1):
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
        if n % 25 == 0 or n == total:
            el = time.time() - t0
            log(f"    creator lookup {n}/{total}  elapsed {el / 60:.1f}m  "
                f"eta {(el / n) * (total - n) / 60:.1f}m")
    try:
        with open(cache_path, "w") as fh:
            json.dump(cache, fh)
    except Exception:
        pass
    return pd.DataFrame(rows)


def cluster_creators(tokens_df: pd.DataFrame, links: pd.DataFrame,
                     min_tokens: int = 2) -> pd.DataFrame:
    """Which creators/factories are behind multiple tokens, and how they ran.

    Tolerates ``tokens_df`` that already carries creator/factory columns: merging
    those again would produce creator_x/creator_y and silently drop the cluster
    column, so only genuinely missing columns are merged in.
    """
    base = tokens_df.copy()
    need = [c for c in ("creator", "factory") if c not in base.columns]
    if need:
        base = base.merge(links[["chain", "token_address"] + need],
                          on=["chain", "token_address"], how="left")
    m = base
    out = []
    for col in ("creator", "factory"):
        if col not in m.columns:
            continue
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
# 5b. WHAT HORIZON CAN THIS DATA ACTUALLY SUPPORT?
# ═════════════════════════════════════════════════════════════════════════════

def observation_spans(df: pd.DataFrame) -> pd.Series:
    """Hours of scan history per token.

    This is the binding constraint. The bot logs rows only while it is scanning
    a token, and it stops when the token leaves the feeds — measured ~1.5h
    median on a real export. A forward window longer than a token's span can
    never be fully observed, so every row is (correctly) left unlabeled.
    """
    g = df.groupby(["chain", "token_address"])["ts_epoch"].agg(["min", "max"])
    return ((g["max"] - g["min"]) / 3600).rename("span_h")


def horizon_sweep(df: pd.DataFrame, cache_dir: str, use_gt: bool,
                  horizons=HORIZON_SWEEP,
                  min_coverage: Optional[float] = None) -> pd.DataFrame:
    """Share of rows labelable at each candidate horizon, and the runner rate.

    Picking the horizon is a data question, not a modelling preference: too long
    and nothing labels (the failure this exists to prevent), too short and you
    only measure a brief pop. The sweep makes the trade-off visible instead of
    hiding it behind a default.
    """
    rows = []
    for h in horizons:
        mm = label_rows(df, cache_dir, use_gt=use_gt, horizon_h=h,
                        min_coverage=min_coverage)["max_mult"]
        lab = mm.notna()
        rows.append({
            "horizon_h": h,
            "labeled_pct": 100.0 * float(lab.mean()),
            "runner_rate_pct": float((mm[lab] >= RUNNER_MULT).mean() * 100) if lab.any() else np.nan,
        })
    return pd.DataFrame(rows)


def pick_horizon(sweep: pd.DataFrame, target: float = TARGET_LABELED) -> float:
    """Shortest horizon that labels at least ``target`` of rows and finds runners.

    Falls back to the most-labelable horizon so the run still does something
    useful, and lets the operator override with an explicit --horizon.
    """
    ok = sweep[(sweep["labeled_pct"] >= 100.0 * target) & sweep["runner_rate_pct"].gt(0)]
    if len(ok):
        return float(ok.iloc[0]["horizon_h"])
    return float(sweep.sort_values("labeled_pct", ascending=False).iloc[0]["horizon_h"])


# ═════════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════════

def main() -> int:
    global HORIZON_H   # must precede any read of HORIZON_H in this scope

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", default="auto",
                    help="label window: 'auto' (default) sweeps %.1g-%.0gh and "
                         "picks the shortest that labels enough rows, or give a "
                         "number of hours." % (HORIZON_SWEEP[0], HORIZON_SWEEP[-1]))
    ap.add_argument("--min-coverage", type=float, default=MIN_WINDOW_COVERAGE,
                    help=f"fraction of the forward window that must be observed "
                         f"for a row to be labeled (default {MIN_WINDOW_COVERAGE}). "
                         "Lower it to label more rows at the cost of measuring "
                         "truncated windows as if they were complete.")
    ap.add_argument("--csv", default="features_*.csv", help="glob of /export files")
    ap.add_argument("--outdir", default="runner_results")
    ap.add_argument("--gt-ohlcv", action="store_true",
                    help="ALSO enrich labels from GeckoTerminal 5m candles. "
                         "Rate-limited to ~30 calls/min, so it is capped by "
                         "--max-tokens and takes ~2.2s per token. Off by default "
                         "because a long export has thousands of tokens.")
    ap.add_argument("--max-tokens", type=int, default=GT_MAX_TOKENS_DEFAULT,
                    help=f"cap on OHLCV enrichment (default {GT_MAX_TOKENS_DEFAULT}); "
                         "tokens with the most scans are enriched first")
    ap.add_argument("--no-gt", action="store_true",
                    help="(kept for compatibility; OHLCV is already off by default)")
    ap.add_argument("--max-creators", type=int, default=200,
                    help="cap on Etherscan creator lookups (default 200). Tokens "
                         "with the biggest multiple are looked up first, so the "
                         "cap lands on the runners. ~0.8s per uncached token.")
    ap.add_argument("--etherscan-key", default=os.getenv("ETHERSCAN_API_KEY", ""),
                    help="for the cabal section (contractCreator/contractFactory)")
    ap.add_argument("--token-level", action="store_true",
                    help="model tokens (best row per token) instead of every scan row")
    # parse_known_args, not parse_args: running this file from the Colab file
    # browser executes it with the KERNEL's argv, which carries Jupyter's own
    # flags (-f /root/.local/share/jupyter/runtime/kernel-*.json). parse_args
    # then exits with "unrecognized arguments" before any analysis runs.
    args, unknown = ap.parse_known_args()
    if unknown:
        log(f"(ignoring interpreter arguments: {unknown[:3]})")
    use_gt = bool(args.gt_ohlcv) and not args.no_gt

    os.makedirs(args.outdir, exist_ok=True)
    cache_dir = os.path.join(args.outdir, ".cache")

    log("── 0. loading exports " + "─" * 50)
    df = load_exports(args.csv)

    log("── 1. labeling (coverage-gated) " + "─" * 35)
    # How long does the bot actually watch a token? This decides the horizon, so
    # print it before anything else: a window longer than a token's span can
    # never be observed, and labeling it anyway would call runners duds.
    spans = observation_spans(df)
    log("  observation span per token (hours):")
    log("    " + spans.describe(percentiles=[.5, .75, .9, .95])
        .round(2).to_string().replace("\n", "\n    "))
    if use_gt:
        prefetch_ohlcv(df, cache_dir, args.max_tokens)

    if str(args.horizon).lower() == "auto":
        log("\n  horizon sweep (share of rows labelable, and the runner rate):")
        sweep = horizon_sweep(df, cache_dir, use_gt,
                              min_coverage=args.min_coverage)
        for _, r in sweep.iterrows():
            extra = (f", runner rate {r['runner_rate_pct']:.1f}%"
                     if pd.notna(r["runner_rate_pct"]) else "")
            log(f"    {r['horizon_h']:>5g}h : {r['labeled_pct']:>6.1f}% labelable{extra}")
        HORIZON_H = pick_horizon(sweep)
        sweep.to_csv(os.path.join(args.outdir, "horizon_sweep.csv"), index=False)
        log(f"  → using HORIZON={HORIZON_H:g}h (shortest horizon reaching "
            f"{100 * TARGET_LABELED:.0f}% of rows; pass --horizon N to override)")
    else:
        HORIZON_H = float(args.horizon)
    if HORIZON_H <= 0:
        raise SystemExit("--horizon must be > 0 hours or the word 'auto'")

    df = label_rows(df, cache_dir, use_gt=use_gt, horizon_h=HORIZON_H,
                    min_coverage=args.min_coverage)
    have = df["max_mult"].notna()
    log(f"\n  horizon {HORIZON_H:g}h, runner bar {RUNNER_MULT:g}x")
    log(f"  labeled {have.sum():,} / {len(df):,} rows "
        f"({100 * have.mean():.1f}%)")
    if len(df):
        log("  label source:\n" + df["label_source"].value_counts().to_string())
    d = df[have].copy()
    if d.empty or d["runner"].nunique() < 2:
        log("\n  Nothing modelable: no labeled rows, or only one class present.")
        log("  Try --horizon auto, a lower --min-coverage, a longer scan history, "
            "or a lower runner bar. Stopping cleanly.")
        return 1
    log(f"  runner rate among labeled rows: {d['runner'].mean():.3f} "
        f"({int(d['runner'].sum()):,} positives)")
    log("  NOTE: the horizon above is the honest limit of how long the bot "
        "watched each token — not a property of the token.")

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
    else:
        log("  no feature has >=5 labeled rows in each class")
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
        # Cap the lookups, highest multiple first: this is a serial, latency-bound
        # API loop (~0.8s/token uncached), so an uncapped pass over a long export
        # is the same multi-hour trap as the OHLCV fetch.
        tok = tok.head(max(1, args.max_creators))
        log(f"  looking up creators for {len(tok)} token(s) "
            f"(highest multiple first; cached results are free on re-run)")
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
