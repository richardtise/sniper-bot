# Code review — my-sniper-bot

Reviewed: `bot.py` (~3,000 lines after pass 2), `signals.py`, `discovery.py`,
`label_outcomes.py`, `requirements.txt`, `start.sh`, `test_telegram.py`,
`.env` (names only), `crime_pump.log`.

**Verdict in one line:** the trading/execution half is solid and carefully written, but
the *finding* half is the weak link — and the single biggest reason the bot surfaces
false positives is that its primary discovery endpoint does not exist.

This document is ordered by impact. Items marked **P0** are worth fixing today.
Items marked **✅ fixed** were changed in this pass; everything else is a recommendation.

---

## Pass 3 — why the bot produced no alerts at all, and the fixes

After pass 2 the scanner went *silent*: no alerts on any chain for days. The cause
was not over-tight filtering in the abstract but four concrete, independent faults,
three of them silent provider failures that made 25 of the 100 score points
permanently unearnable.

| # | Fault | Evidence | Fix |
| --- | --- | --- | --- |
| 3.1 | **Robinhood holder concentration was always 0.** `get_robinhood_holders` called `robinhoodchain.blockscout.com/api/v2/tokens/{addr}/holders`, which now answers every request with a Cloudflare managed challenge. `fetch_json` retries only 429/5xx, so a **403** fell through to `return None` and holders became `(0,0,0)`. | `curl` → HTTP 403 + `<title>Just a moment...</title>` | Holder concentration now comes from GeckoTerminal `/networks/{net}/tokens/{addr}/info`, which publishes exact `top_10` / `11_30` / `31_50` bands, free and keyless, on all four chains. Blockscout is kept behind `TRY_BLOCKSCOUT_HOLDERS` (default `false`). |
| 3.2 | **Moralis made holders 0 on *every* chain.** The configured key returns `401 "Your Moralis Free usage is paused"`, and the code cached that as a successful `(0,0,0)` — indistinguishable from "measured 0%". | Live API call | `HolderData` now carries a `source`, so "provider unavailable" is distinguishable from "0% concentration". GeckoTerminal backs Moralis up on BSC/ETH/Base. |
| 3.3 | **Blocked Blockscout also faked "unverified".** The same 403 left `is_verified=False`, so every Robinhood token took `PENALTY_UNVERIFIED_CONTRACT` **and** tripped `signals`' `not_open_source` hard reject. SCHIFFY, for instance, **is verified**. | Etherscan `getsourcecode` returns its source | Verification now comes from Etherscan v2 (`chainid=4663`; `SCANNER_API_KEY` is accepted as an alias because it already holds an Etherscan key). The penalty applies only when `verification_known` is true — our own outage must not look like a bad token. |
| 3.4 | **`require_open_source` was unconfigurable.** It defaulted to `True` and was absent from `Filters.from_env()`'s mapping, so there was no `SIG_REQUIRE_OPEN_SOURCE` and no way to turn it off without editing code. | — | Default is now `False` (most Robinhood tokens are unverified, including the ones that run) and `SIG_REQUIRE_OPEN_SOURCE` exists. |

### The scoring deadlock

Those faults, plus two structural ones, meant the gate could not be reached:

* **Holder concentration — 20 pts** unearnable on every chain (3.1, 3.2). GT restores
  top-10 and top-50, but publishes **no 51-100 band**, so 4 of the 20 stay
  unmeasurable. `top100` is now `None` rather than a guess.
* **CEX listings — 5 pts** unearnable on Robinhood: no Robinhood token is on CoinGecko.
* **Age gates.** A 1-hour-old pool cannot score the `1h/6h` tier (capped at 0.3×) or
  `6h/24h` (also 0.3×). Empirically its ceiling is **~58.6**, not 100.

Requiring a flat **65** out of a reachable **~43–58** is what produced silence. Measured
against every live Robinhood pool, the maximum attainable score was **32.0** and **0 of
40 would have alerted**.

**Fix — `max_possible_score()` + `effective_threshold()`.** The ceiling is computed by
calling the *real* scorers with ideal inputs, so it cannot drift out of sync with them,
and the bar becomes `MIN_SCORE × (reachable ÷ 100)`, floored at `MIN_EFFECTIVE_SCORE`
(default 35, itself capped by `MIN_SCORE`). `SCORE_NORMALIZE=false` restores the raw
gate; `ROBINHOOD_MIN_SCORE` etc. override per chain.

| Age | reachable (Robinhood) | old bar | new bar |
| --- | --- | --- | --- |
| 10m | 53.0 | 65 | 35.0 |
| 1h | 58.6 | 65 | 38.1 |
| 2h–6h | 81.8 | 65 | 53.1 |
| 12h+ | 89.2 | 65 | 58.0 |

Replaying SCHIFFY's own GeckoTerminal candles through the scorer: it was 0.3 points short
at 1h, and **would have alerted at 2h at 56×, 3h at 16× and 4h at 4× from those prices** —
whereas under the old flat 65 it never alerted at any age.

### What was checked and deliberately *not* changed

* **Age gates** — kept. They are the only thing stopping 30-second-old, $2k-liquidity
  pools, and with the gate now scaled they no longer need to be loosened. The
  discontinuity they create (the bar jumps at 60min/6h because more points become
  measurable) is inherent to the original step functions.
* **`SCANNER_API_KEY`** is *not* dead config after all — see 3.3. It is an Etherscan key
  and now does verification work. `AUTO_BUY_*` remains dead.
* **Live trading** still needs a real hot wallet; nothing here was tested with funds.

### Verification

`python -m unittest test_signals test_discovery test_features test_telegram test_sources`
— **85 tests**, all passing, no network. `test_sources.py` covers the GT band parsing,
the `None` top-100 contract, the ceiling matching a perfect token, threshold scaling,
the floor, per-chain overrides and the open-source flag.

---

## 0. What was added in this pass

| File | Purpose |
| --- | --- |
| `signals.py` | Pure runner/false-positive engine: hard rug filters + runner bonus/penalty, cross-scan acceleration history. 26 unit tests. |
| `discovery.py` | Working candidate discovery via GeckoTerminal (free, no key), mapped to the pair shape `bot.evaluate_token` already consumes. 10 unit tests. |
| `test_signals.py`, `test_discovery.py`, `test_features.py` | `python -m unittest` suites, 57 tests, no network. |
| `label_outcomes.py` | Pass 2: turns logged feature rows into forward-return labels for offline training. |
| `.env.example` | Complete reference incl. the new flags. |
| `.gitignore` | Now ignores `*.log`, `bot-env/`, `*.db`, caches. |
| `README.md` | Full setup/usage/config/deploy docs. |

Wired into `bot.py`, **all opt-in and default-off** except the three bug fixes:

```dotenv
USE_SIGNALS=true          # enable signals.py filters + bonus/penalty
USE_GECKOTERMINAL=true    # enable discovery.py instead of the 404'd endpoint
ALLOWED_USER_IDS=123456   # optional extra Telegram allowlist
```

---

## 0b. Pass 2 — remaining review items fixed + feature logging

Every open item from §2–§5 was addressed except the deliberate deferrals noted at
the end of this section.

| Item | Status |
| --- | --- |
| 4.4 `get_buy_amounts` keyed by native symbol | ✅ Keyed per chain (`buy_amounts_{chain}`) with a one-time migration of any customised legacy values; `/setamounts` validates the chain |
| 4.5 `/risk` stored but unused | ✅ Now sizes trades: the buy keyboard gets a `💵 Risk $X` button that converts USD risk to native via a live native price |
| 2.4 `/health` leaked the wallet | ✅ Wallet field removed from the JSON response |
| 2.5 Infinite approvals | ✅ Approves exactly the sell amount (`APPROVAL_MULTIPLIER`, default 1.0); residual risk documented in code + `.env.example` |
| 4.6 Security `None` cached 1800s | ✅ Failed lookups cached only 120s; added a honeypot.is v2 fallback when GoPlus is down |
| 4.7 Nonce race | ✅ Per-chain `asyncio.Lock` held across nonce fetch → sign → send |
| 4.8 Legacy-only gas | ✅ `build_gas_fields()` uses EIP-1559 when supported, with a `MAX_GAS_PRICE_GWEI` ceiling |
| 4.9 Dead swap params | ✅ Removed; `get_token_decimals()` avoids the extra `balanceOf` |
| 4.10 `fetch_json` retried only 429 | ✅ Retries 429/5xx/timeouts with jittered backoff and honours `Retry-After` |
| 4.11 `tokens_evaluated` unused | ✅ Reported in `/health` and the heartbeat |
| 4.12 `user_state` key mismatch | ✅ Single `state_key()` convention |
| 4.13 Callback data near 64-byte cap | ✅ Buttons carry a short server-side ref instead of chain+address |
| 4.14 Unbounded log file | ✅ `RotatingFileHandler` (5 MB × 3) |
| 4.15 Bare `except:` | ✅ All replaced with `except Exception:` |
| 4.16 Fiction paper fills | ✅ Paper buys fill at the observed native price minus fee/slippage; paper sells value the sold tokens at the live price. Paper mode also no longer requires web3 (it returned "No Web3 RPC" before) |
| 5.2 No feedback loop | ✅ Phase 2 below: per-evaluation feature logging + `label_outcomes.py` |
| 5.3 Blocking handler loop | ✅ Updates dispatched as tasks; polling keeps draining |
| 5.4 No Telegram 429 handling | ✅ `tg_send()` catches `RetryAfter` and retries with backoff |
| 5.5 Unescaped HTML | ✅ `esc()` applied to token names/symbols/tx hashes |
| 5.6 `telebot` not in requirements | ✅ `test_telegram.py` rewritten on python-telegram-bot |
| 5.7 Unused `.env` keys | ⚠️ Documented; `AUTO_BUY_*`/`SCANNER_API_KEY` are still dead config (remove or implement) |

### 0c. Pass-2 regression — excess Base noise (fixed)

Pass 2 accidentally made the bot **less** selective than the original, which
showed up as a flood of Base alerts. Three separate causes:

1. **Permissive security fallback (no flag needed).** `get_token_security` accepted
   a honeypot.is record whenever GoPlus had no data — with `_security_placeholder`
   defaults, an unanalysable token looked "safe". The original code **dropped
   unknown tokens**. *Fixed:* `ALLOW_SECURITY_FALLBACK` (default `false`) restores
   the drop; when enabled, honeypot.is is only trusted if it actually simulated
   (`simulationSuccess` + `honeypotResult`).
2. **Additive signal bonus.** `total_score = hand + bonus - penalty` with
   `max_bonus = 45` let a legacy ~40 token clear `MIN_SCORE=65`. *Fixed:*
   `SIGNAL_BONUS_WEIGHT` (default `0.0`) weights the bonus, and the alert gate is
   the hand-tuned score again (`legacy_total >= ALERT_THRESHOLD`); signals can
   only veto or demote.
3. **Loosened signal thresholds.** liquidity 8000→4000, 5m vol 500→250, age
   3→1 min, txns 8→5, avg-trade 0.10→0.25, holders 50→10. *Fixed:* restored the
   strict values in `signals.py` and `.env.example`.

Also added per-chain floor overrides (`BASE_MIN_LIQUIDITY_USD`,
`BASE_MIN_VOL_5M_USD`, `BASE_MIN_MARKET_CAP_USD`) so one noisy chain can be
tightened without touching the others, and changed the `.env.example` discovery
recommendation to `USE_GECKOTERMINAL=false` / `GT_SOURCES=trending` (the previous
`new_pools,trending` was the noisiest possible source on Base).

### 0d. Why `USE_GECKOTERMINAL=true` + `USE_SIGNALS=true` made it *worse*

Those two flags were supposed to filter more and find runners earlier. They did
the opposite, for reasons that are worth stating plainly:

1. **Discovery and filtering are opposite forces.** GeckoTerminal `new_pools`
   returns every newly created pool, including $2k-liquidity, 30-second-old,
   unverified ones. The original bot discovered from DexScreener boosts/profiles —
   a far smaller, curated set. Broadening discovery *must* increase raw noise;
   it only helps if the filters are strong enough to pay for it.
2. **The signal engine was additive, not gating.** `+bonus` up to 45 meant a
   token with a hand score of ~40 could clear `MIN_SCORE=65`. A sum lets one
   strong-looking metric compensate for missing ones — the exact failure mode
   rugs exploit. Now the gate is the hand-tuned score plus AND-conditions.
3. **Its thresholds were loosened** (liquidity 8000→4000, vol 500→250, age 3→1
   min, txns 8→5, avg-trade 0.10→0.25, holders 50→10) on the strength of "early
   runners are concentrated". That reasoning applies to *holder concentration*
   only; loosening the liquidity/activity gates just admitted dead pools.
4. **A young pool can never reach `MIN_SCORE`** because its 1h/6h/24h windows are
   empty — so `new_pools` was simultaneously the reason it was noisy *and* the
   reason it produced nothing useful. That is the real gap.

**Added to fix #4 properly — `EARLY_RUNNER_MODE`.** A young pool may alert below
`MIN_SCORE` only if *every* condition holds (`signals.early_runner_reasons`):
age ≤ 30 min, liquidity ≥ $15k, liquidity/mcap ≥ 1%, 5m volume ≥ 10% of
liquidity, ≥ 20 txns, buy ratio ≥ 60%, ≥ 15 **unique buyers**, 5m change ≤ 150%,
and a clean security record. AND semantics mean strong volume cannot compensate
for a missing unique-buyer base — which a weighted score always allows. Default
off; needs `USE_SIGNALS=true`.

**Also added — the AND-gates GeckoTerminal unlocks.** Unique buyers are data
DexScreener does not return, so `SIG_MIN_UNIQUE_BUYER_RATIO` (unique wallets /
buys) and `SIG_MIN_VOL_LIQ` are hard rejects that only activate on GT data. These
are what make GT discovery worth enabling instead of just louder.

**Deliberately deferred**
* §5.1 modularising the 2.9k-line `bot.py` — large, mechanical, and best done with
  the trading paths under test first.
* §4.0 router-agnostic execution (Aerodrome/V4/aggregators) — a feature, not a bug.
* §3.3 liquidity ≥ 50× intended buy size — partially covered by the signal engine;
  add as a hard live-trading gate only once position sizing is settled.
* §2.3 mandatory `I_UNDERSTAND_LIVE_TRADING` second flag — would break existing
  live deployments, so the paper-first default stands.

---

## 1. P0 — the discovery endpoint 404s, so the scanner only ever sees paid shills

`get_all_pairs()` (bot.py:561) opens with:

```python
url = f"https://api.dexscreener.com/latest/dex/pairs/{network}?page=0&pageSize=300"
```

That route does not exist. Verified live:

```
$ curl -s -o /dev/null -w "%{http_code}" "https://api.dexscreener.com/latest/dex/pairs/bsc"
404
```

`fetch_json` returns `None` on any non-200 (bot.py:512), so the failure is silent.
The only candidates that reach the scorer therefore come from:

* `https://api.dexscreener.com/token-boosts/top/v1` — **paid boosts**
* `https://api.dexscreener.com/token-profiles/latest/v1` — **paid profiles**

You are running a scanner whose entire input is the advertising list. That is a
structural false-positive generator, and no amount of threshold tuning fixes it.

**Fix (implemented):** `discovery.py` discovers from GeckoTerminal instead —
`new_pools` (the actual sniper surface), `trending_pools`, and a volume-sorted
universe. It also gives you fields DexScreener's search does not: unique
`buyers`/`sellers` per window, `reserve_in_usd`, and pool creation time.

Enable it:

```dotenv
USE_GECKOTERMINAL=true
GT_SOURCES=new_pools,trending
```

`discovery.dedupe_best_pool()` also collapses multiple pools for the same token
to the deepest one, which the old loop never did — it burned holder/CEX API calls
once per pool and could alert twice on the same token.

**Still worth adding later (P1):** on-chain new-pair detection. Subscribe to
`PairCreated` (V2 factories) / `PoolCreated` (V3 factories) via `eth_getLogs` or a
websocket RPC. That is how a sniper sees a pool before any aggregator indexes it,
and it removes your dependence on any third-party listing.

---

## 2. P0 — security

### 2.1 ✅ A live Telegram token is sitting in `crime_pump.log`

`crime_pump.log` contains the bot token in plaintext 9 times (httpx logs the full
`.../bot<id>:<secret>/sendMessage` URL), and `*.log` was **not** in `.gitignore`,
so `git add -A` would have committed it.

```
$ grep -cE "bot[0-9]{6,}:[A-Za-z0-9_-]{30,}" crime_pump.log
9
```

**Action required from you:** revoke that token in @BotFather (`/revoke`), issue a
new one, and delete the log. I added `*.log` to `.gitignore`, but the leaked token
is already on disk and may be in shell history/backups. A bot token controls the
wallet path, so treat it as compromised.

### 2.2 ✅ Any chat could run stateful commands

`telegram_polling_task()` passed every update straight to the handlers. Commands
like `/sell <id>`, `/risk`, `/setamounts` execute against your wallet without
checking who sent them. There was no sender check anywhere.

**Fix (implemented):** `is_authorized()` (bot.py:229) rejects any update whose
chat is not `CHAT_ID`, plus an optional `ALLOWED_USER_IDS` user-level allowlist
for when `CHAT_ID` is a group. Unauthorized updates are logged and dropped.

### 2.3 Private key handling

* Good: `PAPER_TRADING` defaults to `true`, and `bot.py` forces paper mode if no
  web3 connection exists (bot.py:300).
* Recommend: a **dedicated hot wallet** with only spending money, never your main
  wallet. The key sits in a process that also talks to arbitrary third-party APIs
  and parses untrusted token metadata.
* Recommend: `PAPER_TRADING=true` as a hard gate the code cannot silently flip, and
  an explicit `I_UNDERSTAND_LIVE_TRADING=true` second flag before any real swap.
* Recommend: rotate the key after any incident, and consider a KMS/secret manager
  instead of a plain env var.

### 2.4 ✅ `/health` leaked your wallet address

`health_check()` (bot.py:2234) returned `"wallet": WALLET_ADDRESS` on a server
bound to `0.0.0.0`. If the port is ever public (Render gives it a URL), anyone
could link your wallet to the bot. **Fixed:** the field is removed; the endpoint
now also reports `tokens_evaluated` and feature-logging counters.

### 2.5 ✅ Infinite approvals

`ensure_token_approval()` (bot.py:1110) approved `2**256 - 1`. **Fixed:** it now
approves exactly the current sell amount (`APPROVAL_MULTIPLIER`, default `1.0`).
Residual risk is documented: raising the multiplier leaves a larger standing
allowance that a router bug could drain.

---

## 3. P0 — the runner signals the scorer is missing

The existing 0–100 score measures volume ratios, buy pressure, price change,
holder concentration and CEX listings. That is a reasonable start, but it is
missing the features that actually separate organic runners from traps. All of
these are implemented in `signals.py` and can be enabled with `USE_SIGNALS=true`.

### 3.1 Holder concentration: stance-dependent (pump vs rug)

`score_holder()` (bot.py:900) **awards points for concentration**: top-10 ≥ 80%
gets the full 10 points, top-10 ≥ 40% gets 5. That is the right call for *early
runners* — a controlled float is what lets a coin move, and early entry into a
manipulated coin can still pay. The risk is the same property: one wallet can dump
the float. So `signals.py` makes it a deliberate choice:

* `SIG_HOLDER_STANCE=pump` (**default**) — matches `score_holder`: top-10 ≥ 70%
  gets +8, ≥ 45% gets +5, a 3–30% dev bag gets +3 ("skin in the game"). Only the
  absurd is rejected (top-10 > 95%, dev > 40%).
* `SIG_HOLDER_STANCE=rug` — treats concentration as exit risk: rewards
  distribution, penalises 45–60%, hard-rejects above `SIG_MAX_TOP10_PCT` /
  `SIG_MAX_CREATOR_PCT`.

Related defaults were loosened for early entry and are all tunable:
`SIG_MIN_AGE_MINUTES=1`, `SIG_MIN_LIQUIDITY_USD=4000`, `SIG_MIN_TXNS_5M=5`,
`SIG_MAX_AVG_TRADE_LIQ=0.25` (whale-dominated pools allowed), `SIG_MIN_HOLDERS=10`.

### 3.2 No unique-buyer / wash-trade detection

`buys_5m` counts transactions, not wallets. 40 buys can be one bot cycling funds.
GeckoTerminal returns `transactions.m5.buyers` — wallets, not txns. `signals.py`
uses it: `buyers/buys ≥ 0.7` is a bonus; `< 0.4` with ≥ 10 buys is a wash-trade
penalty. It also rejects when the average 5m trade exceeds 10% of pool liquidity
(one whale moving the price).

### 3.3 No liquidity-depth-vs-trade-size check

The bot never asks "can my $50 buy move this pool?". A $600k-mcap token with $6k
liquidity passes the current $3k floor and will be un-sellable at size. `signals.py`
requires `liquidity ≥ 1% of mcap` and rewards `vol_5m ≥ liquidity`. Recommend also
requiring `liquidity ≥ 50 × intended buy size` for live trading.

### 3.4 LP-lock status is fetched but never used

`get_token_security()` reads `is_lp_locked` into `lp_locked`, and then **nothing
reads it**. `signals.py` rewards locked LP (+6) and penalises unlocked (−8), with an
optional hard reject (`SIG_REQUIRE_LP_LOCKED=true`).

### 3.5 GoPlus fields that catch rugs are ignored

Only 8 of GoPlus's flags were extracted. Verified live that GoPlus returns many
more, and **added** to `bot.get_token_security`: `hidden_owner`, `cannot_sell_all`,
`selfdestruct`, `trading_cooldown`, `holder_count`, `creator_percent`. These are
classic rug tells (`cannot_sell_all` = holders literally cannot sell).

### 3.6 No late-entry guard

A token up 2,000% in the last hour with 5m momentum fading is not a runner
opportunity, it is exit liquidity. `signals.py` rejects `chg_1h > 900%` when
`chg_5m < 15%` and penalises `chg_1h > 300%`.

### 3.7 No persistence/acceleration memory

Scoring is a single 30-second snapshot, so a one-candle volume spike scores the
same as sustained accumulation. `signals.PairHistory` stores a rolling snapshot per
token and rewards volume acceleration vs. its own median and rising scores/holders
across scans. `evaluate_token` now records every non-rejected candidate.

### 3.8 Major assets and exotic quotes are evaluated

Nothing skipped WBNB/WETH/USDC or token/token pools. Every cycle you were scoring
majors and wasting provider calls. `signals.is_major_asset()` and the
`exotic_quote` filter handle this.

### 3.9 Paid boosts are treated as organic

Because boosts/profile listings feed discovery, paid promotion is currently
indistinguishable from demand. `signals.py` penalises `paid_boost` (−10). If you
keep DexScreener as a source, treat boost-only tokens as a separate watchlist.

---

## 4. P1 — correctness bugs

### 4.0 Routing/quoter addresses — "no quotes" root cause (fixed)

Verified live against the real RPCs with `cast`:

| Config | Address | Result |
| --- | --- | --- |
| Ethereum quoter **hardcoded in bot.py** | `0x61fFE014bA17989E743c5F6cB21bF969dc0b0e10` | **no contract code** → every ETH quote failed |
| Ethereum router **hardcoded in bot.py** | `0xE592427A0AEce92De3Edee1F18E0157C05861564` | old SwapRouter, 8-field `exactInputSingle` — does **not** match bot.py's 7-field ABI |
| BSC quoter (was hardcoded) | `0xB048Bbc1Ee6b0bD2fD19B4eEdb5f5b5f5b5f` | fabricated; fixed to `…733FFfCFb9e9CeF7375518e25997` |
| `.env` values (ETH/BASE/BSC/ROBINHOOD quoters + routers) | — | all correct, all have code |

So the bot only worked when the `*_QUOTER_V2` / `*_ROUTER_V3` env vars were
present. On any deployment that relies on the hardcoded fallbacks (Render
dashboard missing those vars), Ethereum had no quoter at all and Ethereum swaps
used the wrong ABI. Fixes applied:

1. Corrected `_HARD_QUOTERS["ethereum"]` to `0x61fFE014bA17989E743c5F6cB21bF9697530B21e`
   and `_HARD_ROUTERS["ethereum"]` to SwapRouter02 `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45`
   (verified: 7-field selector `0x04e45aaf` present in its bytecode).
2. Added verified robinhood fallbacks (router, quoter, WNATIVE) so it no longer
   requires env vars to function.
3. Added `address_has_code()` and made `/debug` print `OK` / `NO CODE ⚠` per
   contract, so a typo'd address can never again fail silently.
4. `try_v2_swap` now quotes **direct + multi-hop** paths (via WNATIVE and the
   chain's stables) and picks the best. Many runner tokens only have a
   stablecoin pair, so the single direct path was the other reason for
   "no quote". Failure now reports both routes' reasons.

Remaining gap: only Uniswap/Pancake-style V3 + V2 routers are supported. Base
liquidity is often on Aerodrome and Ethereum micro-caps are often on V4/other
DEXes; those still need a router-agnostic quote (aggregator or per-`dexId`
routing). This is the main "quote" improvement left.

> **Pass 2:** every row in this table is now fixed — see §0b for the per-item
> summary. The "Fix" column records what was done or recommended.

| # | Where | Bug | Fix |
| --- | --- | --- | --- |
| 4.1 | ✅ `_HARD_QUOTERS` (bot.py:116) | BSC QuoterV2 was `0xB048Bbc1Ee6b0bD2fD19B4eEdb5f5b9F5b5f5b5f` — a fabricated address. Real PancakeSwap QuoterV2 is [`0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997`](https://docs.pancakeswap.finance/developers/smart-contracts/pancakeswap-exchange/v3-contracts). V3 quoting on BSC silently failed and always fell back to V2. | Corrected; `BSC_QUOTER_V2` still overrides. |
| 4.2 | ✅ `.env` | `COINGECK0_API_KEY` (zero) vs code reading `COINGECKO_API_KEY` (letter O). CEX scoring was permanently 0 and `build_coingecko_id_map()` never ran. | Renamed the key in `.env`. |
| 4.3 | ✅ `sell_position_pct()` (bot.py:1458) | Called `get_token_price_usd(None, ...)`; with `session=None` the fetch always raised and P&L was recorded as `0`. | `get_token_price_usd` now opens a temporary session when `session is None`. |
| 4.4 | `get_buy_amounts()` (bot.py:1645) | Keys presets by `NATIVE_SYMBOL[chain]`, so `ethereum`, `base` and `robinhood` all map to `buy_amounts_eth`. `/setamounts base ...` silently changes Ethereum's presets; the seeded `buy_amounts_base` row is dead. | Key by chain (`buy_amounts_{chain}`), or accept shared presets deliberately and delete the dead row. |
| 4.5 | `/risk` (bot.py:1866) | Stores `risk_usd` but **nothing sizes trades from it** (contradicting `get_buy_amounts`' docstring). | Either implement USD-risk sizing or remove the command to avoid false confidence. |
| 4.6 | `get_token_security()` (bot.py:615) | Caches `None` for 1800s on *any* lookup failure, and `evaluate_token` drops the token when security is `None`. A transient GoPlus outage hides every token for 30 minutes. | Cache "safe/unsafe" results separately from "lookup failed"; on failure fall back to (a) GeckoTerminal `is_honeypot`, (b) [honeypot.is v2](https://api.honeypot.is/v2/IsHoneypot) (verified working for BSC/ETH/Base). |
| 4.7 | `try_v3_swap`/`try_v2_swap` | No nonce lock. A Telegram buy and a monitor sell can both fetch the same `pending` nonce → one transaction replaces the other. | Guard nonce allocation with a per-chain `asyncio.Lock`, or a serialized tx queue. |
| 4.8 | gas pricing | Legacy `gasPrice` only, no EIP-1559. On ETH/Base this can overpay or get stuck during base-fee spikes. | Use `maxFeePerGas`/`maxPriorityFeePerGas` when the chain supports it; add a gas ceiling. |
| 4.9 | `try_v3_swap(price_native=, token_decimals=)` (bot.py:1164) | Parameters accepted and never used; `execute_buy` does an extra `balanceOf`/`decimals` round-trip for nothing. | Remove the dead params / call. |
| 4.10 | `fetch_json()` (bot.py:512) | Retries only 429; 5xx and timeouts are not retried. | Retry 5xx/timeouts with jittered backoff. |
| 4.11 | `evaluate_token` | `tokens_evaluated` is incremented and never read. | Use it in `/health`, or delete. |
| 4.12 | `handle_text_message` (bot.py:1806) | State is stored by `CHAT_ID` but read by `str(chat_id)`; works only because they coincide. | Use one key convention. |
| 4.13 | callback data | `buy:{chain}:{token}:{amount}` is ~62 bytes for `robinhood` — at Telegram's 64-byte cap. | Send an id and look the rest up server-side. |
| 4.14 | `logging.FileHandler("pump_bot_v5.log")` | Unbounded growth. | `RotatingFileHandler(maxBytes=…)`. |
| 4.15 | bare `except:` in `handle_callback_query`, `handle_text_message` | Swallows `KeyboardInterrupt`/`SystemExit` and hides bugs. | `except Exception:`. |
| 4.16 | paper trading (bot.py:1316) | `simulated_tokens = amount_native * 1000` ignores price, slippage and fees, so paper fills are fiction and can never validate the strategy. | Model entry at the quoted price with slippage + fee; paper P&L should be computed in native, not from `priceUsd` alone. |

---

## 5. P1 — architecture & maintainability

1. **2,242-line single file.** Split into `config.py`, `db.py`, `providers/`
   (`dexscreener.py`, `goplus.py`, `moralis.py`, `coingecko.py`, `geckoterminal.py`),
   `scoring.py`, `trading.py`, `telegram_bot.py`. `signals.py` and `discovery.py`
   are the first modules; the rest can follow without behaviour changes.
   *(Deferred — see §0b.)*
2. ✅ **No feedback loop → Phase 2.** Every evaluation (including rejects) is now
   logged to the `features` table with the full feature vector and reject reason,
   and `label_outcomes.py` derives forward-multiple labels. `positions.pnl_pct`
   is also real now that paper fills are priced (4.16) and `get_token_price_usd`
   works without a session (4.3). See §8.
3. ✅ **Blocking handler loop.** `telegram_polling_task` dispatches each update to
   its own task (`_spawn_handler`), so a 120s buy receipt no longer stalls commands.
4. ✅ **No retry/backoff on Telegram 429.** `tg_send()` catches `RetryAfter` and
   retries with backoff; used by alerts, position exits and menus.
5. ✅ **HTML injection / parse errors.** `esc()` wraps every dynamic value
   (names, symbols, tx hashes, signal notes) before `ParseMode.HTML`.
6. ✅ **`test_telegram.py` imports `telebot`.** Rewritten on python-telegram-bot,
   which is already in `requirements.txt`.
7. ⚠️ **`.env` contains unused keys**: `SCANNER_API_KEY`, `AUTO_BUY_ENABLED`,
   `AUTO_BUY_AMOUNT`, `AUTO_BUY_MIN_SCORE`. `AUTO_BUY_*` in particular implies an
   auto-buy feature that does not exist. Still dead config — remove or implement.

---

## 6. Recommended roadmap (highest value first)

**Do now**
1. Revoke the leaked Telegram token (§2.1).
2. Set `USE_GECKOTERMINAL=true` and `USE_SIGNALS=true`; leave `PAPER_TRADING=true`.
**Done in pass 2**
3. ✅ Fixed `get_buy_amounts` keying and gave `/risk` a real job (§4.4, §4.5).
4. ✅ Holder concentration aligns: `score_holder` and `SIG_HOLDER_STANCE=pump`.
5. ✅ honeypot.is fallback and short failure cache (§4.6).
6. ✅ Nonce locking and EIP-1559 gas (§4.7, §4.8).
7. ✅ Feature logging for offline training (§5.2, §8).

**Next**
8. GeckoTerminal token-info enrichment (`gt_score`, socials, holder distribution,
   dev holding) for phase-1 survivors — free, improves legitimacy + rug filtering.
9. A `trades` table + `/stats` with realized P&L per entry-score bucket, now that
   paper fills and P&L are real.
10. Router-agnostic execution: per-`dexId` routing or an aggregator quote so
    Aerodrome (Base) and V4/other venues are tradeable (§4.0).
11. Train the first model on `label_outcomes.py --export` output and compare its
    precision/recall against the hand-tuned `MIN_SCORE`.

**Then**
12. On-chain new-pair listening via factory logs (§1).
13. Modularise `bot.py` and add tests for the trading paths with a mocked web3.

---

## 7. Enabling what was added

```bash
# 1. nothing to install — signals.py / discovery.py are stdlib-only
python -m unittest test_signals test_discovery test_features   # 57 tests

# 2. .env
USE_SIGNALS=true
USE_GECKOTERMINAL=true
GT_SOURCES=new_pools,trending
ALLOWED_USER_IDS=<your telegram user id>          # optional
SIG_HOLDER_STANCE=pump                            # reward concentrated supply
SIG_MIN_LIQUIDITY_USD=4000                        # all thresholds tunable
LOG_FEATURES=true                                 # collect training data (Phase 2)

# 3. keep paper mode on and watch the "Signal Engine" line in alerts
PAPER_TRADING=true
VERBOSE_LOGGING=true
```

With `USE_SIGNALS=true`, alerts now include a `Signal Engine: +bonus / -penalty`
line and the contributing reasons, and rejected tokens are logged as
`Signal reject SYMBOL@chain: reason` under `VERBOSE_LOGGING`.

**Important:** `USE_SIGNALS` *adds* `bonus − penalty` to the existing 0–100 score.
Until you backtest (see §5.2), keep `MIN_SCORE` where it is and treat the signal
line as an explanation, not as a calibrated probability.

---

## 8. Phase 2 — feature logging for offline model training

Enable with `LOG_FEATURES=true` (default **false** so a long-running bot cannot
fill the disk). `evaluate_token` now builds a feature snapshot for **every**
evaluation — including hard rejects — and calls `FEATURE_LOGGER.log_row()`, which
only does a `queue.put_nowait`. A daemon thread (`FeatureLogger._run`) owns its own
SQLite connection and drains the queue in batches, so the scanner never blocks on
disk I/O. When the queue is full, rows are dropped and counted rather than stalled.

`features` table columns (abridged):

* ids/time: `ts_utc`, `ts_epoch`, `chain`, `token_address`, `pair_address`, `symbol`, `source`
* market: `age_minutes`, `liquidity_usd`, `market_cap_usd`, `fdv_usd`, `price_usd`, `price_native`
* volume: `vol_5m/15m/1h/6h/24h`, `vol_liq_ratio`, `vol_5m_1h`, `vol_1h_6h`, `vol_6h_24h`
* activity: `buys_5m`, `sells_5m`, `buyers_5m`, `sellers_5m`, `buys_1h`, `sells_1h`, `buy_ratio_5m/1h`
* price: `chg_5m/15m/1h/6h/24h`
* holders: `top10`, `top50`, `top100`, `holder_count`, `creator_pct`
* security: `buy_tax`, `sell_tax`, `is_honeypot`, `is_open_source`, `is_proxy`,
  `is_mintable`, `owner_change_balance`, `transfer_pausable`, `slippage_modifiable`,
  `hidden_owner`, `cannot_sell_all`, `selfdestruct`, `trading_cooldown`,
  `is_blacklisted`, `is_whitelisted`, `lp_locked`, `security_source`, `security_known`
* model targets/context: `signal_bonus`, `signal_penalty`, `signal_notes`,
  `base_score`, `hand_score`, `phase1_pass`, `rejected`, `reject_reasons`,
  `passed_threshold`, `alert_sent`, `paper_mode`

Because the bot re-evaluates the same token every scan, the table is also the
price time-series. `label_outcomes.py` walks later rows for the same
`(chain, token_address)` and writes the forward maximum multiple per horizon
(`max_mult_1h`, `max_mult_6h`, `max_mult_24h`) plus binary labels
(`hit_2x_1h`, `hit_3x_24h`, `hit_5x_24h`, …). `--export training.csv` dumps a
sklearn-ready CSV; `--fetch-current` labels the newest rows from live prices.

Caveat for modelling: rows for the same token are **not** independent. Split by
token, not by row, or you will leak the future into the training set.

Tests: `python -m unittest -v test_signals test_discovery test_features` (57).

