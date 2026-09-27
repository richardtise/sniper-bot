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

The handful you are most likely to change:

| Variable | Default | Purpose |
|---|---|---|
| `PAPER_TRADING` | `true` | Simulate trades. Set to `false` only with a funded hot wallet. |
| `MIN_SCORE` | `65` | Alert threshold (0–100), scaled per chain — see [Alert gate](#alert-gate). |
| `SCORE_NORMALIZE` | `true` | Scale the alert bar to the points actually reachable on a chain/age. `false` gates on raw `MIN_SCORE`. |
| `MIN_EFFECTIVE_SCORE` | `35` | Floor for the scaled threshold, so scaling can't become a rubber stamp. |
| `MAX_ALLOWED_TAX` | `10` | Reject tokens above this buy/sell tax %. |
| `ETHERSCAN_API_KEY` | — | Contract verification on all four chains, including Robinhood (chainid 4663). `SCANNER_API_KEY` is accepted as an alias. |
| `MORALIS_API_KEY` | — | Optional. Only used for an exact top-100 figure on BSC/ETH/Base. **Not** needed for holder scoring, and not supported on Robinhood. |
| `COINGECKO_API_KEY` | — | Enables CEX-listing scoring (never applies to Robinhood). |
| `USE_GECKOTERMINAL` | `true` | GeckoTerminal discovery. The DexScreener alternative is only the paid-boost shill list, so prefer `true`. |
| `GT_SOURCES` | `new_pools,trending` | `trending` = momentum (cleaner). `new_pools` = earliest, noisiest. |
| `USE_SIGNALS` | `false` | Enable the signal engine. It only **removes** candidates by default (rejects + penalties), using unique-buyer data DexScreener doesn't provide. The **code default is `false`** — `.env.example` sets `USE_SIGNALS=true`, and you must copy that across or `signals.py` is never called and none of the `SIG_*` filters below do anything. |
| `SIGNAL_BONUS_WEIGHT` | `0.0` | Weight on the signal bonus. `0.0` means the hand-tuned score stays the gate; a bonus can never create an alert. |
| `EARLY_RUNNER_MODE` | `false` | Lets a strong *young* pool alert (its long volume windows are empty, so it can't reach the threshold). Every AND-condition in `SIG_EARLY_*` must hold. |
| `SIG_MAX_AGE_MINUTES` | `0` | `0` = **no upper age limit**. Pool age isn't a quality signal; the activity floors already reject dead pools. Set a number to restore a hard cap. |
| `SIGNAL_BONUS_WEIGHT` | `0.0` | How far the signal engine may *promote* a token (0–1). `0.0` = signals only veto. Raising it lets runner signals rescue a token the hand score under-rates — see [Catching re-ignited pools](#catching-re-ignited-pools). |
| `ALLOW_SECURITY_FALLBACK` | `false` | `false` drops tokens GoPlus doesn't know (original behaviour). `true` accepts a simulated honeypot.is record instead. |
| `SIG_REQUIRE_OPEN_SOURCE` | `false` | Require a verified contract source. Leave `false` — most Robinhood tokens are unverified, including the ones that run. |
| `SIG_HOLDER_STANCE` | `pump` | `pump` rewards concentrated supply (early runners); `rug` penalises it. |
| `ROBINHOOD_MIN_SCORE` / `BASE_MIN_SCORE` etc. | — | Per-chain threshold override, same convention as the floors below. |
| `BASE_MIN_LIQUIDITY_USD` etc. | — | Per-chain floors. Use these to tighten one noisy chain without changing the rest. |
| `ALLOWED_USER_IDS` | — | Extra Telegram allowlist when `CHAT_ID` is a group. |
| `LOG_FEATURES` | `false` | Log a feature row for every token evaluation (see [Training data](#training-data)). |
| `TRY_BLOCKSCOUT_HOLDERS` | `false` | Retry Blockscout for Robinhood holders. Off because that host answers with a Cloudflare challenge. |

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

## Alert gate

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
open positions. Works on Render or any host that injects `PORT`. SQLite
(`pump_bot_v5.db`) and logs are local, so attach a disk if state must survive
redeploys.

## Tests

```bash
python -m unittest -v test_signals test_discovery test_features
```

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

```bash
bot-env/bin/python diag/component_audit.py --pages 3 --csv diag/.cache/audit.csv
bot-env/bin/python diag/holders_from_logs.py 0xTokenAddress
bot-env/bin/python diag/compare_decisions.py            # HEAD vs working tree
python -m unittest -v test_audit_tools
```

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
  original bot. Signals only veto and penalise (`SIGNAL_BONUS_WEIGHT=0.0` by
  default). Tighten a noisy chain directly with `BASE_MIN_LIQUIDITY_USD`,
  `BASE_MIN_VOL_5M_USD`, `BASE_MIN_MARKET_CAP_USD`.
- **Early runners.** `EARLY_RUNNER_MODE=true` + `USE_GECKOTERMINAL=true` gives a
  young pool a chance to alert even though it can't reach `MIN_SCORE`. It is
  AND-gated, so it will not fire on a dead, illiquid or wash-traded pool.
- Default discovery uses DexScreener's boost/profile lists because the DexScreener
  "all pairs" endpoint is dead (404).
- Only Uniswap/Pancake-style V3 + V2 routes are supported for swaps. Liquidity on
  Aerodrome (Base) or V4 venues may not be tradeable.
- See [`REVIEW.md`](REVIEW.md) for the detailed code review, known issues and
  roadmap.
