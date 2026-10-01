"""What the bot's own components would have scored during boar's two legs.

Reconstructs the trailing windows from 5m candles and calls the real scoring
functions, so it is the bot's model evaluated on the real tape — not an
estimate of a different model. Only the 35 points derivable from candles are
shown; liquidity history and per-window transaction counts are not published
retroactively, so the totals are a floor, not the full hand score.

Everything runs under ``main()``: importing this module used to fetch live
OHLCV and print a table, which made `import diag.legs_boar` a network call —
the wrong shape for a module a test or an audit might import.
"""
import os, sys, time, urllib.request, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import diag.component_audit as audit

POOL = "0xef256e214c45aab706ca54d6e2c5d0ca42b87895a43e97b382ea012d13e78e49"
CREATED = time.mktime(time.strptime("2026-09-26T02:58:01Z", "%Y-%m-%dT%H:%M:%SZ")) - time.timezone


def main():
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/{POOL}/ohlcv/minute"
           f"?aggregate=5&limit=1000&currency=usd&token=base")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "x"}), timeout=30) as fh:
        rows = sorted(json.load(fh)["data"]["attributes"]["ohlcv_list"], key=lambda r: r[0])
    obs = audit.build_observations(rows, CREATED, step=1, lookback=24, allow_partial=True)
    audit.score_observations(obs)

    import bot  # noqa: F401  — the real scorers live there

    def bar(ts_utc):
        t = time.mktime(time.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        for o in obs:
            if abs(o["ts"] - t) < 1:
                return o
        return None

    print(f"{'time UTC':16s} {'age(m)':>7s} {'mcap$M':>7s} {'s5m1h':>6s} {'s1h6h':>6s} "
          f"{'s6h24h':>7s} {'price':>6s} {'SUM/35':>7s} {'fwd1h%':>8s} {'fwd6h%':>8s}")
    for label in ("2026-09-26T09:00:00Z", "2026-09-26T09:15:00Z", "2026-09-26T09:30:00Z",
                  "2026-09-26T13:53:00Z", "2026-09-26T15:45:00Z", "2026-09-26T16:00:00Z",
                  "2026-09-26T16:15:00Z", "2026-09-26T16:45:00Z", "2026-09-26T17:30:00Z"):
        o = bar(label)
        if not o:
            continue
        total = o["score_total_testable"]
        # mcap from the candle close: supply 100e9
        closes = [float(r[4]) for r in rows]
        idx = next(i for i, r in enumerate(rows) if abs(float(r[0]) + 300 - o["ts"]) < 1)
        mcap = closes[idx] * 100e9 / 1e6
        print(f"{label[:16]:16s} {o['age_minutes']:7.0f} {mcap:7.2f} "
              f"{o['score_vol_5m_1h']:6.1f} {o['score_vol_1h_6h']:6.1f} "
              f"{o['score_vol_6h_24h']:7.1f} {o['score_price']:6.1f} {total:7.1f} "
              f"{o['fwd_max_1h']:8.1f} {o['fwd_max_6h']:8.1f}")
    print("\n(SUM/35 = the 4 candle-derivable components. Full hand score also includes")
    print(" vol/liq up to 10, buy pressure up to 20, holders ~2.5 for boar, security 10.)")


if __name__ == "__main__":
    main()
