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
| `USE_GECKOTERMINAL` | `false` | Discover candidates via GeckoTerminal instead of DexScreener's paid boost/profile lists. Recommended `true`. |
| `USE_SIGNALS` | `false` | Enable the signal engine (runner bonus / false-positive filters). |
| `SIG_HOLDER_STANCE` | `pump` | `pump` rewards concentrated supply (early runners); `rug` penalises it. |
| `ALLOWED_USER_IDS` | — | Extra Telegram allowlist when `CHAT_ID` is a group. |

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
| `/risk <usd>` | Set risk per trade (stored). |
| `/setamounts <chain> <a,b,c>` | Set preset buy sizes. |
| `/debug` | Log per-chain config and contract status. |

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
python -m unittest -v test_signals test_discovery
```

## Security

- Use a **dedicated hot wallet** with only what you can afford to lose, and start
  in paper mode.
- `.env` is gitignored — never commit it or paste its contents anywhere.
- Only `CHAT_ID` (plus `ALLOWED_USER_IDS`) can control the bot.
- Rotate `TELEGRAM_TOKEN` if it is ever exposed (e.g. in logs).

## Notes

- Default discovery uses DexScreener's paid boost/profile lists because the
  DexScreener "all pairs" endpoint is dead (404). Set `USE_GECKOTERMINAL=true` for
  real `new_pools`/`trending` discovery.
- Only Uniswap/Pancake-style V3 + V2 routes are supported for swaps. Liquidity on
  Aerodrome (Base) or V4 venues may not be tradeable.
- See [`REVIEW.md`](REVIEW.md) for the detailed code review, known issues and
  roadmap.
