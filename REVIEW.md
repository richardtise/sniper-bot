# Code review — my-sniper-bot

Reviewed: `bot.py` (2,242 lines after these changes), `requirements.txt`, `start.sh`,
`test_telegram.py`, `.env` (names only), `crime_pump.log`.

**Verdict in one line:** the trading/execution half is solid and carefully written, but
the *finding* half is the weak link — and the single biggest reason the bot surfaces
false positives is that its primary discovery endpoint does not exist.

This document is ordered by impact. Items marked **P0** are worth fixing today.
Items marked **✅ fixed** were changed in this pass; everything else is a recommendation.

---

## 0. What was added in this pass

| File | Purpose |
| --- | --- |
| `signals.py` | Pure runner/false-positive engine: hard rug filters + runner bonus/penalty, cross-scan acceleration history. 25 unit tests. |
| `discovery.py` | Working candidate discovery via GeckoTerminal (free, no key), mapped to the pair shape `bot.evaluate_token` already consumes. 10 unit tests. |
| `test_signals.py`, `test_discovery.py` | `python -m unittest` suites, 35 tests, no network. |
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

### 2.4 `/health` leaks your wallet address

`health_check()` (bot.py:2234) returns `"wallet": WALLET_ADDRESS` on a server bound
to `0.0.0.0`. If the port is ever public (Render gives it a URL), anyone can link
your wallet to the bot. Drop the wallet field or require a header token.

### 2.5 Infinite approvals

`ensure_token_approval()` (bot.py:1110) approves `2**256 - 1`. That is standard for
bots, but it means any future spender-router bug drains the token. Consider
approving exactly `sell_amount` per trade, or at least document the risk.

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
2. **No backtest / no feedback loop.** You cannot tune false positives without
   labels. Recommended minimum:
   * log every *evaluated* candidate with its feature vector and the reject reason
     (not just alerts),
   * snapshot price and liquidity at +15m/+1h/+6h for each,
   * add a `/stats` command over a new `trades` table with realized P&L per entry
     score bucket,
   * then measure precision/recall of each rule before changing a threshold.
   Right now `positions.pnl_pct` is recorded inconsistently (see 4.3, 4.16), so
   even the alert→outcome loop is unreliable.
3. **Blocking handler loop.** `telegram_polling_task` awaits each update inline; a
   buy that waits up to 120s for a receipt stalls all further commands. Dispatch
   each update as a task (or move to a webhook).
4. **No retry/backoff on Telegram 429.** A burst of alerts can be dropped. Catch
   `RetryAfter` and sleep.
5. **HTML injection / parse errors.** Symbols are interpolated into `ParseMode.HTML`
   unescaped — a token named `<b>` or `A&B` breaks the message (the old
   `crime_pump.log` literally shows a 400 parse error on the startup message).
   Wrap dynamic values in `html.escape`.
6. **`test_telegram.py` imports `telebot` (pyTelegramBotAPI)**, which is not in
   `requirements.txt` and not used by `bot.py`. Either add it or delete the file.
7. **`.env` contains unused keys**: `SCANNER_API_KEY`, `AUTO_BUY_ENABLED`,
   `AUTO_BUY_AMOUNT`, `AUTO_BUY_MIN_SCORE`. `AUTO_BUY_*` in particular implies an
   auto-buy feature that does not exist — remove or implement.

---

## 6. Recommended roadmap (highest value first)

**Do now**
1. Revoke the leaked Telegram token (§2.1).
2. Set `USE_GECKOTERMINAL=true` and `USE_SIGNALS=true`; leave `PAPER_TRADING=true`.
3. Fix `get_buy_amounts` keying and decide what `/risk` means (§4.4, §4.5).
4. Holder concentration already aligns: `score_holder` and the default
   `SIG_HOLDER_STANCE=pump` both reward it (§3.1).

**Next**
5. Add GeckoTerminal token-info enrichment (`gt_score`, socials, holder
   distribution, dev holding) for candidates that pass the phase-1 gate — it is
   free and directly improves both legitimacy scoring and rug filtering.
6. Add a `trades` table + `/stats`, and start logging outcomes for tuning (§5.2).
7. Add the honeypot.is fallback and split "failed lookup" from "unsafe" (§4.6).
8. Add nonce locking and EIP-1559 gas (§4.7, §4.8).
9. Router-agnostic execution: add per-`dexId` routing or an aggregator quote so
   Aerodrome (Base) and V4/other venues are tradeable (§4.0).

**Then**
9. On-chain new-pair listening via factory logs (§1).
10. Router-agnostic execution via aggregator quotes (1inch/OpenOcean) to reduce
    failed V3 fee-tier hunts.
11. Modularise `bot.py` and add tests for the trading paths with a mocked web3.

---

## 7. Enabling what was added

```bash
# 1. nothing to install — signals.py / discovery.py are stdlib-only
python -m unittest test_signals test_discovery   # 36 tests

# 2. .env
USE_SIGNALS=true
USE_GECKOTERMINAL=true
GT_SOURCES=new_pools,trending
ALLOWED_USER_IDS=<your telegram user id>          # optional
SIG_HOLDER_STANCE=pump                            # reward concentrated supply
SIG_MIN_LIQUIDITY_USD=4000                        # all thresholds tunable

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
