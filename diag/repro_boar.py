"""Diagnostic: why boar@base was hard-rejected, and what the bar looks like.

Run:  bot-env/bin/python repro_boar.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_TOKEN", "x")
os.environ.setdefault("CHAT_ID", "1")

for name in ("web3", "telegram", "fastapi", "uvicorn", "eth_account"):
    try:
        __import__(name)
    except Exception:
        m = types.ModuleType(name)
        if name == "web3":
            m.Web3 = object
            m.HTTPProvider = object
        sys.modules[name] = m

import bot  # noqa: E402
import signals  # noqa: E402


def pair(**over):
    p = {
        "chainId": "base",
        "dexId": "uniswap",
        "pairAddress": "0xef256e214c45aab706ca54d6e2c5d0ca42b87895a43e97b382ea012d13e78e49",
        "baseToken": {"address": "0x0cbf291ba052174879d90bf781df1a5f2bc5bb07",
                      "symbol": "boar", "name": "boar"},
        "quoteToken": {"address": "0x4200000000000000000000000000000000000006", "symbol": "WETH"},
        "priceUsd": "0.000016624",
        "liquidity": {"usd": 392000.0},
        "fdv": 1660000.0,
        "marketCap": 1660000.0,
        "volume": {"m5": 12500.0, "m15": 30000.0, "h1": 150000.0,
                   "h6": 900000.0, "h24": 4000000.0},
        "txns": {"m5": {"buys": 30, "sells": 25, "buyers": 22, "sellers": 18},
                 "m15": {"buys": 80, "sells": 60, "buyers": 60, "sellers": 50},
                 "h1": {"buys": 380, "sells": 220, "buyers": 200, "sellers": 150},
                 "h6": {"buys": 1600, "sells": 1200, "buyers": 900, "sellers": 800},
                 "h24": {"buys": 7300, "sells": 6000, "buyers": 3000, "sellers": 2800}},
        "priceChange": {"m5": 8.0, "m15": 20.0, "h1": 30.0, "h6": -24.0, "h24": -48.0},
        "pairCreatedAt": 1790391481000,
        "info": None,
        "boosts": None,
        "source": "geckoterminal:trending",
    }
    p.update(over)
    return p


SEC = {
    "is_honeypot": False, "buy_tax": 0.0, "sell_tax": 0.0,
    "is_whitelisted": False, "is_blacklisted": False, "is_open_source": True,
    "is_proxy": False, "can_take_back_ownership": False, "owner_change_balance": False,
    "is_mintable": False, "slippage_modifiable": False, "transfer_pausable": False,
    "lp_locked": None, "hidden_owner": False, "cannot_sell_all": False,
    "selfdestruct": False, "trading_cooldown": False, "holder_count": 3000,
    "creator_percent": 0.0, "source": "goplus",
}

base = pair()
cases = {
    "A  as-lived 18:15  (5m vol $12.5k, liq $392k, chg_5m +8%)":
        base,
    "B  real 18:10 pump bar (5m vol $62k, chg_5m +30%)":
        pair(volume={**base["volume"], "m5": 62000.0},
             priceChange={**base["priceChange"], "m5": 30.0}),
    "C  same as A but only $60k of liquidity (thin old pool)":
        pair(liquidity={"usd": 60000.0}),
    "D  as-lived but 5m tape flips to sells (chg_5m -40%)":
        pair(priceChange={**base["priceChange"], "m5": -40.0},
             txns={**base["txns"], "m5": {"buys": 8, "sells": 40, "buyers": 7, "sellers": 30}}),
}

print("=== signals.hard_reject_reasons (the veto that logged 'Signal reject') ===")
for label, p in cases.items():
    print(f"{label}\n    -> {signals.hard_reject_reasons(p, security=SEC)}")

print()
print("=== what the bar asks for, base chain, SCORE_NORMALIZE=true ===")
for age in (10, 30, 60, 120, 400):
    ceil = bot.max_possible_score("base", age)
    print(f"age {age:>4}m  reachable={ceil:5.1f}/100  threshold={bot.effective_threshold('base', age):5.1f}"
          f"  (floor MIN_EFFECTIVE_SCORE={os.getenv('MIN_EFFECTIVE_SCORE', '35')})")
