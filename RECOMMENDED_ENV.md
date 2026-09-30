# Recommended `.env` profile, and what the score's weights are actually worth

Two questions answered here: **what settings to run**, and **whether the score's
weights are any good**. The second one is measured, not opinion — re-run
`diag/component_audit.py` to reproduce (4,550 observations, 49 Base pools,
5-minute candles).

---

## 1. The gate: what "score over 50/100" means in this codebase

`threshold = MIN_SCORE × ceiling ÷ 100`, floored at `MIN_EFFECTIVE_SCORE`, where
`ceiling = max_possible_score(chain, age)`. The ceiling is *not* 100: a 10-minute
Robinhood pool can reach about **53**, because its 1h/6h/24h windows are empty and
no Robinhood token can score CEX points.

So a raw `MIN_SCORE=50` out of 100 is 94% of what a young pool can physically
score — which is why the original `65` default produced silence, and why
`SCORE_NORMALIZE` exists.

For "hand score ≥ 50" to be the thing that actually alerts, on every chain and
age, the working combination is:

| `SCORE_NORMALIZE` | `MIN_SCORE` | `MIN_EFFECTIVE_SCORE` | Young RH pool bar | Verdict |
|---|---|---|---|---|
| `false` | 50 | — | **50 / 53 = 94% of reachable** | near-silence |
| `true` | 50 | 45 | 45 / 53 = **85%** | too strict; the floor overrides your intent |
| **`true`** | **50** | **30** | **30 / 53 = 57%** | **consistent ~50% of reachable everywhere** |

With `MIN_SCORE=50`, `SCORE_NORMALIZE=true`, `MIN_EFFECTIVE_SCORE=30`:

| chain | age | ceiling | bar | % of reachable |
|---|---|---|---|---|
| robinhood | 10m | 53.0 | 30.0 | 57% |
| robinhood | 70m | 74.8 | 37.4 | 50% |
| robinhood | 25h | 89.2 | 44.6 | 50% |
| base / bsc / ethereum | 10m | 58.0 | 30.0 | 52% |
| base / bsc / ethereum | 25h | 94.2 | 47.1 | 50% |

That is the meaningful reading of "alert me above 50": 50% of what the chain and
age can actually reach, rather than 50 of a theoretical 100.

---

## 2. The profile

Set these in `.env`. Everything else can keep its documented default.

```dotenv
# ── The gate you asked for ───────────────────────────────────────────────────
SCORE_NORMALIZE=true
MIN_SCORE=50
MIN_EFFECTIVE_SCORE=30
ROBINHOOD_MIN_SCORE=50          # must be set, or it silently overrides MIN_SCORE
PAPER_TRADING=true              # until you have watched a full day of alerts

# ── Discovery: early on every chain ──────────────────────────────────────────
USE_GECKOTERMINAL=true
GT_SOURCES=new_pools,trending,top_volume
GT_PAGES_NEW=1
GT_PAGES_TRENDING=1
SIG_MAX_AGE_MINUTES=0           # age is not a quality signal

# ── Filters: safety is the part that works, so keep it strict ────────────────
USE_SIGNALS=true
MAX_ALLOWED_TAX=10
ALLOW_SECURITY_FALLBACK=false   # unknown != safe. Tradeoff below.
SIG_REQUIRE_OPEN_SOURCE=false   # most Robinhood runners are unverified

# ── Early lane (the only path that was ever going to catch young pools) ──────
EARLY_RUNNER_MODE=true
SIG_EARLY_MAX_AGE_MINUTES=30
SIG_EARLY_MIN_LIQUIDITY_USD=5700
SIG_EARLY_MIN_VOL_LIQ=0.070
SIG_EARLY_MIN_TXNS_5M=13
SIG_EARLY_MIN_BUY_RATIO=0.561
SIG_EARLY_MIN_UNIQUE_BUYERS=6
BSC_SIG_EARLY_MIN_LIQUIDITY_USD=13600
BSC_SIG_EARLY_MIN_VOL_LIQ=0.045
BSC_SIG_EARLY_MIN_TXNS_5M=8
BSC_SIG_EARLY_MIN_BUY_RATIO=0.74
BSC_SIG_EARLY_MIN_UNIQUE_BUYERS=5

# ── Entry window: this is a sniper, not a momentum chaser ───────────────────
MAX_MARKET_CAP_USD=200000

# ── The feedback loop. Nothing below gets fixed without this ────────────────
LOG_FEATURES=true
VERBOSE_LOGGING=true
NEAR_MISS_POINTS=15

# ── Watchlist for pools that go quiet and re-ignite ─────────────────────────
WATCHLIST_ENABLED=true

# ── Venues ──────────────────────────────────────────────────────────────────
REQUIRE_TRADEABLE_VENUE=false   # false = still alert, but say there is no route
```

### Two settings that are genuine trade-offs, not defaults

* **`ALLOW_SECURITY_FALLBACK=false`** is the safe choice, but it *drops* any
  BSC/ETH/Base pool GoPlus has not indexed yet — which is most pools in their
  first minutes. That is a direct recall cost on exactly the early entries this
  bot is for. `true` accepts honeypot.is only when it actually simulated the
  token. If early BSC/Base entries matter more than that risk, set `true`.
* **`SIGNAL_BONUS_WEIGHT`** (default `0.0` code / `0.5` example) lets the signal
  engine promote a token the score under-rates. Given the score is a coin flip,
  promotion is doing real work — but it is unvalidated, so raise it in steps and
  watch what arrives.

### What this profile does **not** fix

The buy button only works on pools the routers can reach. Currently that is
Uniswap v2/v3 on ETH/Base/Robinhood and PancakeSwap v2/v3 on BSC — about **20.6%
of births**, and **0 of 3** simulated early-lane alerts, because the young cohort
is dominated by Uniswap V4. Alerts on those pools arrive with the venue named and
the buy buttons withheld. See the README's *Tradeable venues* section.

---

## 3. Are the weights good?

The score is 100 points across ten hand-tuned tier functions. Only ~35 points are
testable from candle data; the rest need transaction counts, holder bands and
security lookups. Per block, measured:

| Block | Pts | AUC ≥10%/1h | AUC ≥25%/1h | AUC ≥25%/6h | Verdict |
|---|---|---|---|---|---|
| `vol_5m / liquidity` | 10 | untestable (needs liquidity history) | — | — | depth check; belongs in a filter, not the score |
| `vol_5m / vol_1h` | 8 | **0.499** | **0.424** | **0.470** | **pure noise — 8 points wasted** |
| `vol_1h / vol_6h` | 7 | 0.545 | 0.444 | 0.473 | weak, and worse than chance on big moves |
| `vol_6h / vol_24h` | 5 | 0.565 | 0.487 | 0.504 | weak |
| buy pressure 5m + 1h | 20 | not testable offline | — | — | **most promising, and unmeasured** |
| price 5m / 1h / 6h | 15 | **0.565** | **0.558** | **0.518** | the only block with real (still weak) signal |
| holders top10/50/100 | 20 | not testable offline | — | — | rewards concentration a distributed runner lacks |
| security | 10 | n/a | n/a | n/a | **non-discriminating**: every survivor passes it |
| CEX listings | 5 | n/a | n/a | n/a | late signal, and unreachable on Robinhood |

The headline: **the 15 price points alone rank as well as all 45 testable points
combined** (0.565 vs 0.568 at ≥10%), and *better* on the moves that matter
(0.558 vs 0.463 at ≥25%/1h; 0.518 vs 0.480 at ≥25%/6h). The 30 volume-ratio
points are buying essentially nothing.

Specific criticisms, in priority order:

1. **`vol_5m / vol_1h` (8 pts) is a coin flip at 0.499** and is below chance on
   larger moves. It is the clearest thing to cut.
2. **The whole volume-ratio family measures the wrong shape.** Each is a ratio of
   a short window to a longer one, so it rewards volume spread evenly and
   penalises volume concentrated into one bar. A pump *is* one bar, so the model
   penalises the signature of the thing it is hunting.
3. **Security (10 pts) should be a gate, not a score.** Every token that survives
   the hard honeypot/tax/flag checks gets the same 10 points, so it adds a
   constant to every candidate and 10% of dead scale. It discriminates nothing.
4. **CEX (5 pts) is backwards for a sniper.** A CEX listing means the token has
   already run. It cannot score on Robinhood at all.
5. **Holders (20 pts) rewards concentration** that a genuinely distributed runner
   does not have. It is also the block with the normalisation trap: making top-100
   *measurable* raises the bar for everyone, which measured out as making the case
   study **worse** by 2.6 points.
6. **Buy pressure (20 pts) is the block most likely to carry signal** — order-flow
   imbalance is a real predictor — and it is completely unmeasured here.

### Will it find runners while filtering false positives?

**Filtering: partly yes.** The binary safety gates (honeypot, tax,
`cannot_sell_all`, `hidden_owner`, proxy/mint/pause flags, unique-buyer ratio,
wash-trade checks) are real and do real work. That machinery is worth keeping.

**Ranking: no.** The measured AUC is ~0.57 for ≥10% moves, which is barely above
a coin flip, and **below** chance (0.42–0.49) for ≥25% moves. At 0.46–0.49 the
top-scoring tokens are slightly *less* likely to be the large movers than a random
pick. So as a gate it will both miss runners and admit false positives that happen
to look like calm accumulation.

### What to do about it, in order

1. **Do not reweight on intuition.** The current ~80 constants were fitted that
   way; adding another guess repeats the error. That is the whole reason
   `diag/fit_thresholds.py` refuses below 30 positives across 30 tokens.
2. **Turn on the data collection** (`LOG_FEATURES=true`, already in the profile).
   The `features` table currently has **0 rows**, so nothing can be fitted yet.
3. **Then let the evidence do the cuts.** The audit already supports one: the
   volume-ratio blocks, particularly `vol_5m / vol_1h`. A model fitted on labels
   will decide the rest, including the 55 unmeasured points.
4. **Treat the score as an explanation, not a forecast,** until it has been fitted
   and validated on a token-grouped split at a matched alert volume.
