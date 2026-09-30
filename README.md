# Sniper Bot

A multi-chain DEX pump/runner alert bot with manual trading over Telegram. It scans
BSC, Ethereum, Base and Robinhood chain, scores candidates, pushes alerts with
one-tap buy buttons, and manages open positions with a take-profit ladder and a
trailing stop. Paper mode is the default.

> This is a manual-trading assistant. It does **not** snipe new pairs and it does
> **not** auto-buy — every trade is a button tap.

## Features

- Scans continuously (default every 30s) for early runners and sends scored alerts.
- Hard filters for honeypots, taxes, and risky contract flags before scoring.
- Optional signal engine for runner/rug signals: unique buyers, wash trading,
  LP lock, dev holding, holder concentration, volume acceleration.
- Paste any contract address in chat to get a token card and buy buttons.
- Positions tracked in SQLite, with take-profit ladder + trailing stop and manual
  sell/buy-more/set-SL buttons.
- Paper or live trading, FastAPI `/health` endpoint for hosting.

## Quick start

```bash
python3.12 -m venv bot-env
source bot-env/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then edit it
./start.sh                # or: python bot.py
```

Minimum required in `.env`:

```dotenv
TELEGRAM_TOKEN=...        # from @BotFather
CHAT_ID=...               # your chat/user id
WALLET_PRIVATE_KEY=       # only needed for live trading
PAPER_TRADING=true
```

Everything else has a sane default. `.env.example` documents every variable.

## Configuration

The handful you are most likely to change. Two values matter per knob:
**code default** (what runs if `.env` omits it) vs **`.env.example` recommends**
(what the measured rollout uses). Your live `.env` predates most flags, so an
omitted key means the code default — currently the conservative/off side.

| Variable | Code default | `.env.example` recommends | Purpose |
|---|---|---|---|
| `PAPER_TRADING` | `true` | `true` | Simulate trades. Set to `false` only with a funded hot wallet. |
| `MIN_SCORE` | `65` | `65` | Alert threshold (0–100), scaled per chain — see [Alert gate](#alert-gate). |
| `SCORE_NORMALIZE` | `true` | `true` | Scale the alert bar to the points actually reachable on a chain/age. `false` gates on raw `MIN_SCORE`. |
| `MIN_EFFECTIVE_SCORE` | `35` | `45` | Floor for the scaled threshold, so scaling can't become a rubber stamp. At age ~3m the reachable ceiling is ~53, so `MIN_SCORE=55` scales to 29 and the floor is what actually gates young pools. `35` admitted a token at hand=35.0 exactly; `45` makes the early lane's AND-gates the way in instead. |
| `MAX_ALLOWED_TAX` | `0` | `10` | Reject tokens above this buy/sell tax %. |
| `ETHERSCAN_API_KEY` | — | — | Contract verification on all four chains, including Robinhood (chainid 4663). `SCANNER_API_KEY` is accepted as an alias. |
| `MORALIS_API_KEY` | — | — | Optional. Only used for an exact top-100 figure on BSC/ETH/Base. **Not** needed for holder scoring, and not supported on Robinhood. |
| `COINGECKO_API_KEY` | — | — | Enables CEX-listing scoring (never applies to Robinhood). |
| `USE_GECKOTERMINAL` | `false` | `true` | GeckoTerminal discovery. The DexScreener alternative is only the paid-boost shill list (`/latest/dex/pairs/{chain}` 404s), so prefer `true`. Code default is `false` (unchanged behaviour until you opt in). |
| `GT_SOURCES` | `new_pools,trending,top_volume` | `new_pools,trending,top_volume` | Sources for **every** chain. `new_pools` = the only early feed (carries a pool while it is minutes old and small). `trending` = momentum, but *lagging*: on Robinhood the youngest pool it offered was 234 min old at a median $3.5M mcap. `top_volume` = liquid universe (median age 24h, $12.4M). Dropping `new_pools` makes a sub-50k entry mathematically impossible — no threshold tuning can recover a pool the scanner never listed. |
| `DEXSCREENER_SOURCES` | `""` (off) | `boosts,boosts_top,profiles` | DexScreener boost/profile lists as a **second** source alongside GeckoTerminal — a union, not a switch. Measured 2026-09-30: these list tokens at a median **~24h old** (p25 112m, min 26m), so they are *not* earlier than `new_pools`; the value is the opposite — a much smaller list (~20 tokens per cycle across four chains), skewed small (median mcap ~$107k) and mostly on `uniswap`/`pancakeswap`, i.e. venues the routers can actually trade. Costs one call per 30 tokens. |
| `GT_SOURCES_<CHAIN>` | — (falls back to `GT_SOURCES`) | — | Per-chain override (also accepts `<CHAIN>_GT_SOURCES`). Use it to drop a feed on one chain, never to blind the early lane globally. `new_pools` was Robinhood-only until 2026-09-30, switched off on BSC after an **n=20** alert preview showed template-liquidity placeholders — the same error as tuning a floor on two tokens. It is now on every chain and the *filters* do the work, per chain. |
| `GT_PAGES_NEW` / `GT_PAGES_TRENDING` / `GT_PAGES_TOP` | `1` / `2` / `1` | `1` / `1` / `1` | Pages per source per chain. Page 1 of `new_pools` *is* the newest cohort, so depth there buys little. `trending` is one page in `.env.example` because the second page buys lagging $3.5M-median pools that the mcap ceiling rejects anyway, and that budget is better spent on births now that `new_pools` runs everywhere. |
| `GT_LIST_TTL_NEW` / `GT_LIST_TTL_TRENDING` / `GT_LIST_TTL_TOP` | `30` / `60` / `180` | `30` / `60` / `180` | Per-source list cache (s). `new_pools` churns a cohort every few minutes; `top_volume` barely moves, so a long TTL there saves budget for holder lookups. |
| `MAX_MARKET_CAP_USD` (+ per-chain) | `0` (= disabled) | `200000` | Alert **ceiling**. Without it the scanner alerts on $3M/$22M tokens that already ran. Verified to reject a $1.9M launch that `new_pools` surfaced. Code default leaves behaviour unchanged until you set it. |
| `NEAR_MISS_POINTS` | `15` | `15` | Tokens within this many points of the bar still log full score lines at INFO. The rest die silently into the features table — this is what makes `new_pools` + `VERBOSE_LOGGING=true` usable instead of spam. |
| `WATCHLIST_ENABLED` | `false` | `true` | Re-price pools that dropped out of the feeds: born quiet, runs later (the shape it targets — remember what was seen, re-price on a TTL, forget after expiry). DexScreener lookups, no GT budget cost. Code default is `false`. |
| `USE_SIGNALS` | `false` | `true` | Enable the signal engine. It only **removes** candidates by default (rejects + penalties), using unique-buyer data DexScreener doesn't provide. The **code default is `false`** — `.env.example` sets `USE_SIGNALS=true`, and you must copy that across or `signals.py` is never called and none of the `SIG_*` filters below do anything. |
| `SIGNAL_BONUS_WEIGHT` | `0.0` | `0.5` | Weight on the signal bonus. Code default `0.0` means the hand-tuned score stays the gate; a bonus can never create an alert. `.env.example` uses `0.5`. The structural reason it is non-zero: a clean distributed runner earns 0 holder and 0 CEX points on Robinhood, so its hand score cannot reach a bar scaled for a concentrated one. **This value is not outcome-fitted** — treat it as a declared policy and re-fit with `diag/fit_thresholds.py` once labelled data exists. Raising it re-opens the pass-2 flood vector — see [Catching re-ignited pools](#catching-re-ignited-pools). |
| `EARLY_RUNNER_MODE` | `false` | `true` | Lets a strong *young* pool alert (its long volume windows are empty, so it can't reach the threshold). Every AND-condition in `SIG_EARLY_*` must hold. Code default is `false`; without it a sub-50k pool structurally cannot alert. |
| `SIG_MAX_AGE_MINUTES` | `0` | `0` | `0` = **no upper age limit**. Pool age isn't a quality signal; the activity floors already reject dead pools. Set a number to restore a hard cap. |
| `ALLOW_SECURITY_FALLBACK` | `false` | `false` | `false` drops tokens GoPlus doesn't know (original behaviour). `true` accepts a simulated honeypot.is record instead. |
| `SIG_REQUIRE_OPEN_SOURCE` | `false` | `false` | Require a verified contract source. Leave `false` — most Robinhood tokens are unverified, including the ones that run. |
| `SIG_HOLDER_STANCE` | `pump` | `pump` | `pump` rewards concentrated supply (early runners); `rug` penalises it. |
| `<CHAIN>_SIG_EARLY_*` | — (falls back to the global `SIG_EARLY_*`) | BSC overrides only | Per-chain early-lane floors, so one chain can be objectively stricter without the early feed being disabled. The global values are the **Robinhood** cohort; BSC's p90 birth liquidity is ~$14.6k vs ~$5.4k, so BSC gets its own floors. Base (n=15) and Ethereum (n=2) deliberately get none — see [per-chain floors](#per-chain-early-floors). |
| `ROBINHOOD_MIN_SCORE` / `BASE_MIN_SCORE` etc. | — (falls back to `MIN_SCORE`) | `55` for Robinhood in `.env.example` | Per-chain threshold override, same convention as the floors below. Robinhood `55` because distributed-clean runners there earn 0 holder/CEX points. |
| `BASE_MIN_LIQUIDITY_USD` etc. | — | — (examples commented out) | Per-chain floors. Use these to tighten one noisy chain without changing the rest. |
| `ALLOWED_USER_IDS` | — | — | Extra Telegram allowlist when `CHAT_ID` is a group. |
| `LOG_FEATURES` | `false` | `true` | Log a feature row for every token evaluation (see [Training data](#training-data)). Code default is `false`; example turns it on because the table is the tuning signal. |
| `TRY_BLOCKSCOUT_HOLDERS` | `false` | `false` | Retry Blockscout for Robinhood holders. Off because that host answers with a Cloudflare challenge. |
| `REQUIRE_TRADEABLE_VENUE` | `false` | `false` | Reject a pool whose DEX the configured routers cannot reach (Uniswap V4, V3 forks, Pons, Aerodrome) before spending enrichment budget. `false` still alerts but names the DEX and withholds buy buttons. See [Tradeable venues](#tradeable-venues--do-you-need-uniswap-v4). |
| `TRADEABLE_DEXES` / `<CHAIN>_TRADEABLE_DEXES` | — (built-in per-chain patterns) | — | Regex patterns (matched against the feed's `dexId`) for venues you have added routers for. Overrides the built-in uniswap v2/v3 (eth/base/robinhood) and pancakeswap v2/v3 (bsc) defaults. |

Per-chain routing addresses (`ETH_QUOTER_V2`, `BSC_ROUTER_V3`, …) can be
overridden in `.env`, but working defaults are compiled in for all four chains.
`/debug` prints each contract as `OK` or `NO CODE` so a bad address is obvious.

## Catching re-ignited pools

A pool can sit dead for weeks and then run on a catalyst. Two things used to
make those invisible:

1. **A hard 5-day age cap** (`SIG_MAX_AGE_MINUTES`). It rejected on creation date
   rather than on current life. Measured across 122 live pools: 91 were older than
   5 days and 4 were rejected on age alone — including a **27-day-old pool up
   40,605% in 24h** and an **8-day-old pool up 2,533%**. The cap is now `0`
   (disabled); dead pools are still rejected by `vol5m_too_low`,
   `low_activity` and `txns5m`, which measure *current* activity. Set
   `SIG_MAX_AGE_MINUTES=7200` to restore the old behaviour.
2. **`SIGNAL_BONUS_WEIGHT` did nothing.** The gate read the hand score, which
   never contains the bonus, so at *any* weight the bonus could not promote. The
   gate is now `total_score` — identical at the `0.0` default, but the knob is
   live above it.

Removing the age cap is necessary but not sufficient: the hand score measures
*short-term volume concentration*, so a pool whose volume is spread across the
hour still scores low. The 27-day pool above scored hand 42 against a bar of 58,
with a strong signal bonus of +26 that could not lift it past the hand gate. To
actually catch that pattern you must also lower the bar and let signals promote:

```dotenv
SIG_MAX_AGE_MINUTES=0        # already the default
ROBINHOOD_MIN_SCORE=45       # lower the per-chain bar
SIGNAL_BONUS_WEIGHT=1.0      # let signals promote
```

With that combination the same pool alerted (score 54 vs bar 40.2). **This is a
noise trade-off, not a free win** — pass 2 of this repo had to be reverted because
an additive bonus flooded Base with junk. Raise `SIGNAL_BONUS_WEIGHT` in small
steps and watch what arrives. `VERBOSE_LOGGING=true` prints
`score (hand=X/threshold)` on every evaluation so you can see the margin.

That block is the **aggressive profile** (lower bar *and* full promotion). The
`.env.example` default is the moderate one — `ROBINHOOD_MIN_SCORE=55` with
`SIGNAL_BONUS_WEIGHT=0.5` — which keeps the bar honest while still letting a
clean distributed runner through. The two are not in conflict; one is a
haircut, the other is a rewrite, and the table under
[Configuration](#configuration) always lists both the code default and what
`.env.example` recommends.

## Tradeable venues — do you need Uniswap V4?

**Yes, if you want to trade what the early feeds actually list.** The bot holds a
Uniswap V3 router + V2 router on Ethereum/Base, a PancakeSwap V3 + V2 router on
BSC, and only a V3 router on Robinhood. A router can only route pools created by
its *own* factory, so:

* **Uniswap V4 is not reachable at all.** V4 swaps go through the Universal
  Router + Permit2, which this bot does not implement. No fee-tier setting makes
  a V4 pool quotable through a V3 router.
* **Every V3 fork needs its own router.** Aerodrome (Base), Pons, `up-v3`,
  Ramses and PancakeSwap-Infinity pools are not routable through a Uniswap or
  PancakeSwap router.

Measured 2026-09-30 on GeckoTerminal page 1 (20 pools per feed) — the share of
pools the configured routers can execute on:

| Chain | Feed | Routable | What the rest is |
|---|---|---|---|
| Robinhood | `new_pools` | **0%** | `pons-v2` 14/20, `uniswap-v4-robinhood` 6/20 |
| Base | `new_pools` | **25%** | `uniswap-v4-base` 8/20, bankr 4, aerodrome 2, o1 1 |
| BSC | `new_pools` | **10%** | `uniswap-v4-bsc` 14/20, four-meme 4 |
| Ethereum | `new_pools` | **10%** | `uniswap-v4-ethereum` 18/20 |

The earliest feed on every chain is therefore mostly unroutable today. That is
why the bot now reports venue honestly instead of offering buttons that fail:

* every evaluation records `dex_id` and `venue_tradeable` in the `features` table;
* an alert on an unroutable pool **names the DEX, says there is no route, and
  withholds the buy buttons** (there is nothing to tap that could work);
* `REQUIRE_TRADEABLE_VENUE=true` rejects such pools before any enrichment budget
  is spent — note this silences Robinhood's early lane entirely at 0% routable;
* `TRADEABLE_DEXES` / `<CHAIN>_TRADEABLE_DEXES` override the recognised patterns
  when you add a router for another venue.

Ranked by value if you want to widen coverage: **(1) Uniswap V4 via the Universal
Router** (the single biggest gap — 30–90% of early pools per chain), **(2) a
Robinhood V2 router** (unlocks `pons-v2`, the dominant Robinhood `new_pools`
venue), **(3) Aerodrome on Base**. Item 2 is only a config change *if* you have a
verified Pons router address — do not guess one.

#### Why V2 + V3 + V4 routers on every chain is still not enough

The unit of trading coverage is the **DEX**, not the router version. A router
routes only pools created by **its own factory**, so:

* a Uniswap V3 router on Base reaches *Uniswap V3 Base* pools and nothing else —
  not Aerodrome, not `up-v3`, not PancakeSwap Infinity;
* a PancakeSwap router on BSC reaches *PancakeSwap* pools, not `four-meme`;
* Uniswap V4 is a different architecture entirely (below), not a fourth router
  address you can add to the existing V2/V3 code paths.

Verified on-chain 2026-09-30, not asserted: for token `SI`
(`0x5ea8f2c761c5da750eca48e5900c0639c8ed2c9b`), which trades actively on
Aerodrome, the Uniswap V3 factory on Base
(`0x33128a8fC17869897dcE68Ed026d694621f6FDfD`) returned
`0x0000000000000000000000000000000000000000` for **every** fee tier
(100 / 500 / 3000 / 10000) and **both** quotes (WETH, USDC). Nothing to route
through, even with a V3 router and quoter configured and working.

**What V4 actually requires.** All V4 pools live in a single `PoolManager`
singleton; a "pool address" is a 32-byte `PoolId`, not a contract. Confirmed by
reading `PoolManager.extsload` on Base — the pool id GeckoTerminal reports holds
initialised state in the PoolManager's `_pools` mapping. Consequences:

| Requirement | Why |
|---|---|
| Universal Router (command encoding) | V4 swaps are dispatched by opcode, not a simple `exactInputSingle` call |
| Permit2 approval flow | The Universal Router pulls tokens through Permit2, so approval is two-step |
| `PoolKey` = (currency0, currency1, fee, tickSpacing, hooks) | Needed to build the swap — and **GeckoTerminal publishes only the `PoolId` (its hash)**, no fee/tickSpacing/hooks. It must be recovered from `Initialize` logs or an indexer. |
| Native ETH as `currency0 = address(0)` | ETH pools are not WETH pools in V4 |
| A hooks review per pool | A pool may attach a hook contract with custom logic (fees, transfer restrictions). Attempts to reconstruct keys with `hooks = 0` over standard fee tiers failed for 8/8 sampled Base V4 pools, which is consistent with launchpad pools attaching hooks. Hooked pools cannot be assumed to behave like a plain AMM. |

So the honest sequencing for "buy on all chains" is: Universal Router + Permit2 +
`PoolKey` recovery first (unlocks V4 and every V4-based launchpad — `bankr`,
`o1-launchpad`), then per-DEX routers (Pons on Robinhood, Aerodrome on Base,
`four-meme` on BSC), each needing a *verified* address. This is not a config
change, and it should not be written blind: the `PoolKey` source and hooks
handling need a real log/indexer endpoint before any transaction path is built.

Until then the bot stays honest: alerts name the DEX and the exact missing
integration (`Venue requirement` in the `features` table), and withhold buy
buttons rather than offering ones that fail.

#### Both sources, and what to do about `new_pools` spam

Discovery is now the **union** of GeckoTerminal and DexScreener
(`DEXSCREENER_SOURCES`). It used to be either/or, so turning on the early feed
silently turned off the curated list. Measured 2026-09-30:

| Source | Typical age at listing | List size | Character |
|---|---|---|---|
| GT `new_pools` | **3–5 minutes** | hundreds/cycle | mostly Uniswap V4 → unroutable |
| DS boosts/profiles | median **~24h** (p25 112m, min 26m) | **~20 across 4 chains** | median mcap ~$107k; mostly `uniswap`/`pancakeswap` |

So DexScreener is *not* earlier than `new_pools` — it is much later. If you
remember being "genuinely early" on it, you were early relative to
**competition**, not relative to birth: the list is tiny and few bots watch it,
and its tokens are small ($107k median mcap, 13/21 inside the <200k window) and on
tradeable venues.

If `new_pools` spam is the problem, there are two fixes and they are not
equivalent:

* **(a) Drop it** — `GT_SOURCES=trending,top_volume`. You lose the only
  3–5-minute source; earliest entry becomes ~26m at best, median ~24h.
* **(b) Keep it and stop the spam reaching you** — `REQUIRE_TRADEABLE_VENUE=true`.
  Most `new_pools` candidates are Uniswap V4 and cannot be bought anyway, so they
  are rejected before enrichment and never alert. Early entries on tradeable
  venues survive.

(b) is usually what is wanted, and it costs nothing but the alerts you could not
have acted on.

#### Is Uniswap enough for ETH/Base/Robinhood, and PancakeSwap for BSC?

##### Will the buy button actually work on what it alerts on?

**No — right now, essentially none of it.** Of the births the scanner sees, most
are Uniswap V4, which the bot cannot route. Measured on a fresh birth sample
(2026-09-30, `new_pools` pages 1–2, age-eligible for the early lane):

| Chain | Births | Young enough | Pass floors | Early-lane alerts | Tradeable |
|---|---|---|---|---|---|
| Ethereum | 40 | 8 | 2 | 1 | 0 |
| Base | 40 | 29 | 1 | 1 | 0 |
| BSC | 40 | 40 | 4 | 0 | 0 |
| Robinhood | 20 | 20 | 2 | 1 | 0 |
| **Pooled** | 140 | **97** | **9** | **3** | **0 (0%)** |

All three would-be alerts were Uniswap V4 pools, so all three would have arrived
with **no route and no buy buttons** — the bot now says so explicitly rather than
offering a button that fails at quote time.

Two honest caveats. The sample is small (3 alerts), so treat 0% as "very few"
rather than a precise rate; the broader coverage measurement above agrees in
direction (20.6% of all births routable, V4 dominating the young cohort). And
this simulates the **early lane only** — the momentum lane needs live
holder/security/score lookups that cannot be reproduced offline.

The practical upshot: the corrected joint floors did open the lane (3 alerts where
the independent-p90 version produced 0), but **Uniswap V4 is now the gate on
actually trading any of it**.

Reproduce:

```bash
bot-env/bin/python diag/alert_tradeability.py --pages 6
```

#### Do launchpad tokens later get an AMM pool — and how early?

Sometimes, and it is chain-dependent — this is the one place the "Uniswap will
pick it up soon" intuition holds. Measured with `diag/launchpad_migration.py`
2026-09-30, age-stratified so right-censoring cannot flatter the result:

| Chain | Birth venue | n | Migrated to an AMM | Median lag after birth pool | Destination |
|---|---|---|---|---|---|
| Robinhood | `pons-v2` | 17 | **76%** | **4 min** (p25 2m, p75 12m) | `uniswap-v4-robinhood` (13/13 cases) |
| BSC | `four-meme` | 21 | **5%** | ~37 h (single case) | — |
| Base | `bankr` | 18 | 22% | 54 min (p25 16m, p75 123m) | — |

By age bucket — the share of tokens *of that age* that have an AMM pool:

| Venue | <1h | 1–6h | 6–24h | 1–7d | >7d |
|---|---|---|---|---|---|
| `pons-v2` (RH) | 100% | 60% | 80% | 80% | — |
| `four-meme` (BSC) | 0% | 0% | 0% | 20% | 0% |
| `bankr` (Base) | 0% | 0% | 0% | 25% | 75% |

What this means for the strategy:

* **Robinhood: waiting for the AMM is genuinely viable.** Pons tokens migrate to
  `uniswap-v4-robinhood` with a median lag of **4 minutes**, so implementing
  Uniswap V4 on Robinhood covers ~76% of pons births after a ~4-minute delay —
  close to the value of integrating Pons directly. 24% never migrate, so it is
  not a full substitute.
* **BSC: no.** `four-meme` tokens essentially do not migrate (1/21), and the one
  that did took ~37 hours. Waiting for PancakeSwap is not a strategy there;
  `four-meme` has to be integrated, or those tokens skipped.
* **Base: not early enough to help.** `bankr` migrates 22%, median ~54 minutes,
  and **0% within 24 hours** in this sample (migration concentrates in tokens now
  older than a week). A 54-minute-plus delay is past the entry the bot exists for.

Caveats, stated because these samples are small: n=17–21 per venue, so treat the
rates as indicative and the *lags* as the more robust part (the pons result is
tight and single-destination, which is why it is the most trustworthy). Tokens
that died early and were never indexed are absent, and a pool GT does not index
is invisible here.

Reproduce:

```bash
bot-env/bin/python diag/launchpad_migration.py --chain robinhood --dex-pools pons-v2 --per-bucket 5
bot-env/bin/python diag/launchpad_migration.py --chain bsc --dex-pools four-meme --per-bucket 5
```

No — that holds on Ethereum, roughly on Base, and fails on BSC and Robinhood.
Measured with `diag/dex_coverage.py` over **480 births, 120 per chain, 6 pages
each, zero failed pages** (2026-09-30). Coverage of `new_pools`, i.e. "can the
bot buy this pool at birth":

| Scenario | Ethereum | Base | BSC | Robinhood | Pooled |
|---|---|---|---|---|---|
| **A.** configured today (uni v2/v3; pancake v2/v3) | 25.8% | 18.3% | 36.7% | **1.7%** | 20.6% |
| **B.** A + Uniswap V4 everywhere | **97.5%** | **81.7%** | 58.3% | 17.5% | **63.7%** |
| **C.** "uniswap for eth/base/rh, pancake for bsc" | 97.5% | 81.7% | **41.7%** | **17.5%** | 59.6% |
| **D.** C + Uniswap V4 on BSC | 97.5% | 81.7% | 63.3% | 17.5% | 65.0% |

What the birth feed actually contains:

| Chain | Top venues (share of births) |
|---|---|
| Ethereum | `uniswap-v4-ethereum` 71.7%, `uniswap_v2` 22.5%, `uniswap_v3` 3.3% |
| Base | `uniswap-v4-base` 63.3%, `uniswap-v2-base` 18.3%, `o1-launchpad` 8.3%, `aerodrome-slipstream-3` 4.2%, `bankr` 3.3% |
| BSC | `four-meme` **35.8%**, `pancakeswap_v2` 33.3%, `uniswap-v4-bsc` **21.7%**, `pancakeswap-infinity-clmm` 5.0%, `pancakeswap-v3-bsc` 3.3% |
| Robinhood | `pons-v2` **80.0%**, `uniswap-v4-robinhood` 15.8%, `bankr-robinhood` 2.5% |

Three corrections to the "99%" intuition:

1. **It is right on Ethereum** (97.5% Uniswap-branded) — which is presumably
   where the impression came from. It does not generalise.
2. **On BSC, PancakeSwap is no longer where pools are made.** PancakeSwap-branded
   venues are 41.7% of births; `four-meme` alone is 35.8%, and Uniswap V4 on BSC
   is 21.7%. A PancakeSwap-only BSC config reaches 41.7%, not ~99%.
   PancakeSwap *Infinity* (5.0%) is also a separate product needing its own
   router, not something the existing V2/V3 code covers.
3. **Robinhood is overwhelmingly not Uniswap.** `pons-v2` is 80% of births.
   Neither V4 nor a Uniswap-only inventory helps much there: scenario B moves
   Robinhood from 1.7% to 17.5%.

Ranked by measured marginal value:

| Addition | Gains |
|---|---|
| Uniswap V4 (Universal Router + Permit2) | pooled 20.6% → 63.7%; +71.7 pts on Ethereum, +63.3 on Base, +21.7 on BSC, +15.8 on Robinhood |
| Pons router (Robinhood) | +80 pts on Robinhood (17.5% → ~97%) |
| `four-meme` (BSC) | +35.8 pts on BSC |
| Aerodrome (Base) | +4.2 pts on Base |
| PancakeSwap Infinity (BSC) | +5.0 pts on BSC |

**Caveat on what this measures.** These are venues where pools are *created*.
Several are launchpads (`four-meme`, `bankr`, `o1-launchpad`): at birth their
tokens trade on the launchpad's own venue, in a single pool — sampled
`four-meme` tokens each had exactly one pool, on `four-meme`, with no PancakeSwap
pool alongside it. So "covered" means *buyable at birth*, which is precisely what
an early-entry bot needs, but it is not the same as "buyable ever": a token may
graduate to an AMM later, by which point the early entry is gone.

Reproduce any row:

```bash
bot-env/bin/python diag/dex_coverage.py --pages 6 --pause 2.8
bot-env/bin/python diag/alert_tradeability.py --pages 6
```

## "Why did it ping me at $3M instead of $300k?"

Two opposite causes, and they need opposite fixes:

* **discovery never saw it small** — the feed only listed the pool after the move;
* **the gate held it back** — it was seen small and rejected until the score caught up.

Every alert now answers this directly, so it does not need a log grep:

```
🕒 Age: 52 min

👀 First seen: 47m ago at $62,000 mcap -> now $3,100,000 (50.0x)
```

If `First seen` is minutes ago at a small mcap, discovery was early and the
**gate** is the problem. If it is at a large mcap, **discovery** is the problem
(a wrong `GT_SOURCES`, or `USE_GECKOTERMINAL` off). `_first_sight` is recorded
before any floor can reject a token, and `first_sight_mcap` /
`first_sight_age_minutes` / `first_sight_ts` are stored on every `features` row.

### The most likely cause: a deployment running code defaults

Nearly every guard in this bot is opt-in, so its **code defaults are "off"**. A
deployment that sets only `TELEGRAM_TOKEN` and `CHAT_ID` therefore runs:

| Setting | Code default | Consequence |
|---|---|---|
| `USE_GECKOTERMINAL` | `false` | discovery is the DexScreener boost/profile list — it cannot see a pool while it is minutes old |
| `USE_SIGNALS` | `false` | no rug/wash-trade/unique-buyer gates, and the early lane is disabled |
| `MAX_MARKET_CAP_USD` | `0` | **no alert ceiling** — it will alert on $3M and $22M tokens that already ran |
| `EARLY_RUNNER_MODE` | `false` | a young sub-50k pool can never alert |
| `LOG_FEATURES` | `false` | no training rows, so the score can never be refitted |
| `MIN_SCORE` | `65` | unreachable on a young pool (ceiling ~53) |

That combination reproduces exactly: alerts on multi-million-mcap tokens, false
positives on wash-traded pools, and no early entries. **`bot.py` now prints these
at startup and exposes them at `/health` as `config_warnings`**, because the
difference between the documented bot and a defaulted one is otherwise invisible
from the outside. Set them in the *deployment's* environment (e.g. the Render
dashboard) — a local `.env` does not affect a hosted instance.

## Detection latency — "why do I only get pinged after the pump?"

Being late is two different failures that look identical in a log full of reject
reasons, and they need opposite fixes:

* **discovery latency** — the bot never *looked* at the pool until it was big; or
* **gate latency** — the bot looked early and the score/threshold said no.

A hard mcap ceiling or looser filters only address the second. If the feed never
carried the pool while it was small, no amount of filter tuning can help.

**The feeds are not interchangeable.** Measured on Robinhood Chain 2026-09-29:

| Source | Age of pools offered | Median mcap |
|---|---|---|
| `new_pools` page 1 | **3.2 – 5.5 min** | **$6,663** (15/20 under $50k) |
| `new_pools` page 3 | 7.1 – 9.2 min | $4,687 |
| `trending` pages 1–3 | 234 min – 133,194 min (median ~31 days) | $3,537,366 |
| `top_volume` page 1 | median 24 h | $12,407,781 |

`trending` and `top_volume` are *lagging*: the youngest pool trending could offer
was 234 minutes old and already past $300k. Running them without `new_pools` makes
a sub-50k entry **mathematically impossible** — the pool only ever enters the
pipeline after the move that made it trend. That is the single most common cause
of "it pings me at $3M".

Two structural guarantees now protect latency:

1. **Discovery runs for every chain before any evaluation.** The loop used to
   interleave discover→evaluate per chain, and because holder lookups draw on the
   same GeckoTerminal budget as discovery, the last chain in `NETWORKS`
   (`robinhood`) was not even *listed* until minutes into the cycle. Discovery is
   now Phase A for all chains; enrichment is Phase B.
2. **Page depth is per source, and the early feed is global.** `new_pools` runs on
   every chain (`GT_PAGES_NEW=1`: page 1 *is* the newest cohort, so depth there
   buys little). `trending` is one page in `.env.example`, down from two — the
   second page bought lagging $3.5M-median pools that the mcap ceiling rejects,
   and that budget is better spent on births now that the early feed covers all
   four chains (`GT_PAGES_NEW=1`, `GT_PAGES_TRENDING=1`, `GT_PAGES_TOP=1`).

**Budget, since four chains now pay for `new_pools`.** The limiter is shared at
`GT_MIN_INTERVAL_S=2.1` (~28/min), so this is the constraint that decides whether
"earlier everywhere" is real or just a longer cycle:

| Config | Calls / 30s cycle | Paced time |
|---|---|---|
| `new_pools` on Robinhood only, `trending` 2 pages (old default) | ~5.7 | ~12s |
| **`new_pools` on all chains, `trending` 1 page (current)** | **~6.7** | **~14s** |
| `new_pools` all chains, `trending` 2 pages | ~8.7 | ~18s |

All three fit inside the 30s interval, so enabling the early feed everywhere
costs about 2s of paced calls per cycle rather than pushing the cycle over.
Raising `GT_PAGES_TRENDING` back to 2 leaves ~12s of headroom for holder lookups.


**Read the latency straight off the log.** Every token is logged once, before any
floor can reject it:

```
FIRST SIGHT SYMBOL@robinhood age=2.0m mcap=$12,256 liq=$13,919 v5=$3,551 [geckoterminal:new_pools]
```

That line is the answer to "was I early?": `age` is how long the pool had existed
at first sighting, `mcap` is what it was worth then. If first sightings cluster at
`age=180m mcap=$3M`, discovery is the problem (wrong `GT_SOURCES`). If they cluster
at `age=3m mcap=$6k` and you still get no alert, the gate is the problem — and
`NEAR_MISS_POINTS` plus the `features` table will show which component withheld
the points.

## Alert gate

### How good is the score? Measured, not assumed

It has almost no rank power for forward returns, and *negative* rank power for the
large moves the bot exists to catch. Re-run at HEAD with
`diag/component_audit.py` (4,550 observations, 49 pools, Base 5-minute candles):

| Forward move | positives | AUC of the tested components (0.5 = coin flip) |
|---|---|---|
| ≥ 10% in 1h | 66 | **0.568** |
| ≥ 25% in 1h | 8 | **0.463** |
| ≥ 25% in 6h | 89 | **0.480** |
| ≥ 50% in 6h | 13 | **0.487** |
| ≥ 100% in 6h | 1 | 0.461 (one positive — meaningless) |

Rank correlation is ~0.02–0.09, and in economic terms the components are worth
**+0.1 to +0.4 points** of forward 1-hour return (1.5% when a component fires vs
1.4% when it does not). The one exception, `score_price`, shows +4.7 points — on
**42 observations**, and it is a momentum term, i.e. buying what already moved.

Two caveats that bound this, stated so it is not over-read:

* It tests the **candle-derivable** components only — `vol_5m_1h`, `vol_1h_6h`,
  `vol_6h_24h`, `score_price` ≈ 35 of the 100 points. Buy pressure (20), holders
  (20), security (10) and CEX (5) cannot be reconstructed from candles. They
  measure the same "recent accumulation" character, so the untested blocks are
  not obviously better, but they are untested here.
* The pool universe is GeckoTerminal trending/top-volume, i.e. already selected.
  The per-pool sign test is robust to that for *within-pool* comparison; the
  headline AUC is not a claim about all pools.

**Why it fails structurally**, independent of the numbers: every volume component
is a *ratio of a short window to a longer one*, which rewards volume spread
evenly across the period and penalises volume concentrated into one bar. A pump
is a step function, so the model penalises precisely the shape it is looking for.
It also looks strictly backwards: at the ignition bar of the historical case study
the model awarded 3.0 of 35 candle-derivable points while the next hour returned
+1,202%. On top of that the holder block (up to 20 points) rewards concentration
that a genuinely distributed runner does not have.

**What is actually sound in the pipeline** — worth keeping separate from the
score, because it is a different mechanism:

* the hard safety gates (honeypot, tax, `cannot_sell_all`, `hidden_owner`,
  risky-flag rejects, unique-buyer and wash-trade gates) — binary and real;
* attribution: the `score_*` columns sum to `hand_score`, pinned by a test, so the
  model is auditable rather than a black box;
* the early lane's AND-gated design, which cannot be gamed by one strong metric
  the way an additive score can;
* `SCORE_NORMALIZE`, which fixed a genuine "no alerts at all" bug.

The honest conclusion is that the score should be a **feature, not the gate**.
Ranking is the part that needs fitting on labelled outcomes
(`diag/fit_thresholds.py`), and that currently refuses because the `features`
table is empty. Until then, treat the score as an explanation of why a token was
picked, not as evidence that it will move.

### Per-block weight audit

Only ~35 of the 100 points are testable from candle data (the rest need
transaction counts, holder bands and security lookups). Measured per block:

| Block | Pts | AUC ≥10%/1h | AUC ≥25%/1h | AUC ≥25%/6h |
|---|---|---|---|---|
| `vol_5m / liquidity` | 10 | untestable | — | — |
| `vol_5m / vol_1h` | 8 | **0.499** | **0.424** | **0.470** |
| `vol_1h / vol_6h` | 7 | 0.545 | 0.444 | 0.473 |
| `vol_6h / vol_24h` | 5 | 0.565 | 0.487 | 0.504 |
| buy pressure 5m + 1h | 20 | untestable | — | — |
| price 5m / 1h / 6h | 15 | **0.565** | **0.558** | **0.518** |
| holders top10/50/100 | 20 | untestable | — | — |
| security | 10 | non-discriminating — every survivor passes it | | |
| CEX listings | 5 | late signal; unreachable on Robinhood | | |

**The 15 price points alone rank as well as all 45 testable points combined**
(0.565 vs 0.568 at ≥10%) and better on the moves that matter (0.558 vs 0.463 at
≥25%/1h). The 30 volume-ratio points buy essentially nothing, and
`vol_5m / vol_1h` (8 pts) is a literal coin flip.

Note also what that implies about the *shape* the model rewards: each volume
component is a ratio of a short window to a longer one, so it favours volume
spread evenly and penalises volume concentrated into a single bar — which is what
a pump looks like. See [`RECOMMENDED_ENV.md`](RECOMMENDED_ENV.md) for the
settings profile and the ordered fix list.

### Notes on reachability

The score is out of 100, but those 100 points are only meaningful if they are all
*earnable* — and they are not:

| Points | Why they can be unreachable |
|---|---|
| 20 — holder concentration | Blocked by provider: Robinhood's Blockscout instance returns a Cloudflare challenge, and the Moralis free tier is easily suspended. Now sourced from GeckoTerminal, but GT publishes no 51–100 band, so 4 of the 20 stay unmeasurable. |
| 5 — CEX listings | No Robinhood token is listed on CoinGecko, so this can never score there. |
| 7 + 5 — 1h/6h and 6h/24h volume tiers | Age-gated: a pool younger than 1h (or 6h) has no such window to measure. |

With `SCORE_NORMALIZE=true` the bar becomes
`MIN_SCORE × (reachable points ÷ 100)`, floored at `MIN_EFFECTIVE_SCORE`. Without
it, a young Robinhood runner was being asked for 65 out of a reachable ~43, which
is why the bot could run for days and never alert. `VERBOSE_LOGGING=true` logs
the effective threshold on every evaluation.

## Data sources

| Data | Source | Notes |
|---|---|---|
| Discovery | GeckoTerminal | Free, keyless. Gives unique buyers/sellers, which DexScreener does not. |
| Security / tax | GoPlus (+ honeypot.is fallback) | BSC, Ethereum, Base. |
| Security / verification | Etherscan v2 | All four chains incl. Robinhood (4663). Free tier covers `getsourcecode`. |
| Holder concentration | GeckoTerminal | All four chains. Moralis is used first on BSC/ETH/Base only when it answers, because it alone gives an exact top-100. |
| CEX listings | CoinGecko | Never applies to Robinhood. |

### Why so much comes from GeckoTerminal

Holder concentration is the one input where every alternative is either paid or
does not cover Robinhood Chain (4663):

| Provider | Robinhood (4663)? | Holder endpoint | Free tier |
|---|---|---|---|
| **GeckoTerminal** | yes | top-10 / 11-30 / 31-50 bands | **free, keyless, no signup** |
| Etherscan | yes | PRO only | free for verification |
| Moralis | **no** | — | trial; free usage is easily "paused" |
| GoldRush (Covalent) | yes | top holders | **14-day trial**, then $10/mo |
| Bitquery | yes | `EVM.Holders` top-N | **10K points, first month only** |
| Ankr | **no** | — | Premium plan only |
| GoPlus | **no** | — | free, but no Robinhood |

So the bot deliberately does not depend on a trial that will expire. GeckoTerminal
publishes `top_10`, `11_30` and `31_50` — enough for exact top-10 and top-50, but
**no 51-100 band**, so `top100` stays unmeasured (`None`) rather than guessed and
the alert gate scales down for those 4 points. Ethereum holders also come from
GeckoTerminal today because a suspended Moralis key returns 401 and would
otherwise zero the score on every chain.

GeckoTerminal's free tier is ~30 calls/min, so discovery and holder lookups share
one limiter (`GT_MIN_INTERVAL_S`, default 2.1s ≈ 28/min). A burst gets HTTP 429.

### How the security check works (and why new pairs are dropped)

GoPlus does not index a pool the moment it is born, and the bot treats "not
indexed" as **unknown, not safe**. Per chain:

| Chain | Provider | What happens when there is no record |
|---|---|---|
| BSC / Ethereum / Base | GoPlus `token_security` | `None` → token **dropped** as `security_unknown`. With `ALLOW_SECURITY_FALLBACK=true`, honeypot.is v2 is tried, and accepted **only** if it actually simulated (`simulationSuccess` + `honeypotResult`); otherwise still dropped. |
| Robinhood | Etherscan v2 `getsourcecode` (Blockscout fallback) | No GoPlus and no honeypot.is chain id exist, so **no honeypot/tax check runs at all**. An unverified contract is a −8 penalty, not a reject. |

Two consequences worth knowing before you tune anything:

1. **GoPlus lag is a silent recall ceiling on BSC/ETH/Base.** A pool minutes old
   can clear the floors, the signal gates and the score, then die at
   `security_unknown`. `/health` now reports `security_unknown_rejects` so you
   can see how often, and the failed-lookup cache is 120s (vs 1800s for a good
   record), so a token becomes eligible as soon as GoPlus catches up rather than
   staying hidden for half an hour.
2. **Robinhood's early lane has no honeypot protection.** Security there means
   "Etherscan says whether the source is verified". That is why the early lane's
   AND-gates (unique buyers, txns, buy ratio, liquidity) carry the rug-filtering
   weight on that chain — they are the only protection present.

`ALLOW_SECURITY_FALLBACK=true` is the lever for trading unindexed BSC/ETH/Base
pairs. It is off by default because accepting an unanalysable token as "safe" is
what flooded Base with noise in pass 2.

### Where these numbers come from

Two different things are at work in this repo, and they are not interchangeable.

**Population-derived thresholds (the defaults).** The early-lane floors are
quantiles of the population the lane actually receives, produced by
`diag/population_floors.py`. That is objective in the sense that matters:
reproducible, outcome-blind, and a property of the cohort rather than of any
named token. Nothing in the derivation reads price or subsequent performance,
which is what keeps it free of survivorship bias.

The lane only ever fires on young pools, and young pools only arrive from
`new_pools`, which the shipped config enables on Robinhood alone — so the
relevant cohort is Robinhood `new_pools`, not a cross-chain pool:

```bash
bot-env/bin/python diag/population_floors.py \
    --chains robinhood --sources new_pools --pages 2 --quantile 0.90
```

Measured 2026-09-30, **n=40** births, floor at p90 ("keep the most active decile"):

| Metric | p50 | p75 | p90 | Floor @ p90 | 95% CI |
|---|---|---|---|---|---|
| Liquidity | $4,477 | $4,806 | $5,402 | **$5,400** | $4.9k – $7.2k |
| vol / liq | 0 | 0.0011 | 0.112 | **0.112** | 0.003 – 0.276 |
| txns 5m | 0 | 2.25 | 20.3 | **21** | 2.9 – 39 |
| buy ratio | 0 | 0.042 | 0.574 | **0.574** | 0.05 – 0.77 |
| unique buyers | 0 | 0.25 | 12.2 | **13** | 1 – 16 |

Three findings worth stating plainly:

1. **The population is mostly dead.** Median txns, buyers and vol/liq are all
   zero. Any floor above ~p75 is selecting a small tail, so *the quantile choice
   is the alert-volume policy* — not a fact to be discovered.
2. **The intervals are wide** (txns 2.9–39, buyers 1–16). n=40 cannot pin these
   tightly. The values this replaced were derived from **n=2**, which cannot pin
   them at all.
3. **The old floors were not what their own comment claimed.** They were
   described as the "upper quartile"; measured against this population, txns 20
   was p90, buyers 15 was *above* p90 (p90 = 12.2), and liquidity 10,000 sat
   beyond the sample maximum of $10,090 — roughly p99. A liquidity floor above
   almost every pool that exists is a sufficient explanation for early entries
   never firing, and it is exactly the error that picking tokens produces.

**Outcome-fitted thresholds (none exist yet).** A quantile says "this is a strong
pool for its cohort". It does not say the pool goes up — `diag/component_audit.py`
measured the underlying components at AUC ≈ 0.59 for a ≥10% move and ≈ 0.24 for
≥100%, i.e. near-useless for the tail this bot is hunting. Thresholds that claim
predictive power must be fitted on realised forward returns, and
`diag/fit_thresholds.py` does that with a hard gate: it **refuses to emit any
recommendation** below 30 positive outcomes across 30 distinct tokens, splits by
time *and* by token, and reports the base rate next to every precision figure.
Run today it refuses, because the `features` table is empty — which is the honest
state of affairs, not a missing feature.

To get there: `LOG_FEATURES=true`, let the bot run, then
`python label_outcomes.py --fetch-current` and re-run the fitter. Until then treat
the shipped floors as a declared policy with stated uncertainty, not calibration.

#### Per-chain early floors, and the AND-trap

The lane runs on every chain (`new_pools` is global), so floors are per-chain
wherever a chain's own cohort supports one. Crucially, they are derived against
the **joint** pass rate, not per metric.

**The trap:** these gates are ANDed, so setting each metric at its own p90 does
*not* select the top decile. Measured on the Robinhood birth cohort, five
independent p90 thresholds clear **0.71%** of births — and on the pooled cohort,
none at all. The lane was effectively shut while every individual number looked
defensible in isolation.

**The fix:** choose one *common* quantile level such that the share of births
clearing every gate hits a target volume.

```bash
bot-env/bin/python diag/population_floors.py \
    --chains robinhood --sources new_pools --pages 6 --target-pass-rate 0.02
```

Measured 2026-09-30, target 2%:

| | independent p90 | joint (level 0.86) |
|---|---|---|
| liquidity | $5,650 | **$5,700** |
| vol / liq | 0.202 | **0.070** |
| txns 5m | 26 | **13** |
| buy ratio | 0.602 | **0.561** |
| buyers 5m | 11 | **6** |
| **joint pass rate** | **0.71%** | **2.50%** |

Per chain, same joint method (`robinhood` n=80 → level 0.86; `bsc` n=40 → level
0.92, both 2.5% joint):

| Chain | n | liq | vol/liq | txns | buy ratio | buyers | Override |
|---|---|---|---|---|---|---|---|
| Robinhood | 80 | $5,700 | 0.070 | 13 | 0.561 | 6 | no — these are the globals |
| BSC | 40 | $13,600 | 0.045 | 8 | 0.74 | 5 | **yes** |
| Base | 15 | — | — | — | — | — | **no** — p90 liq CI spans $14.5k–$544k |
| Ethereum | 2 | — | — | — | — | — | **no** — not a sample |

Base and Ethereum fall back to the globals: correct behaviour, not a gap. Quoting
a $204k floor from Base's n=15 interval would be the two-token error with extra
steps.

This is the mechanism that answers "early on every chain, but no BSC spam"
without switching the early feed off: an objective, per-chain-derived floor
instead of a blinded discovery layer.



## Usage

Send `/start` in the chat, then paste a contract address to buy.

| Command | What it does |
|---|---|
| `/start` | Wallet, mode, balances, command list. |
| `/positions` | List and manage open positions. |
| `/sell <id>` | Market-sell a position. |
| `/balance` | Native balances on all chains. |
| `/settings` | Show slippage, trailing stop, TP ladder, risk. |
| `/risk <usd>` | Set $ risk per trade (adds a one-tap 💵 Risk buy button). |
| `/setamounts <chain> <a,b,c>` | Set preset buy sizes (independent per chain). |
| `/debug` | Log per-chain config and contract status. |
| `/features` | Feature-logging counters. |

Alerts, token cards and positions all carry inline buttons: preset/custom buy,
sell 25/50/100%, buy more, and set trailing stop.

## Deployment

`bot.py` is a FastAPI app; `python bot.py` starts it on `PORT` (default `10000`)
and the bot runs from the app lifespan. `GET /health` returns status, counters and
open positions, including the three "why was it quiet" counters
(`security_unknown_rejects`, `unsupported_venue_rejects`, `untradeable_alerts`).
Works on Render or any host that injects `PORT`. SQLite
(`pump_bot_v5.db`) and logs are local, so attach a disk if state must survive
redeploys.

## Tests

```bash
python -m unittest -v          # 183 tests, no network
```

The suite includes the objectivity guards that matter for the floors:
`test_audit_tools.TestPopulationFloors` asserts the cohort selection reads no
outcome field and names no token, that the censored age metric is excluded from
the derived floors, and that provenance (chain, n, command) is always emitted;
`TestThresholdFitting` asserts the fitter's small-sample gate refuses a two-token
sample and that the grouped split never puts one token on both sides.

## Research tooling (`diag/`)

Offline tools for asking *why* the bot missed something, without running a
scan. They share no state with the bot and are safe to run alongside it (read-only
against public APIs; GeckoTerminal responses are cached under `diag/.cache/`).

| Tool | What it answers |
|---|---|
| `diag/component_audit.py` | **Do the score components rank forward returns?** Rebuilds trailing windows from 5-minute OHLCV for a pool universe, scores every bar with the bot's real functions, and reports rank correlation, a per-pool sign test and AUC for ≥10/25/50/100 % moves. |
| `diag/holders_from_logs.py` | **Exact top-10/50/100 holder concentration, free.** Replays every `Transfer` log over Base JSON-RPC. Self-validating: it reproduces GeckoTerminal's published bands. |
| `diag/legs_boar.py` | The `boar` case study: what the model scored at each bar of the move, and what the price did next. |
| `diag/log_boar_row.py` | The exact `features` row the bot would write for any token, no scan required. |
| `diag/repro_boar.py` | Reproduces the hard-reject decision for the boar fixtures. |
| `diag/compare_decisions.py` | **Decision-neutrality check.** Runs 13 fixture cases through two revisions of `bot.py` and exits non-zero if any reject reason or alert decision changed — use it before shipping a refactor of `evaluate_token`. |
| `diag/population_floors.py` | **Where the default floors come from.** Samples the cohort a lane actually receives and reports per-metric p50/p75/p90 with bootstrap CIs, then emits `.env`-ready floors at a declared quantile. Outcome-blind: it never reads price or performance, which is what keeps it free of survivorship bias. Change the policy with `--quantile`, never by picking tokens. |
| `diag/dex_coverage.py` | **Where pools are born vs what the bot can trade.** Samples `new_pools` across chains and pages, reports each venue's share, and scores coverage under a declared router inventory (configured / +V4 / a hypothetical). Prints per-chain page depth and flags under-sampled chains so an incomplete fetch cannot masquerade as a chain-level difference. |
| `diag/alert_tradeability.py` | **Of what would alert, what can be bought?** Replays the shipped floors and the real `signals.early_runner_reasons` gates over a birth cohort, then splits survivors by `bot.dex_is_supported`, reporting the missing integration behind each no-route alert. Excludes tokens older than the lane's age cap instead of counting them as failures. |
| `diag/launchpad_migration.py` | **Do launchpad tokens reach an AMM, and when?** Reads a launchpad's pool listing, samples tokens evenly across age buckets, then measures each token's migration to an AMM and the lag from its birth pool. Age-stratified so a minutes-old token cannot be scored as a migration failure. |
| `diag/fit_thresholds.py` | **Outcome-fitted thresholds, or a refusal.** Fits on realised forward returns with a time split *and* a token-grouped split, reports the base rate beside every precision, and **refuses to emit anything** below 30 positives across 30 tokens. Run against an empty `features` table it refuses — the honest answer until labelled data exists. |

```bash
bot-env/bin/python diag/component_audit.py --pages 3 --csv diag/.cache/audit.csv
bot-env/bin/python diag/holders_from_logs.py 0xTokenAddress
bot-env/bin/python diag/compare_decisions.py            # HEAD vs working tree
bot-env/bin/python diag/population_floors.py --chains robinhood --quantile 0.90
bot-env/bin/python diag/dex_coverage.py --pages 6 --pause 2.8
bot-env/bin/python diag/fit_thresholds.py               # refuses until data exists
python -m unittest -v test_audit_tools
```

The `*_boar.py` tools and `FINDINGS_BOAR_2026-09-26.md` are post-mortems for one
incident. They are named after it because that is what they investigate; **no
shipped threshold references any token**, and `test_audit_tools` asserts that the
floor derivation names none and reads no outcome field.

## Training data

Set `LOG_FEATURES=true` and the bot writes one row per token evaluation — including
rejections — into the `features` table: liquidity/mcap, volume windows and ratios,
buy/sell counts and unique buyers, price changes, holder concentration, taxes and
security flags, signal-engine bonus/penalty, the hand-tuned score, and whether an
alert was sent. It is fire-and-forget (a background thread writes it), so the
scanner never slows down.

Two properties matter when the table is used to find out *why* a runner was
missed:

- **Every row carries the raw inputs, even floor rejects.** The metrics are read
  before the per-chain floors (`liquidity`, `vol_5m`, `market_cap`, `price`), so a
  token dropped early is still a usable row rather than a row of zeros. Before
  this, a thin 5-minute read was indistinguishable from a token that never traded.
- **The score is recorded as a breakdown, not just a total.** `score_vol_liq`,
  `score_vol_5m_1h`, `score_vol_1h_6h`, `score_vol_6h_24h`, `score_buy_5m`,
  `score_buy_1h`, `score_price`, `score_holder`, `score_security`, `score_cex`
  and `penalties_total` sum to `hand_score`, so a token that scored 48 against a
  bar of 64 can be attributed to the components that withheld the points.
  `ceiling_score` is the reachable maximum the bar was scaled against, and
  `alert_threshold` is the bar itself — recorded provisionally even for rows
  rejected before the holder lookup runs.

```sql
-- How close did each token ever get, and what stopped it?
SELECT symbol, chain, MAX(hand_score) AS best, MAX(ceiling_score) AS ceiling,
       MAX(alert_threshold) AS bar, MAX(reject_reasons) AS last_reason,
       COUNT(*) AS scans
FROM features GROUP BY chain, token_address ORDER BY best * 1.0 / ceiling DESC;
```

`label_outcomes.py` turns those rows into supervised labels using the forward
maximum price each token reached (the repeated scans act as the price series):

```bash
python label_outcomes.py                      # writes max_mult_1h/6h/24h + hit_Nx_* labels
python label_outcomes.py --fetch-current      # also label the newest rows from live prices
python label_outcomes.py --export training.csv
```

To see the exact row the bot would write for any token, without running a scan:

```bash
bot-env/bin/python diag/log_boar_row.py [token_address]   # defaults to the boar case
```

Then train offline, e.g. logistic regression on "did it 2× within 24h":

```python
import pandas as pd
from sklearn.linear_model import LogisticRegression

df = pd.read_csv("training.csv")
features = ["liquidity_usd", "vol_liq_ratio", "buy_ratio_5m", "buyers_5m",
            "top10", "lp_locked", "signal_bonus", "hand_score"]
X, y = df[features].fillna(0), df["hit_2x_24h"].fillna(0).astype(int)
model = LogisticRegression(max_iter=1000).fit(X, y)
```

Rows sharing a `(chain, token_address)` are **not** independent (they are the same
token at different times) — split by token, not by row, when validating.

## Security

- Use a **dedicated hot wallet** with only what you can afford to lose, and start
  in paper mode.
- `.env` is gitignored — never commit it or paste its contents anywhere.
- Only `CHAT_ID` (plus `ALLOWED_USER_IDS`) can control the bot.
- Rotate `TELEGRAM_TOKEN` if it is ever exposed (e.g. in logs).

## Notes

- **Finding runners early vs. filtering noise is a real trade-off.** Discovery
  breadth (`new_pools`) is what finds pools early; filtering is what keeps junk
  out. They pull in opposite directions, so the bot keeps them separate:
  discovery decides *what gets looked at*, and AND-gated filters decide *what
  alerts*. GeckoTerminal supplies unique `buyers`/`sellers`, which DexScreener
  does not — those gates (`SIG_MIN_UNIQUE_BUYER_RATIO`, `SIG_MIN_VOL_LIQ`) are
  what stop a rug from scoring high on volume alone.
- **Filtering.** The hand-tuned score (`MIN_SCORE`) is the gate, as in the
  original bot. Signals only veto and penalise at the code default
  (`SIGNAL_BONUS_WEIGHT=0.0`); `.env.example` recommends `0.5` so runner
  signals can promote. Tighten a noisy chain directly with `BASE_MIN_LIQUIDITY_USD`,
  `BASE_MIN_VOL_5M_USD`, `BASE_MIN_MARKET_CAP_USD`.
- **Early runners.** `EARLY_RUNNER_MODE=true` + `USE_GECKOTERMINAL=true` gives a
  young pool a chance to alert even though it can't reach `MIN_SCORE`. It is
  AND-gated, so it will not fire on a dead, illiquid or wash-traded pool.
  Both default to `false` in code; `.env.example` turns them on.
- Discovery defaults to DexScreener's boost/profile lists (`USE_GECKOTERMINAL=false`
  in code) because the DexScreener "all pairs" endpoint is dead (404) — but that
  path only ever sees paid shills, so set `USE_GECKOTERMINAL=true` for real
  discovery.
- Only Uniswap/Pancake-style V3 + V2 routes are supported for swaps. Uniswap V4,
  Aerodrome (Base), Pons and other V3 forks are **not** routable — the alert says
  so and withholds the buy buttons. See
  [Tradeable venues](#tradeable-venues--do-you-need-uniswap-v4) for the measured
  coverage and what to add first.
- `/health` reports why the bot was quiet as counters, not just a log:
  `security_unknown_rejects` (GoPlus lag), `unsupported_venue_rejects`
  (`REQUIRE_TRADEABLE_VENUE=true` dropping V4/forks) and `untradeable_alerts`
  (alerts sent with buy buttons withheld).
- `REVIEW.md` and `FINDINGS_BOAR_2026-09-26.md` are **point-in-time records** of
  specific review passes and one incident. Where they disagree with this README,
  the README and the code are current and those documents are history.
- See [`REVIEW.md`](REVIEW.md) for the detailed code review, known issues and
  roadmap.
