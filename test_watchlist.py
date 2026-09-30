"""Tests for the ignition watchlist — the lane that re-prices pools which fell
out of the discovery feeds.

The behaviour that matters here is *bounded* re-checking: remember what was seen,
re-price only what once looked alive, never faster than a cooldown, never more
than a cap, and forget it after a TTL. A watchlist that re-prices everything,
forever, is just a slower copy of the feed.

Run with: python -m unittest -v test_watchlist
"""

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "1:TEST")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")

import bot  # noqa: E402


class WatchlistTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {
            "DB_PATH": bot.DB_PATH,
            "db_conn": bot.db_conn,
            "WATCHLIST_ENABLED": bot.WATCHLIST_ENABLED,
            "WATCHLIST_MAX": bot.WATCHLIST_MAX,
            "WATCHLIST_TTL_HOURS": bot.WATCHLIST_TTL_HOURS,
            "WATCHLIST_RECHECK_MINUTES": bot.WATCHLIST_RECHECK_MINUTES,
            "WATCHLIST_MIN_BEST_SCORE": bot.WATCHLIST_MIN_BEST_SCORE,
        }
        bot.DB_PATH = os.path.join(self.tmpdir, "watch.db")
        bot.db_conn = bot.init_db()
        bot.WATCHLIST_ENABLED = True
        bot.WATCHLIST_MAX = 40
        bot.WATCHLIST_TTL_HOURS = 24.0
        bot.WATCHLIST_RECHECK_MINUTES = 10.0
        bot.WATCHLIST_MIN_BEST_SCORE = 25.0
        bot.watchlist_cycle_start()

    def tearDown(self):
        try:
            bot.db_conn.close()
        except Exception:
            pass
        for key, value in self._saved.items():
            setattr(bot, key, value)
        bot.watchlist_cycle_start()

    def note(self, token="0xtoken", hand=30.0, chain="base", price=1.0, symbol="T"):
        bot._watchlist_note(chain, token, "0xpair" + token[-3:], symbol,
                            "test", hand, price)


class TestWatchlistPersistence(WatchlistTestBase):
    def test_disabled_lane_records_and_returns_nothing(self):
        bot.WATCHLIST_ENABLED = False
        self.note()
        self.assertEqual(bot.db_watchlist_due(), [])
        rows = bot.db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        self.assertEqual(rows, 0, "a disabled lane must not write")

    def test_remember_updates_high_water_mark(self):
        self.note(hand=30.0)
        bot.watchlist_cycle_start()
        self.note(hand=44.0)
        bot.watchlist_cycle_start()
        self.note(hand=20.0)  # a later, worse look must not lower the mark
        row = bot.db_conn.execute(
            "SELECT best_hand, last_hand, evals FROM watchlist WHERE token_address='0xtoken'"
        ).fetchone()
        self.assertEqual(row[0], 44.0)
        self.assertEqual(row[1], 20.0)
        self.assertEqual(row[2], 3, "one row per cycle, not one per evaluation")

    def test_written_once_per_cycle(self):
        bot.watchlist_cycle_start()
        self.note(hand=30.0)
        self.note(hand=30.0)  # same token again in the same cycle
        evals = bot.db_conn.execute(
            "SELECT evals FROM watchlist WHERE token_address='0xtoken'"
        ).fetchone()[0]
        self.assertEqual(evals, 1)

    def test_due_excludes_pools_that_never_showed_life(self):
        self.note(token="0xdead", hand=10.0)
        self.assertEqual(bot.db_watchlist_due(), [])

        self.note(token="0xalive", hand=30.0)
        due = bot.db_watchlist_due()
        self.assertEqual([d["token_address"] for d in due], ["0xalive"])

    def test_due_respects_recheck_cooldown(self):
        self.note(hand=30.0)
        due = bot.db_watchlist_due()
        self.assertEqual(len(due), 1)

        bot.db_watchlist_mark_checked(due[0]["chain"], due[0]["token_address"])
        self.assertEqual(bot.db_watchlist_due(), [],
                         "just-checked pool must wait out the cooldown")

        # ...but is due again once the cooldown has elapsed.
        past = time.time() - (bot.WATCHLIST_RECHECK_MINUTES * 60 + 1)
        bot.db_watchlist_mark_checked(due[0]["chain"], due[0]["token_address"], past)
        self.assertEqual(len(bot.db_watchlist_due()), 1)

    def test_due_respects_ttl_and_cap_and_ordering(self):
        self.note(token="0xlow", hand=26.0)
        self.note(token="0xhigh", hand=55.0)
        self.note(token="0xmid", hand=40.0)

        bot.WATCHLIST_MAX = 2
        due = bot.db_watchlist_due()
        self.assertEqual([d["token_address"] for d in due], ["0xhigh", "0xmid"],
                         "cap applies, best score first")

        # Push one entry past the TTL: it must drop out.
        stale = time.time() - (bot.WATCHLIST_TTL_HOURS * 3600 + 60)
        bot.db_conn.execute("UPDATE watchlist SET last_seen=? WHERE token_address='0xhigh'", (stale,))
        bot.db_conn.commit()
        bot.WATCHLIST_MAX = 40
        tokens = [d["token_address"] for d in bot.db_watchlist_due()]
        self.assertNotIn("0xhigh", tokens)
        self.assertIn("0xmid", tokens)

    def test_prune_deletes_expired_rows(self):
        self.note(token="0xold", hand=30.0)
        self.note(token="0xnew", hand=30.0)
        stale = time.time() - (bot.WATCHLIST_TTL_HOURS * 3600 + 60)
        bot.db_conn.execute("UPDATE watchlist SET last_seen=? WHERE token_address='0xold'", (stale,))
        bot.db_conn.commit()

        removed = bot.db_watchlist_prune()
        self.assertEqual(removed, 1)
        left = [r[0] for r in bot.db_conn.execute("SELECT token_address FROM watchlist")]
        self.assertEqual(left, ["0xnew"])

    def test_prune_is_a_no_op_when_disabled(self):
        self.note(token="0xold", hand=30.0)
        bot.WATCHLIST_ENABLED = False
        self.assertEqual(bot.db_watchlist_prune(), 0)


class TestWatchlistFetching(WatchlistTestBase):
    def test_get_watchlist_pair_matches_the_requested_pool(self):
        calls = []

        async def fake_fetch(session, url, headers=None, **kw):
            calls.append(url)
            return {"pairs": [
                {"pairAddress": "0xOTHER", "chainId": "base"},
                {"pairAddress": "0xABC", "chainId": "base", "baseToken": {"symbol": "T"}},
            ]}

        saved = bot.fetch_json
        bot.fetch_json = fake_fetch
        try:
            pair = asyncio.run(bot.get_watchlist_pair(None, "base", "0xabc"))
        finally:
            bot.fetch_json = saved

        self.assertIsNotNone(pair)
        self.assertEqual(pair["pairAddress"], "0xABC")
        self.assertEqual(pair["source"], "watchlist:dexscreener")
        self.assertIn("/latest/dex/pairs/base/0xabc", calls[0],
                      "must use DexScreener, not the shared GeckoTerminal budget")

    def test_get_watchlist_pair_returns_none_when_pool_missing(self):
        async def fake_fetch(session, url, headers=None, **kw):
            return {"pairs": []}

        saved = bot.fetch_json
        bot.fetch_json = fake_fetch
        try:
            self.assertIsNone(asyncio.run(bot.get_watchlist_pair(None, "base", "0xabc")))
        finally:
            bot.fetch_json = saved

    def test_collect_marks_checked_even_when_the_fetch_fails(self):
        """A provider outage must not make the same pool retry every cycle."""
        self.note(token="0xflaky", hand=30.0)

        async def fake_fetch(session, url, headers=None, **kw):
            raise RuntimeError("provider down")

        saved = bot.fetch_json
        bot.fetch_json = fake_fetch
        try:
            pairs = asyncio.run(bot.collect_watchlist_pairs(None))
        finally:
            bot.fetch_json = saved

        self.assertEqual(pairs, [])
        self.assertEqual(bot.db_watchlist_due(), [],
                         "the failed entry must have been marked checked")
        checked = bot.db_conn.execute(
            "SELECT last_checked FROM watchlist WHERE token_address='0xflaky'"
        ).fetchone()[0]
        self.assertIsNotNone(checked)

    def test_collect_returns_pairs_for_due_entries(self):
        self.note(token="0xabc", hand=30.0)

        async def fake_fetch(session, url, headers=None, **kw):
            return {"pairs": [{"pairAddress": "0xpairabc", "chainId": "base",
                               "baseToken": {"symbol": "T"}}]}

        saved = bot.fetch_json
        bot.fetch_json = fake_fetch
        try:
            pairs = asyncio.run(bot.collect_watchlist_pairs(None))
        finally:
            bot.fetch_json = saved

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["source"], "watchlist:dexscreener")


class TestWatchlistEndToEnd(WatchlistTestBase):
    def test_a_re_ignited_pool_alerts_through_the_watchlist_lane(self):
        """The boar shape: seen while quiet, then re-priced after it runs."""
        self.note(token="0xboar", hand=30.0, symbol="boar")

        async def fake_fetch(session, url, headers=None, **kw):
            # The re-ignition: heavy 5m volume, buys dominant, price up.
            return {"pairs": [{
                "pairAddress": "0xpairoar", "chainId": "base", "dexId": "uniswap",
                "baseToken": {"address": "0xboar", "symbol": "boar", "name": "boar"},
                "quoteToken": {"symbol": "WETH", "address": "0x" + "ef" * 20},
                "priceUsd": "0.001", "priceNative": "0.000001",
                "liquidity": {"usd": 60_000.0}, "marketCap": 500_000.0,
                "volume": {"m5": 40_000.0, "h1": 120_000.0, "h6": 300_000.0,
                           "h24": 600_000.0},
                "txns": {"m5": {"buys": 60, "sells": 10},
                         "h1": {"buys": 200, "sells": 60}},
                "priceChange": {"m5": 40.0, "h1": 150.0, "h6": 200.0, "h24": 300.0},
                "pairCreatedAt": (time.time() - 6 * 3600) * 1000,
            }]}

        async def fake_security(session, chain, token):
            sec = bot._security_placeholder("test")
            sec.update({"is_open_source": True, "lp_locked": True})
            return sec

        async def fake_holders(session, chain, token):
            return bot.HolderData(top10=45.0, top50=70.0, top100=None, source="test")

        async def fake_cex(session, chain, token):
            return (0, False, 0)

        saved = (bot.fetch_json, bot.get_token_security,
                 bot.get_holder_concentration, bot.get_cex_listings,
                 bot.ALERT_THRESHOLD, bot.USE_SIGNALS)
        bot.fetch_json = fake_fetch
        bot.get_token_security = fake_security
        bot.get_holder_concentration = fake_holders
        bot.get_cex_listings = fake_cex
        bot.ALERT_THRESHOLD = 0  # the lane's job is coverage; the gate is separate
        bot.USE_SIGNALS = False
        # Isolate from the operator's .env: a global/per-chain mcap ceiling
        # (e.g. MAX_MARKET_CAP_USD=200000) would reject this $500k fixture
        # before the lane is even exercised.
        saved_gate_env = {
            k: os.environ.get(k) for k in (
                "MAX_MARKET_CAP_USD", "BASE_MAX_MARKET_CAP_USD",
                "ROBINHOOD_MAX_MARKET_CAP_USD",
            )
        }
        for k in saved_gate_env:
            os.environ.pop(k, None)
        try:
            pairs = asyncio.run(bot.collect_watchlist_pairs(None))
            self.assertEqual(len(pairs), 1)
            result = asyncio.run(bot.evaluate_token(None, pairs[0]))
        finally:
            (bot.fetch_json, bot.get_token_security,
             bot.get_holder_concentration, bot.get_cex_listings,
             bot.ALERT_THRESHOLD, bot.USE_SIGNALS) = saved
            for key, value in saved_gate_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertIsNotNone(result, "the re-ignited pool should clear a zero bar")
        self.assertEqual(result["chain"], "base")
        self.assertEqual(result["token_address"], "0xboar")


if __name__ == "__main__":
    unittest.main()
