"""Show the feature row the bot now writes for a real token, end to end.

Feeds a live DexScreener pair (the exact shape ``evaluate_token`` consumes)
through the real evaluation path and prints the row that reaches the features
table. Used to verify the score-breakdown capture on real data.

    bot-env/bin/python diag/log_boar_row.py [token_address]
"""
import asyncio
import json
import os
import sys
import types
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TELEGRAM_TOKEN", "x")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")

for name in ("web3", "telegram", "fastapi", "uvicorn", "eth_account"):
    try:
        __import__(name)
    except Exception:
        mod = types.ModuleType(name)
        if name == "web3":
            mod.Web3 = object
            mod.HTTPProvider = object
        sys.modules[name] = mod

import bot  # noqa: E402

DEFAULT_TOKEN = "0x0cbf291Ba052174879d90bf781dF1A5F2BC5Bb07"  # boar, Base


class Recorder:
    def __init__(self):
        self.rows = []

    def log_row(self, row):
        self.rows.append(dict(row))

    def mark_alert_sent(self, *a, **k):
        pass

    def stats(self):
        return {}


def fetch_pair(token):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": "sniper-bot-diag"}), timeout=25) as fh:
        data = json.load(fh)
    pairs = [p for p in (data.get("pairs") or []) if p.get("liquidity")]
    if not pairs:
        raise SystemExit("no DexScreener pools with liquidity for that token")
    return max(pairs, key=lambda p: float(p["liquidity"].get("usd") or 0))


async def main():
    token = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TOKEN
    pair = fetch_pair(token)
    pair["source"] = "diag:dexscreener"

    rec = Recorder()
    bot.FEATURE_LOGGER = rec

    async def fake_security(session, chain, tkn):
        sec = bot._security_placeholder("diagnostic")
        sec.update({"is_open_source": True, "lp_locked": None})
        return sec

    async def fake_holders(session, chain, tkn):
        return bot.HolderData()

    async def fake_cex(session, chain, tkn):
        return (0, False, 0)

    bot.get_token_security = fake_security
    bot.get_holder_concentration = fake_holders
    bot.get_cex_listings = fake_cex

    result = await bot.evaluate_token(None, pair)
    if not rec.rows:
        raise SystemExit("evaluate_token logged nothing")
    row = rec.rows[0]

    print(f"\n{row['symbol']}@{row['chain']}  pool={row['pair_address']}")
    print(f"  source               {row['source']}")
    print(f"  age / liquidity      {row['age_minutes']} min / ${row['liquidity_usd']:,.0f}")
    print(f"  price / mcap         ${row['price_usd']} / ${row['market_cap_usd']:,.0f}")
    print(f"  volume 5m/1h/6h/24h  "
          f"${row['vol_5m']:,.0f} / ${row['vol_1h']:,.0f} / "
          f"${row['vol_6h']:,.0f} / ${row['vol_24h']:,.0f}")
    print(f"  ratios 5m-liq 5m-1h 1h-6h 6h-24h  "
          f"{row['vol_liq_ratio']:.4f} {row['vol_5m_1h']:.4f} "
          f"{row['vol_1h_6h']:.4f} {row['vol_6h_24h']:.4f}")
    print(f"  txns 5m buys/sells   {row['buys_5m']}/{row['sells_5m']}  "
          f"buy_ratio={row['buy_ratio_5m']:.3f}")
    print("  --- score breakdown (previously not recorded) ---")
    parts = [
        ("vol_5m/liquidity", row["score_vol_liq"]),
        ("vol_5m/vol_1h", row["score_vol_5m_1h"]),
        ("vol_1h/vol_6h", row["score_vol_1h_6h"]),
        ("vol_6h/vol_24h", row["score_vol_6h_24h"]),
        ("buy pressure 5m", row["score_buy_5m"]),
        ("buy pressure 1h", row["score_buy_1h"]),
        ("price change", row["score_price"]),
        ("holders", row["score_holder"]),
        ("security", row["score_security"]),
        ("cex", row["score_cex"]),
    ]
    for label, value in parts:
        shown = "not reached" if value is None else f"{float(value):5.2f}"
        print(f"    {label:20s} {shown}")
    print(f"    {'penalties':20s} {row['penalties_total']}")
    print(f"    {'base_score':20s} {row['base_score']}")
    print(f"    {'hand_score':20s} {row['hand_score']}")
    print(f"    {'ceiling_score':20s} {row['ceiling_score']}")
    print(f"    {'alert_threshold':20s} {row['alert_threshold']}")
    print(f"    {'reject_reasons':20s} {row['reject_reasons']!r}")
    print(f"\n  alert: {'YES' if result else 'no'}")


if __name__ == "__main__":
    asyncio.run(main())
