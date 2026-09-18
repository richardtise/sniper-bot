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
| `MIN_SCORE` | `65` | Alert threshold (0–100). |
| `MAX_ALLOWED_TAX` | `10` | Reject tokens above this buy/sell tax %. |
| `MORALIS_API_KEY` | — | Enables holder-concentration scoring. |
| `COINGECKO_API_KEY` | — | Enables CEX-listing scoring. |
| `USE_GECKOTERMINAL` | `false` | Keep `false` for the original DexScreener discovery. `true` finds pools earlier but adds noise; pair it with `USE_SIGNALS=true` and the AND-gates below. |
| `GT_SOURCES` | `trending` | `trending` = momentum (cleaner). `new_pools` = earliest, noisiest. |
| `USE_SIGNALS` | `false` | Enable the signal engine. It only **removes** candidates by default (rejects + penalties), using unique-buyer data DexScreener doesn't provide. |
| `SIGNAL_BONUS_WEIGHT` | `0.0` | Weight on the signal bonus. `0.0` means the hand-tuned `MIN_SCORE` stays the gate; a bonus can never create an alert. |
| `EARLY_RUNNER_MODE` | `false` | Lets a strong *young* pool alert (its long volume windows are empty, so it can't reach `MIN_SCORE`). Every AND-condition in `SIG_EARLY_*` must hold. |
| `ALLOW_SECURITY_FALLBACK` | `false` | `false` drops tokens GoPlus doesn't know (original behaviour). `true` accepts a simulated honeypot.is record instead. |
| `SIG_HOLDER_STANCE` | `pump` | `pump` rewards concentrated supply (early runners); `rug` penalises it. |
| `BASE_MIN_LIQUIDITY_USD` etc. | — | Per-chain floors. Use these to tighten one noisy chain without changing the rest. |
| `ALLOWED_USER_IDS` | — | Extra Telegram allowlist when `CHAT_ID` is a group. |
| `LOG_FEATURES` | `false` | Log a feature row for every token evaluation (see [Training data](#training-data)). |

Per-chain routing addresses (`ETH_QUOTER_V2`, `BSC_ROUTER_V3`, …) can be
overridden in `.env`, but working defaults are compiled in for all four chains.
`/debug` prints each contract as `OK` or `NO CODE` so a bad address is obvious.

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

## Training data

Set `LOG_FEATURES=true` and the bot writes one row per token evaluation — including
rejections — into the `features` table: liquidity/mcap, volume windows and ratios,
buy/sell counts and unique buyers, price changes, holder concentration, taxes and
security flags, signal-engine bonus/penalty, the hand-tuned score, and whether an
alert was sent. It is fire-and-forget (a background thread writes it), so the
scanner never slows down.

`label_outcomes.py` turns those rows into supervised labels using the forward
maximum price each token reached (the repeated scans act as the price series):

```bash
python label_outcomes.py                      # writes max_mult_1h/6h/24h + hit_Nx_* labels
python label_outcomes.py --fetch-current      # also label the newest rows from live prices
python label_outcomes.py --export training.csv
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
