"""Prove a code change to the evaluation path is decision-neutral.

Loads two revisions of ``bot.py`` side by side and runs an identical fixture
matrix through both, comparing the reject reason and the alert decision for
every case. Use it before shipping a refactor of ``evaluate_token``: a change
that is meant to alter only logging, metrics or plumbing must produce the same
table.

    bot-env/bin/python diag/compare_decisions.py                 # HEAD vs working tree
    bot-env/bin/python diag/compare_decisions.py --old 9458812   # a specific revision

Exit code is 0 when every case matches, 1 when any case differs, so it can gate
a commit. It is deliberately NOT a golden test of the scores themselves — the
scoring model is expected to change, and pinning its current output would fight
that. This tool answers one narrow question: "did behaviour change by accident?"
"""
import argparse
import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "x")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")
sys.path.insert(0, ROOT)

for _name in ("web3", "telegram", "fastapi", "uvicorn", "eth_account"):
    try:
        __import__(_name)
    except Exception:
        _mod = types.ModuleType(_name)
        if _name == "web3":
            _mod.Web3 = object
            _mod.HTTPProvider = object
        sys.modules[_name] = _mod


class _Recorder:
    def __init__(self):
        self.rows = []

    def log_row(self, row):
        self.rows.append(dict(row))

    def mark_alert_sent(self, *a, **k):
        pass

    def stats(self):
        return {}


def _load(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _revision_source(ref, dest):
    """Write ``git show <ref>:bot.py`` to ``dest`` and return the path."""
    with open(dest, "wb") as fh:
        fh.write(subprocess.check_output(["git", "show", f"{ref}:bot.py"], cwd=ROOT))
    return dest


def _pair(**over):
    now = time.time()
    pair = {
        "chainId": "base", "dexId": "uniswap_v3", "pairAddress": "0x" + "ab" * 20,
        "baseToken": {"address": "0x" + "cd" * 20, "symbol": "T", "name": "T"},
        "quoteToken": {"symbol": "WETH", "address": "0x" + "ef" * 20},
        "priceUsd": "0.001", "priceNative": "0.000001",
        "liquidity": {"usd": 50_000.0}, "marketCap": 500_000.0,
        "volume": {"m5": 5_000.0, "h1": 30_000.0, "h6": 90_000.0, "h24": 200_000.0},
        "txns": {"m5": {"buys": 30, "sells": 15, "buyers": 25, "sellers": 10},
                 "h1": {"buys": 120, "sells": 60}},
        "priceChange": {"m5": 10.0, "h1": 40.0, "h6": 60.0, "h24": 80.0},
        "pairCreatedAt": (now - 6 * 3600) * 1000,
        "fdv": 500_000.0, "source": "matrix",
    }
    pair.update(over)
    return pair


def _cases():
    good = {"is_open_source": True}
    return [
        # label, pair, security overlay, ALERT_THRESHOLD
        ("alert_eligible", _pair(), good, 0),
        ("floor_liquidity", _pair(liquidity={"usd": 10.0}), good, 0),
        ("floor_price", _pair(priceUsd="0.0"), good, 0),
        ("floor_mcap", _pair(marketCap=1.0), good, 0),
        ("floor_vol5m", _pair(volume={"m5": 1.0, "h1": 30_000.0,
                                      "h6": 90_000.0, "h24": 200_000.0}), good, 0),
        ("signal_low_activity",
         _pair(liquidity={"usd": 400_000.0},
               volume={"m5": 6_000.0, "h1": 120_000.0,
                       "h6": 400_000.0, "h24": 900_000.0}), good, 0),
        ("signal_sell_dominated",
         _pair(txns={"m5": {"buys": 3, "sells": 40, "buyers": 3, "sellers": 30},
                     "h1": {"buys": 120, "sells": 60}}), good, 0),
        ("signal_active_dump",
         _pair(priceChange={"m5": -45.0, "h1": 40.0, "h6": 60.0, "h24": 80.0},
               txns={"m5": {"buys": 5, "sells": 40, "buyers": 5, "sellers": 30},
                     "h1": {"buys": 120, "sells": 60}}), good, 0),
        ("security_honeypot", _pair(), {"is_honeypot": True}, 0),
        ("security_tax", _pair(), {"buy_tax": 99.0}, 0),
        ("security_risky_flag", _pair(), {"is_mintable": True}, 0),
        ("below_threshold", _pair(), good, 65),
        ("robinhood_too_new",
         _pair(chainId="robinhood", pairCreatedAt=(time.time() - 60) * 1000), good, 0),
    ]


async def _run(module, pair, security_overlay, alert_threshold):
    recorder = _Recorder()
    module.FEATURE_LOGGER = recorder
    module.ALERT_THRESHOLD = alert_threshold
    module.PAIR_HISTORY = module.signals.PairHistory()
    # The signal engine is opt-in (default false in bot.py), so enable it
    # explicitly: otherwise every signal-veto case falls through to the hand
    # gate and the matrix would silently stop testing those paths.
    module.USE_SIGNALS = True
    module.SIGNAL_FILTERS = module.signals.Filters()
    module.SIGNAL_BONUS_WEIGHT = 0.0
    module.security_cache.clear()

    async def fake_security(session, chain, token):
        record = module._security_placeholder("matrix")
        record.update(security_overlay)
        return record

    async def fake_holders(session, chain, token):
        return module.HolderData(top10=40.0, top50=60.0, top100=None, source="matrix")

    async def fake_cex(session, chain, token):
        return (0, False, 0)

    module.get_token_security = fake_security
    module.get_holder_concentration = fake_holders
    module.get_cex_listings = fake_cex

    result = await module.evaluate_token(None, dict(pair))
    row = recorder.rows[-1] if recorder.rows else {}
    return {
        "alerted": result is not None,
        "reasons": row.get("reject_reasons"),
        "hand": row.get("hand_score"),
    }


async def main(old_ref, new_path, verbose):
    tmpdir = tempfile.mkdtemp(prefix="sniper-compare-")
    old_path = _revision_source(old_ref, os.path.join(tmpdir, "old_bot.py"))
    old = _load("compare_old_bot", old_path)
    new = _load("compare_new_bot", new_path)

    print(f"old = git {old_ref}:bot.py\nnew = {new_path}\n")
    print(f"{'case':24s} {'old decision':>34s} | {'new decision':>34s}  match")
    differences = []
    for label, pair, overlay, threshold in _cases():
        a = await _run(old, pair, overlay, threshold)
        b = await _run(new, pair, overlay, threshold)
        same = (a["alerted"] == b["alerted"] and a["reasons"] == b["reasons"]
                and a["hand"] == b["hand"])
        if not same:
            differences.append(label)
        fa = f"{a['reasons']} alert={a['alerted']}"
        fb = f"{b['reasons']} alert={b['alerted']}"
        print(f"{label:24s} {fa:>34s} | {fb:>34s}  {'OK' if same else 'DIFF'}")
        if verbose and not same:
            print(f"    old hand={a['hand']}  new hand={b['hand']}")

    if differences:
        print(f"\n{differences} changed behaviour — NOT decision-neutral")
        return 1
    print("\nAll cases match — decision-neutral")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", default="HEAD", help="git revision to compare from")
    parser.add_argument("--new", default=os.path.join(ROOT, "bot.py"),
                        help="path to the candidate bot.py")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.old, args.new, args.verbose)))
