# ═══════════════════════════════════════════════════════════════════════════
# WEIGHT FITTING v2 — run AFTER the runner-analysis cell (uses `d`).
#
# Why v2: the first version printed one coefficient per component and called
# anything <= 0 "cut". With ~70 positives and components that are strongly
# CORRELATED with each other (the three volume ratios are built from the same
# volume data), a coefficient's sign is unstable — a "cut" can be pure noise,
# and it does NOT mean the component is irrelevant.
#
# This version: cluster-bootstraps the coefficients (resampling TOKENS, since
# rows within a token are not independent), reports 95% CIs so unstable signs
# are visible as "unresolved", measures collinearity, and applies a verdict
# that refuses to call a weak result a win.
# ═══════════════════════════════════════════════════════════════════════════
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import roc_auc_score

N_BOOT = 200          # cluster-bootstrap resamples
MIN_POSITIVES = 100   # below this, treat the whole table as provisional

COMPS = {
    "score_vol_liq":     10,   # VOL_LIQUIDITY_PTS
    "score_vol_5m_1h":    8,
    "score_vol_1h_6h":    7,
    "score_vol_6h_24h":   5,
    "score_buy_5m":      12,   # BUY_PRESSURE_5M_PTS
    "score_buy_1h":       8,
    "score_price":       15,   # 6 + 5 + 4
    "score_holder":      20,   # 10 + 6 + 4
    "score_security":    10,
    "score_cex":          5,   # 3 listing + 2 perps
}
cols = [c for c in COMPS if c in d.columns]
Xd = d[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
yd = d["runner"].astype(int).to_numpy()
gd = (d["chain"] + ":" + d["token_address"]).to_numpy()
n_pos = int(yd.sum())

print(f"{len(yd):,} labeled rows, {n_pos:,} positives, "
      f"{len(set(gd)):,} tokens")
if n_pos < MIN_POSITIVES:
    print(f"\n!! WARNING: only {n_pos} positives. Fitting ~{len(cols)} correlated "
          f"weights on this many positives gives unstable coefficients — expect "
          f"signs to flip between resamples. Treat everything below as a "
          f"hypothesis to re-test with more data, NOT as new weights.")

# ── 1. collinearity: why a component can be "cut" while mattering ──────────
corr = Xd.corr().abs()
pairs = [(a, b, corr.loc[a, b]) for i, a in enumerate(cols) for b in cols[i+1:]
         if corr.loc[a, b] >= 0.70]
print("\n── collinear component pairs (|r| >= 0.70) ──")
if pairs:
    for a, b, r in sorted(pairs, key=lambda t: -t[2]):
        print(f"  {a} ~ {b}: r={r:.2f}")
    print("  Highly correlated components SHARE one signal. Logistic regression "
          "splits that signal arbitrarily between them, so one may get a large "
          "positive coefficient and the other a negative one — the negative is "
          "an artifact of the split, not evidence the component is harmful.")
else:
    print("  none above 0.70")

# ── 2. fit + cluster bootstrap (resample TOKENS, not rows) ────────────────
def _fit(Xm, ym):
    m = LogisticRegression(max_iter=3000, class_weight="balanced")
    m.fit(Xm, ym)
    return m.coef_[0]

full = _fit(Xd, yd)
rng = np.random.default_rng(0)
tok_ids = pd.Series(gd).unique()
boot = []
for _ in range(N_BOOT):
    pick = rng.choice(tok_ids, size=len(tok_ids), replace=True)
    idx = np.concatenate([np.where(gd == t)[0] for t in pick])
    yb = yd[idx]
    if yb.min() == yb.max():
        continue
    boot.append(_fit(Xd.iloc[idx], yb))
boot = np.array(boot)
lo, hi = np.percentile(boot, [2.5, 97.5], axis=0) if len(boot) else (full, full)

tab = pd.DataFrame({
    "component": cols,
    "current_max_pts": [COMPS[c] for c in cols],
    "coef_per_point": full.round(3),
    "ci_low": lo.round(3),
    "ci_high": hi.round(3),
    "verdict": ["raise" if l > 0 else ("cut" if h < 0 else "unresolved")
                for l, h in zip(lo, hi)],
})
print(f"\n── component weights, {N_BOOT} cluster-bootstrap resamples "
      f"({len(boot)} usable) ──")
print(tab.to_string(index=False))
n_unres = int((tab["verdict"] == "unresolved").sum())
if n_unres:
    print(f"  {n_unres} of {len(cols)} components have a CI spanning zero: their "
          f"sign is NOT determined by this data. Do not cut them on this evidence.")

# ── 3. two candidate reweightings, scored honestly ────────────────────────
def reweight(comp_weights):
    ratio = np.array([comp_weights.get(c, 0) / COMPS[c] for c in cols])
    return (Xd.to_numpy() * ratio).sum(axis=1)

cur = pd.to_numeric(d["hand_score"], errors="coerce").fillna(0).to_numpy()
pos = pd.Series(full, index=cols).clip(lower=0)
cur_total = sum(COMPS[c] for c in cols)
sug_full = (pos / pos.sum() * cur_total).round(2) if pos.sum() else pos
# conservative: only components whose CI is strictly positive get weight
cons = pd.Series([full[i] if lo[i] > 0 else 0.0 for i in range(len(cols))], index=cols)
sug_cons = (cons / cons.sum() * cur_total).round(2) if cons.sum() else cons

cands = {
    "current hand_score": cur,
    "reweighted (all fitted)": reweight(sug_full),
    "reweighted (CI-positive only)": reweight(sug_cons),
}
def patk(yy, s, frac):
    k = max(1, int(len(s) * frac)); return float(np.mean(yy[np.argsort(s)[::-1][:k]]))

print("\n── does reweighting rank better? (token-grouped split) ──")
rows = []
for tr, te in GroupShuffleSplit(n_splits=5, test_size=.25, random_state=0).split(Xd, yd, groups=gd):
    if len(np.unique(yd[tr])) < 2 or len(np.unique(yd[te])) < 2:
        continue
    for name, sc in cands.items():
        rows.append({"score": name, "auc": roc_auc_score(yd[te], sc[te]),
                     "prec@5%": patk(yd[te], sc[te], .05),
                     "prec@1%": patk(yd[te], sc[te], .01)})
res = pd.DataFrame(rows).groupby("score")[["auc", "prec@5%", "prec@1%"]].mean()
print(res.round(4).to_string())
print(f"  (random = {yd.mean():.4f})")

# ── 4. verdict, with a floor that refuses to call a weak result a win ─────
auc_cur = float(res.loc["current hand_score", "auc"])
auc_new = float(res.loc["reweighted (all fitted)", "auc"])
p_cur = float(res.loc["current hand_score", "prec@5%"])
p_new = float(res.loc["reweighted (all fitted)", "prec@5%"])
delta = auc_new - auc_cur
MIN_AUC = 0.60      # below this the score barely ranks at all
MIN_DELTA = 0.03    # and a tiny gain is not worth a config change

print("\n── verdict ──")
print(f"  AUC {auc_cur:.4f} -> {auc_new:.4f} ({delta:+.4f}); "
      f"precision@5% {p_cur:.4f} -> {p_new:.4f}")
if n_pos < MIN_POSITIVES:
    print(f"  NOT ACTIONABLE: {n_pos} positives is too few to reweight on. "
          f"Collect more data first (and see the span note below).")
elif auc_new < MIN_AUC and auc_cur < MIN_AUC:
    print(f"  NOT ACTIONABLE: both scores are below AUC {MIN_AUC:.2f}, i.e. barely "
          f"better than a coin flip. Reweighting this will not separate runners "
          f"from duds; the features themselves are the limit.")
elif delta < MIN_DELTA:
    print(f"  NOT WORTH IT: a gain of {delta:+.4f} AUC is inside the noise. "
          f"Look at observation spans and new features instead of weights.")
else:
    print(f"  ACTIONABLE: +{delta:.4f} AUC is a real gain. Apply the "
          f"'CI-positive only' weights (they ignore unstable components), then "
          f"re-measure on fresh data before trusting them.")
print("\n  Reminder: AUC uses every row, but you only trade the top of the "
      "ranking — precision@k is the number that pays. And remember the label "
      "horizon: if it is 0.5h, this predicts a 30-minute double, not a durable "
      "runner.")

# ── 5. is the edge just momentum? (needs the analysis cell's feats/X) ─────
MOMENTUM = ["chg_5m", "chg_15m", "chg_1h", "chg_6h", "chg_24h",
            "buy_ratio_5m", "buy_ratio_1h", "buys_5m", "sells_5m",
            "buyers_5m", "sellers_5m", "vol_5m", "vol_5m_1h"]
try:
    keep = [f for f in feats if f not in MOMENTUM]
    if len(keep) < 3:
        print(f"\n(momentum ablation skipped: only {len(keep)} feature(s) left "
              f"after dropping the recent-move set — not enough to compare)")
    else:
        Xk = d[keep].apply(pd.to_numeric, errors="coerce").fillna(0.0)
        a_all, a_no = [], []
        for tr, te in GroupShuffleSplit(n_splits=5, test_size=.25,
                                        random_state=0).split(X, y, groups=grp):
            if len(np.unique(y[tr])) < 2 or len(np.unique(y[te])) < 2:
                continue
            m1 = LogisticRegression(max_iter=3000, class_weight="balanced").fit(X.iloc[tr], y[tr])
            m2 = LogisticRegression(max_iter=3000, class_weight="balanced").fit(Xk.iloc[tr], y[tr])
            a_all.append(roc_auc_score(y[te], m1.predict_proba(X.iloc[te])[:, 1]))
            a_no.append(roc_auc_score(y[te], m2.predict_proba(Xk.iloc[te])[:, 1]))
        print(f"\n── momentum ablation ──\n  AUC with recent-move features   : "
              f"{np.mean(a_all):.4f}\n  AUC without them ({len(keep)} left)     : "
              f"{np.mean(a_no):.4f}")
        if np.mean(a_all) - np.mean(a_no) > 0.05:
            print("  The edge leans on features describing the move already "
                  "underway. That can still be tradeable, but it is momentum "
                  "detection, not foresight — and it is what late alerts look "
                  "like.")
        else:
            print("  The edge survives dropping recent-move features, so it is "
                  "not purely momentum.")
except NameError:
    print("\n(momentum ablation skipped: run the runner-analysis cell first so "
          "`feats`, `X`, `y` and `grp` exist)")
