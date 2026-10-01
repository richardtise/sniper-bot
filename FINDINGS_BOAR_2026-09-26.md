# Findings — why boar (`0x0cbf291Ba052174879d90bf781dF1A5F2BC5Bb07`, Base) never alerted

> **Point-in-time record (2026-09-27, code at `9458812`).** This document explains
> one incident and the fixes it motivated. It is *history*, not current
> configuration: where it disagrees with [`README.md`](README.md) or `bot.py`,
> those are authoritative. In particular, threshold values quoted here (bars,
> floors, weights) have since been retuned, and the "still open" list at §8 is
> partly closed — see the README's [Tradeable venues](README.md#tradeable-venues--do-you-need-uniswap-v4)
> and [security](README.md#how-the-security-check-works-and-why-new-pairs-are-dropped)
> sections for the current state.
>
> The thresholds it discusses were calibrated on single cases like boar. That is
> a falsification test (a setting must admit a known runner without admitting a
> known rug), not calibration — see
> [Where these numbers come from](README.md#where-these-numbers-come-from).

Date of incident: **2026-09-26**. Written 2026-09-27 from code at `9458812`.

**Verdict: the bot was seeing this token and scoring it. The miss is the scoring
model, not discovery.** Confirmed by the operator's production logs: boar was
present from ≈13:53 UTC at ~$250k mcap with score **38**, and its best score in
the whole run was **48** at ~$370k mcap. It then went to a **$4.82M** ATH. The
alert bar that day was **61.26** (see §2a — the commonly quoted 63.86 is the
bar only when a top-100 holder figure is available, which it is not on this
deployment).

---

## 1. What actually happened (verified from pool OHLCV + DexScreener/GeckoTerminal)

Pool: `0xef256e214c45aab706ca54d6e2c5d0ca42b87895a43e97b382ea012d13e78e49`
(`uniswap-v4-base`, `boar / WETH`), created **2026-09-26 02:58:01 UTC**,
total supply 100B.

| Time (UTC) | Event | mcap at candle open |
|---|---|---|
| 09:00–09:30 | first leg, $34k → $800k on ~$800k volume (~30 min) | 0.03M → 0.5M |
| 10:00–15:30 | fade to $270k, then chop $270k–$800k for ~5.5 h | 0.47M → 0.29M |
| 15:45 | **ignition** — one 15m bar does $153k | 0.31M |
| 16:00 | one 15m bar does **$1.42M**, high $0.0000326 | 0.77M → 3.26M |
| 16:15 | high $0.0000406, $1.22M of volume | 4.06M |
| 16:45 | **ATH $0.0000482 / $4.82M mcap**, $730k volume | 3.19M |
| 17:30–19:00 | round trip $2.2M → $4.5M → $1.5M | — |
| Sep 27 | dead: $1.66M mcap, 5m volume often **$0** | — |

Operator's flow description ("38k → 800k, dip to 250k, then ATH 4.7m") matches
this tape. The number the bot needed to beat while boar sat at $250k–$370k was
**61.26**.

## 2. Why 61.26 was unreachable in practice

`effective_threshold()` scales the bar by `max_possible_score()`, which is a
theoretical ceiling computed by driving every ratio to its top tier
(`bot.py:1650-1714`). At age 655 min (13:53 UTC) the ceiling is 94.25, so the
gate becomes `65 × 94.25/100 = **61.26**`, i.e. the model demanded **65 % of all
points**. (With a top-100 holder figure available the ceiling would be 98.25 and
the bar 63.86 — the *higher* number quoted elsewhere. Boar's deployment gets no
top-100, so 61.26 is the bar it actually faced; §10 shows that having the
top-100 would have made things worse, not better.)

The components the model *could* award that day:

| Component | Max | Notes |
|---|---|---|
| `vol_5m / liquidity` | 10 | needs 5m volume ≥ pool liquidity |
| `vol_5m / vol_1h` | 8 | needs 5m volume ≥ ⅔ of the hour |
| `vol_1h / vol_6h` | 7 | needs the hour ≥ ¼ of the 6 h block |
| `vol_6h / vol_24h` | 5 | needs **vol_6h ≥ 4 × vol_24h** |
| buy pressure 5m | 12 | needs buy ratio ≥ 0.85 |
| buy pressure 1h | 8 | needs buy ratio ≥ 0.80 |
| price 5m / 1h / 6h | 6 / 5 / 4 | needs +50 % / +100 % / +200 % in each window |
| holders top10 / top50 | 10 / 6 | top10 ≥ 80 % / top50 ≥ 90 % |
| holders top100 | 4 | **never measurable on Base** (no GT 51–100 band) |
| security | 10 | GoPlus clean |
| CEX | 5 | |

### 2.1 The ceiling is a fiction — the ratio tiers are mutually exclusive

The four volume components share one volume distribution and cannot peak
together:

* `vol_5m ≥ 0.667·vol_1h`
* `vol_1h ≥ 1.0·vol_6h` (top tier of `score_1h_6h` needs normalised ≥ 6 → ratio ≥ 1.0)
* `vol_6h ≥ 4·vol_24h`

Satisfying all three implies a pool whose entire 24 h of trading happened inside
one hour and mostly inside one 5-minute window. Any real pool — including one
doing 13× — loses at least a third of the 30 volume points. So the "reachable"
ceiling overstates reality by roughly 10–15 points, and the scaled bar inherits
the error.

Working the realistic distribution for a genuine pump at that age (5m at 0.4–1.0×
liquidity, hour-heavy but not hour-total, 6h/24h ≈ 1.3×):

```
vol/liq        4.0 – 6.0
5m/1h          4.0 – 6.0
1h/6h          5.25 (tier 3)
6h/24h         3.0  (tier 2, needs ratio 2.0)
buy pressure  12.0 (5m) + 8.0 (1h)
price         15.0
holders       16.0 (top100 unmeasurable)
security      10.0
cex            0.0
             ─────────
              77.3 – 79.3  of a 94.25 ceiling = 82–84 % of ceiling
```

So a *textbook* pump bar could clear 61.26 — barely, and only if the 5-minute
slice happens to be large. That is the entire problem: the gate sits so close to
the practical maximum that **the only thing that alerts is the single luckiest
5-minute candle**, and anything the model slightly dislikes (a 5m ratio of 0.2,
one down-candle, a sell-dominated 5m window) drops 20+ points and silences it.

### 2.2 Where a real 13× token lost its points

Observed best score **48**. Reconstructing the 13:53–16:45 window with real
candle data, the losses land almost entirely in five places:

| Lost | Points | Cause |
|---|---|---|
| `vol_5m / liquidity` | ~8 | pool at $250k–$800k depth; a $20k 5-minute bar is 0.025–0.08× |
| `vol_5m / vol_1h` | ~6 | the hour is the unit that moved, not the 5 m slice |
| `vol_6h / vol_24h` | ~2 | pool younger than 24 h; 6h/24h can never reach 4× |
| holder top10/top50 | ~8 | top10 33 %, top50 56 % → only 2.5 + 0 = 2.5 of 16 |
| holder top100 | 4 | structurally unmeasurable on all chains |
| `signal_penalty` | 0–30 | `active_dump`, `low_activity`, `sell_dominated_5m` fire on the pullbacks *inside* the pump |

That is 28 points unavailable before any judgement is made, out of a 100-point
scale being policed at 65 %.

## 3. Structural defects, in priority order

1. **The model is a "steady accumulation" scorer, not a "runner" scorer.**
   Every volume component is a *ratio of short window to longer window*. That
   rewards a token that trades evenly all day and punishes one whose volume is
   concentrated in a single hour — which is what a 15× move looks like.
   (`bot.py:1559-1600`)
2. **The ceiling counts unearnable points as reachable.** Mutually exclusive
   ratio tiers (§2.1) plus the permanently unmeasurable `top100` block mean the
   scaled gate is set against a number no pool can hit.
   (`bot.py:1650-1714`)
3. **The signal engine can only veto, never promote.** `SIGNAL_BONUS_WEIGHT`
   defaults to `0.0` (`bot.py:1930-1940`), and signals are evaluated *after* the
   hand score, so `PairHistory` acceleration data — the one thing that measures
   "this pool is waking up" — can never lift a token over the gate.
4. **Hard vetoes run before scoring**, so a candidate never even gets a number
   when `low_activity` / `sell_dominated_5m` / `active_dump` fire
   (`signals.py:361-446`). `low_activity` is a flat ratio: `min_vol_liq_ratio =
   0.05`, so the deeper the pool, the more dollars it takes to pass. For a
   $392k pool the floor is **$19.6k per 5 minutes**, and the same veto clears
   trivially at $60k depth.
5. **Holder concentration is worth up to 16 points, and a runner cannot earn
   them.** `score_holder` wants top10 ≥ 80 % / top50 ≥ 90 % for full marks
   (`bot.py:1602-1628`); a healthy running token is *less* concentrated, so the
   scorer pays it less. On top of that the pump stance pays nothing at
   top50 < 60 %.
6. **Age gates silently zero the long windows** (`age > 60` for 1h pressure,
   `age > 360` for the 6h price branch) while `max_possible_score` keeps counting
   them for young pools — the same class of error as (2).
7. **Tuning the current knobs cannot fix this.** Raising `SIGNAL_BONUS_WEIGHT`
   makes an unvalidated additive bonus the gate (README admits pass 2 had to be
   reverted for exactly that). Lowering `MIN_SCORE` lowers everything, including
   the junk. The defect is the ranking function, not its thresholds.

## 4. The consequence for this trade

boar was ranked **below** the gate at 38 and 48 while it was 12 hours into a
13× move. It was not a filter problem and not a latency problem — the model
looked at a live runner and called it mediocre. **No amount of threshold tuning
makes a 48 out of 61.26**, and the same function also scored `TALIS`
(`0xd5D26bac…`, Robinhood) high enough to be a false positive while it was
`h6 = −51 %` with a 0.54 buy ratio. Both errors are the same error: component
weights that do not correspond to forward return.

## 5. Recommended direction — learn the weights, don't guess them

The hand-tuned score is 10 tier functions with 8 breakpoints each
(≈80 hardcoded constants) fitted by intuition. It should be replaced by a model
fitted on outcomes. The infrastructure already exists; it is switched off and
partly blind:

**Already built**
* `LOG_FEATURES=true` → `features` table, one row per *evaluation*, including
  all raw inputs (volume windows, ratios, txns, buyers, price changes, holders,
  taxes, security, `base_score`, `hand_score`, `reject_reasons`,
  `alert_sent`). `bot.py:537-722`, `bot.py:1761-1795`.
* `label_outcomes.py` → joins each row to the token's *forward* max price using
  later scans as the price series and writes `max_mult_1h/6h/24h` plus
  `hit_2x_*`, `hit_3x_*`, `hit_5x_*` labels, and `--export training.csv`.
* `diag/repro_boar.py` — reproduces the gate decisions for this incident offline.
* `diag/log_boar_row.py` — runs a live token through the real evaluation path and
  prints the exact feature row, without starting a scan.

**Gaps that must be closed first**

1. ✅ **CLOSED — the rows are logged but were often empty.** Verified: `reject(reason)`
   is `finish(reason)`, and `finish` always calls `FEATURE_LOGGER.log_row(feat)`,
   so rejects *are* persisted and labelled with `reject_reasons` (this is the good
   news, and my first draft of this document got it wrong). The real hole was that
   the early floors — `liquidity`, `price_too_low`, `market_cap`, `vol_5m` —
   returned **before** the block that fills the volume/txn/price-change fields, so
   those rows carried zeros for every interesting input and a thin 5-minute read
   was indistinguishable from a token that never traded. All raw metrics are now
   read before the floors, and `ceiling_score` / `alert_threshold` are recorded
   provisionally (with `has_top100=False`) so even an early reject shows the bar it
   faced. Pinned by `test_floor_reject_still_carries_the_raw_metrics`.
2. ⬜ **OPEN — the local `features` table is empty (0 rows).** The configuration
   half has since been done — `LOG_FEATURES=true` is now set in the deployment's
   `.env`, and both `.env.example` and the README document it — but nothing below
   matters until rows actually exist: every day it runs blind is a day of
   training data that cannot be recovered.
3. ✅ **CLOSED — the score components are now stored**, not just the total:
   `score_vol_liq`, `score_vol_5m_1h`, `score_vol_1h_6h`, `score_vol_6h_24h`,
   `score_buy_5m`, `score_buy_1h`, `score_price`, `score_holder`, `score_security`,
   `score_cex`, `penalties_total`. `test_components_reconstruct_the_hand_score`
   asserts they sum to `hand_score`, so a component added later without a column
   fails the suite instead of silently vanishing.
4. ⬜ **OPEN — tokens discovery never offers are invisible.** The table can only
   contain what `dedupe_best_pool` passed through, so it cannot answer "how many
   Base pools did GT rank above boar, and what were their scores". If that
   question matters, log the feed listing itself, not just the evaluation.
5. ⬜ **OPEN — label with a realistic horizon and an eligible-entry price.** `hit_3x_1h`
   from a row where the 5m candle already pumped is not a tradeable label; the
   forward max should be measured from the next tick, as `label_outcomes.py`
   already does, and entry should be filtered by whether a route exists (§6).
5. **Train and compare against the hand score, not in the abstract.** Split by
   token, not row (same token across scans is not independent — README says this
   too). Report precision/recall at the *same alert volume* as the hand scorer,
   so the comparison is honest: "same number of alerts, 3× the runners".

**Then** the gate becomes `model_score` with the signal engine as features
rather than a veto-only overlay, and `low_activity`/`active_dump` become model
inputs instead of hard pre-emptions. That preserves their protective value
(wash trading, honeypots) where it is real, while letting the model speak for a
runner whose 5-minute slice looks thin.

## 6. Separate defect found while investigating (execution, not detection)

Every pool involved in this incident trades on **Uniswap V4**:

| Token | Chain | Pool labels | Depth | 24h volume |
|---|---|---|---|---|
| boar `0x0cbf…Bb07` | Base | `v4` | $392k (DS) / $534k (GT) | $4.0M |
| FILR `0xb6ef…d1da` (your catch) | Robinhood | `v4` | $310k | $10.8M |
| TALIS `0xd5D2…D8e2` (false positive) | Robinhood | `v4` | $33k | $0.45M |

The bot only builds `exactInputSingle` on `*_ROUTER_V3` (`bot.py:2011-2200`).
`REVIEW.md` roadmap item 10 states Aerodrome and V4 are not tradeable. So the
buy button on every one of these alerts would fail at quote time — the operator
was never able to act on the alerts that did fire. **Detection fixes are of
limited value until V4/Universal Router execution exists**, or until alerts
refuse to present dead buy buttons and say why.

> **Since closed — the alert half only.** Alerts now name the venue and, when no
> configured router reaches the pool, print a `⚠️ No route — not tradeable by
> this bot.` header naming the DEX, with the buy buttons withheld
> (`build_no_route_keyboard`); `REQUIRE_TRADEABLE_VENUE=true` drops such pools
> before they cost enrichment budget instead (see the README's
> [Tradeable venues](README.md#tradeable-venues--do-you-need-uniswap-v4) section).
> **Still open:** V4 / Universal Router *execution* itself — the bot still has no
> V4 path, so even a venue-named alert cannot be acted on for those pools.

## 7. One-paragraph answer to "is this code better than the initial one?"

On *discovery*, yes and unambiguously: the initial version called
`/latest/dex/pairs/{chain}` (verified today: still **HTTP 404**) and
`token-boosts/top/v1` (currently 30 entries, **0 on Base**), so it could only
ever see paid shills — it would never have put boar in front of any gate. On
*scoring*, no: both versions use the same hand-tuned ratio model, and that model
is what scored a 13× mover 38–48 against a 61.26 bar. The initial code's
silence on Base was accidental; the current code's silence on boar is systematic.

## 8. What shipped on 2026-09-27 (findings only — scoring untouched)

The scoring model is deliberately **not** changed yet: that decision was left
open while the code is studied. What shipped is the instrumentation the model
work needs, plus a guard against changing behaviour by accident.

| Change | Why |
|---|---|
| Raw metrics are read **before** the per-chain floors | Floor rejects used to log zeros for every window and ratio (gap 1) |
| Ten `score_*` columns + `penalties_total` in `features` | "48 against a bar of 61" is now attributable to the components that withheld the points (gap 3) |
| `ceiling_score` + `alert_threshold` logged on every row, provisionally for early rejects | The bar and the reachable maximum are what make a hand score interpretable |
| `diag/log_boar_row.py` | Prints the exact row the bot would write for any token, without a scan |
| `diag/compare_decisions.py` | Runs 13 fixture cases through two revisions of `bot.py` and fails if any reject reason or alert decision changed |
| `test_features.py`: 6 new tests | Pin the pre-floor capture, the components-sum-to-hand-score invariant, the signal-veto row, the schema migration onto an existing DB, and the writer path |

Verification performed:

* `python -m unittest test_features test_signals test_discovery test_telegram`
  → **64 tests, OK**.
* `diag/compare_decisions.py` (HEAD vs working tree) → **13/13 cases identical**,
  including all four floors, the three signal vetoes, the three security rejects,
  `alert_eligible`, `below_threshold` and `robinhood_too_new`. The logging change
  is decision-neutral.
* `diag/log_boar_row.py` on live boar data wrote a full row
  (`score_vol_liq=2.0`, `score_security=10.0`, `penalties=15`,
  `base_score=1.0`, `ceiling_score=94.25`, `alert_threshold=61.26`,
  `reject_reasons='phase1_gate'`) where the pre-change code logged almost nothing.

### 8a. Config bug found while verifying

`USE_SIGNALS` defaults to **`false`** in code (`bot.py:85`) but the README config
table claimed `true`, and `.env.example` ships `USE_SIGNALS=true`. A deployment
that does not explicitly set it runs with the entire signal engine disabled —
no unique-buyer gates, no wash-trade gate, no activity floor, no `active_dump`
guard — while the README says otherwise. Your production logs show
`Signal reject …`, so **your deployment has it on**; the README row has been
corrected to match the code. Whether the *code* default should become `true` is a
behaviour change and was left to you.

### Still open (unchanged by this pass)

1. The scoring model itself (§3) — the actual reason boar was missed.
2. Training rows (gap 2): the local `features` table still has **0 rows** — the
   configuration half is now done (`LOG_FEATURES=true` in the deployment's `.env`,
   documented in the README), but nothing above matters until rows start
   accumulating.
3. V4 / Universal Router *execution* (§6) — only half closed: alerts now refuse
   dead buy buttons, but the router path does not exist yet.
4. A false-positive guard for the `TALIS` pattern — h6 ≈ −50 % with a 0.54 buy
   ratio while alerting (§4).

**Since closed (after this pass):** the ignition watchlist for pools that go
quiet and re-ignite (§5, gap 4) shipped in commit `dc37eae` —
`bot.db_watchlist_remember` / `collect_watchlist_pairs`, and the README now calls
the [watchlist the main
lane](README.md#the-universe-problem--and-why-the-watchlist-is-now-the-main-lane).
The alert half of item 3 also shipped: alerts name the venue and withhold buy
buttons when no configured router reaches the pool (§6).


## 9. The two legs, scored with the bot's own functions

`diag/legs_boar.py` reconstructs the trailing windows from 5-minute candles and
calls the real `score_5m_1h` / `score_1h_6h` / `score_6h_24h` / `score_price`.
Only 35 of the 100 points are derivable from candles (liquidity history and
per-window transaction counts are not published retroactively), so the totals
below are a **floor with a stated range**, not the exact hand score. The range
adds the blocks that need live lookups: holders (2.5 for boar), security (10),
`vol/liq` (0–10) and buy pressure (0–20).

| Bar (UTC, Sep 26) | age | mcap | candle-derivable | next 1 h max | achievable total | bar |
|---|---|---|---|---|---|---|
| 09:00 | 362 m | $0.03 M | **20.6 / 35** | **+2242 %** | 33.1 – 63.1 | 61.26 |
| 09:15 | 377 m | $0.23 M | **29.4 / 35** | +278 % | 41.9 – 71.9 | 61.26 |
| 09:30 | 392 m | $0.50 M | 25.6 / 35 | +77 % | 38.1 – 68.1 | 61.26 |
| 15:45 (ignition) | 767 m | $0.31 M | **3.0 / 35** | **+1202 %** | 15.5 – 45.5 | 61.26 |
| 16:00 | 782 m | $0.77 M | 18.4 / 35 | +528 % | 30.9 – 60.9 | 61.26 |
| 16:15 | 797 m | $3.12 M | 25.4 / 35 | +54 % | 37.9 – 67.9 | 61.26 |
| 16:45 | 827 m | $3.19 M | 17.2 / 35 | +51 % | 29.7 – 59.7 | 61.26 |

Two different failures, not one:

**(a) The 38k→800k leg was a coverage failure.** At 09:00 the model was already
awarding boar **20.6 of the 35 candle-derivable points** with a +2242 % hour
ahead; by 09:15 it was 29.4/35 and the achievable range (41.9–71.9) straddled the
bar, needing only 19.4 of the 30 lookup-dependent points. The operator's logs
show the first boar line at **13:53**, so nothing was evaluating it during that
leg. The model was not the blocker for the move you asked about — the bot was
not looking.

**(b) The $0.31 M→$4 M leg was a scoring failure, and specifically a lag.**
At the ignition bar (15:45, +1202 % in the next hour) the model awarded
**3.0 of 35**: every trailing window was still flat, because the components
measure the *past* hour, not the bar that starts the move. It then chased: 18.4
at +15 min, 25.4 at +30 min — by which point mcap had gone $0.31 M → $3.12 M.
The best bar it produced was still only somewhere around the operator's logged
maximum of 48, against 61.26.

## 10. Top-100 holders: can it be obtained, and would it have helped?

### 10a. What is actually available for Base

| Source | Result (tested today) |
|---|---|
| GeckoTerminal `tokens/{addr}/info` | **Works, free, keyless.** Bands `top_10` 31.11 %, `11_30` 14.89 %, `31_50` 9.01 %, `rest` 44.99 %, holder count 4097. Exact top-10 and top-50; **no 51–100 band**. |
| Moralis (the bot's first choice) | **HTTP 403 Forbidden** — the key in `.env` is dead, exactly as README warned. |
| Etherscan v2 `tokenholderlist` | `NOTOK: Free API access is not supported for this chain.` Base is not covered on the free tier. |
| Base Blockscout `/holders` | Endpoint answers, keyless — but its index for this token is **wrong**: 51 holders totalling 8.6 % of supply, and the Uniswap V4 PoolManager (which holds 14.8 %) is missing entirely. Unusable for V4-era Base tokens. |
| **Own RPC replay** (`diag/holders_from_logs.py`) | **Works, free, exact.** Replays every `Transfer` log over Base JSON-RPC and aggregates balances. |

The replay self-validates against GeckoTerminal on the same token:

| | replay (now) | GeckoTerminal |
|---|---|---|
| top-10 | 30.53 % | 31.11 % |
| top-50 | 55.26 % | 55.01 % |
| **top-100** | **68.92 %** | not published |

(The small deltas are snapshot timing; the method is sound. It also confirms the
Blockscout diagnosis — the replay's largest holder is the V4 PoolManager at
14.80 %, which Blockscout omits.)

### 10b. Would a top-100 figure have saved boar? No — it would have hurt

Boar's real top-100 is **68.92 %**. The scoring tier requires `top100 >= 70` for
any points at all (`bot.py:1634`), so 68.92 % earns **zero** — the same as the
unmeasurable `None`. But the *ceiling* grows by the full 4 points, and the bar is
scaled against the ceiling:

| | ceiling | bar | boar's holder points | boar's best score | gap |
|---|---|---|---|---|---|
| top-100 unavailable (today) | 94.25 | **61.26** | 2.5 | 48 | −13.3 |
| top-100 available, 68.92 % | 98.25 | **63.86** | 2.5 | 48 | **−15.9** |
| top-100 available, ≥ 70 % | 98.25 | 63.86 | 4.5 | 50 | −13.9 |
| top-100 available, ≥ 95 % | 98.25 | 63.86 | 6.5 | 52 | −11.9 |

Even in the theoretical best case (95 %+ concentration) boar gains 4 points while
the bar gains 2.6 — it ends up **11.9 points short instead of 13.3**. At its
actual 68.92 % it loses **2.6 points** net.

**The general lesson is bigger than boar:** because
`threshold = MIN_SCORE × ceiling / 100`, every newly measurable scoring
dimension raises the bar by `MIN_SCORE` % of that dimension's weight (2.6 points
for a 4-point block at `MIN_SCORE=65`). A token only benefits if it earns *more*
than 65 % of the new block — and a concentrated-holder metric is precisely where
a real runner earns below average. **Improving provider coverage can make the
gate stricter for the tokens you are hunting.** If top-100 is wanted for its own
sake (rug detection, or chains where runners *are* concentrated), it should be
added without counting its points toward the normalised ceiling, or the
normalisation itself needs revisiting.

## 11. Measured: do the score components rank runners at all?

`diag/component_audit.py` rebuilds the trailing windows for 50 Base pools
(5 148 observations, 1 h non-overlapping) from public OHLCV, scores each bar with
the bot's real functions, and measures what the price did next. Inference uses a
per-pool sign test because observations inside a pool are autocorrelated. Sample
caveat: the pool universe is GeckoTerminal trending/top-volume, i.e. already
selected, which limits generalisation but not the within-pool comparison.

**Rank quality for a ≥ 10 % move within the hour** (78 of 5 148 bars):

| component | AUC | verdict |
|---|---|---|
| `score_vol_5m_1h` | 0.512 | coin flip |
| `score_vol_1h_6h` | 0.555 | marginal |
| `score_vol_6h_24h` | 0.579 | marginal |
| `score_price` | 0.580 | marginal |
| **all four combined** | **0.590** | marginal |

For the moves that actually matter the ranking degrades to or below chance:

* ≥ 25 % in 1 h (9 bars): combined AUC **0.472**
* ≥ 50 % in 6 h (20 bars): combined AUC **0.455**
* ≥ 100 % in 6 h (4 bars): combined AUC **0.238** — the *largest* movers were
  ranked worst. (Four positives; treat the exact figure as noise, the direction
  as a warning.)

Rank correlations agree: pooled Spearman 0.06–0.10, and the per-pool means are
0.02–0.12. In economic terms, a 1 h forward return of **1.5 % when the component
fires versus 1.3 % when it does not** — a 0.2-point edge, for the 20–35 points of
the model that candles can reconstruct.

This is the quantitative version of the boar anecdote: the volume/price
components carry a *detectable but tiny* relationship with forward return, and
for the fat tail the bot exists to catch they carry none. That is what a 48
against a 61 bar looked like from the inside.
