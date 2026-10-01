"""Tests for alert de-duplication — "one message per token".

The complaint these encode: *the same token alerted me more than once*. There
are four independent ways that happened, and each gets its own test:

1. One scan lists two pools of the same contract (GeckoTerminal's list and
   DexScreener's ``/tokens/v1`` disagree about which pool), so one cycle
   produced two results for one token.
2. The old re-alert policy fired within its own cooldown whenever the score had
   climbed 12 points — and scores move by more than that between scans.
3. The dedupe key was compared byte-for-byte while feeds disagree about
   checksum casing: two spellings, two rows, two alerts.
4. ``db_record_alert`` swallows upsert failures as a warning, so a table whose
   UNIQUE clause was missing meant *no* dedupe state was ever written and the
   token re-alerted every cycle.

Run with: python -m unittest -v test_alert_dedupe
"""

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("TELEGRAM_TOKEN", "1:TEST")
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("WALLET_PRIVATE_KEY", "")

import bot  # noqa: E402

TOKEN = "0x" + "ab" * 20
PAIR = "0x" + "cd" * 20


async def _no_sleep(_delay=0):
    """dispatch_alerts throttles 1s between sends; tests must not pay it."""
    return None


def result(token=TOKEN, chain="base", score=61.0, pair=PAIR, symbol="X"):
    """The shape dispatch_alerts consumes. send_alert is stubbed, so the
    rendering fields do not matter here."""
    return {
        "chain": chain, "token_address": token, "symbol": symbol, "name": symbol,
        "pair_address": pair, "total_score": score,
    }


class DedupeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sent = []
        self._saved = {
            "DB_PATH": bot.DB_PATH,
            "db_conn": bot.db_conn,
            "send_alert": bot.send_alert,
            "RE_ALERTS_ENABLED": bot.RE_ALERTS_ENABLED,
            "RE_ALERT_COOLDOWN_HOURS": bot.RE_ALERT_COOLDOWN_HOURS,
            "SCORE_IMPROVEMENT_THRESHOLD": bot.SCORE_IMPROVEMENT_THRESHOLD,
        }
        bot.DB_PATH = os.path.join(self.tmpdir, "alerts.db")
        bot.db_conn = bot.init_db()
        bot.RE_ALERTS_ENABLED = False
        bot.RE_ALERT_COOLDOWN_HOURS = 4.0
        bot.SCORE_IMPROVEMENT_THRESHOLD = 12.0
        bot.ALERTED_THIS_RUN.clear()

        async def fake_send(alert):
            self.sent.append((alert["chain"], alert["token_address"], alert["total_score"]))
            return True

        self.fake_send = fake_send
        bot.send_alert = fake_send
        self._sleep = mock.patch.object(bot.asyncio, "sleep", new=_no_sleep)
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        bot.send_alert = self._saved["send_alert"]
        try:
            bot.db_conn.close()
        except Exception:
            pass
        for key, value in self._saved.items():
            setattr(bot, key, value)
        bot.ALERTED_THIS_RUN.clear()

    def dispatch(self, *results):
        return asyncio.run(bot.dispatch_alerts(list(results)))

    def rows(self):
        return bot.db_conn.execute(
            "SELECT chain, token_address, total_score FROM alerts ORDER BY id"
        ).fetchall()


class TestSameCycleDuplicates(DedupeTestBase):
    def test_two_pools_of_one_token_send_one_message(self):
        """The union can hand the same contract over twice in one scan."""
        first = result(score=61.0, pair="0x" + "11" * 20)
        second = result(score=61.0, pair="0x" + "22" * 20)
        self.assertEqual(self.dispatch(first, second), 1)
        self.assertEqual(len(self.sent), 1)

    def test_batch_guard_holds_even_when_the_score_would_qualify_for_a_re_alert(self):
        """With RE_ALERTS on, a 14-point gap used to buy a second message one
        second after the first — the "two pools" case reaching dispatch as two
        results whose scores differ."""
        bot.RE_ALERTS_ENABLED = True
        first = result(score=61.0, pair="0x" + "11" * 20)
        second = result(score=75.0, pair="0x" + "22" * 20)
        self.assertEqual(self.dispatch(first, second), 1)
        self.assertEqual(len(self.sent), 1)

    def test_repeated_results_are_one_alert_across_batches(self):
        self.assertEqual(self.dispatch(result()), 1)
        self.assertEqual(self.dispatch(result()), 0)
        self.assertEqual(len(self.sent), 1)


class TestReAlertPolicy(DedupeTestBase):
    def test_default_is_never_re_alert(self):
        self.assertEqual(self.dispatch(result(score=61.0)), 1)
        # Later scans, much higher score, four hours later — still silent.
        bot.db_conn.execute("UPDATE alerts SET alert_time = ?", (time.time() - 9 * 3600,))
        bot.db_conn.commit()
        self.assertEqual(self.dispatch(result(score=90.0)), 0)
        self.assertEqual(len(self.sent), 1)

    def test_re_alerts_can_be_enabled_again(self):
        bot.RE_ALERTS_ENABLED = True
        self.assertEqual(self.dispatch(result(score=61.0)), 1)
        bot.db_conn.execute("UPDATE alerts SET alert_time = ?", (time.time() - 5 * 3600,))
        bot.db_conn.commit()
        self.assertEqual(self.dispatch(result(score=65.0)), 1)
        self.assertEqual(len(self.sent), 2)

    def test_enabled_policy_still_respects_its_cooldown(self):
        bot.RE_ALERTS_ENABLED = True
        self.assertEqual(self.dispatch(result(score=61.0)), 1)
        # One hour later and only 9 points better: inside the cooldown, below
        # the improvement bar.
        bot.db_conn.execute("UPDATE alerts SET alert_time = ?", (time.time() - 3600,))
        bot.db_conn.commit()
        self.assertEqual(self.dispatch(result(score=70.0)), 0)
        # 14 points better beats the cooldown early.
        self.assertEqual(self.dispatch(result(score=75.0)), 1)
        self.assertEqual(len(self.sent), 2)


class TestKeyIdentity(DedupeTestBase):
    def test_checksummed_and_lowercase_are_one_token(self):
        self.assertEqual(self.dispatch(result(token=TOKEN)), 1)
        self.assertEqual(self.dispatch(result(token=TOKEN.upper())), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_the_same_address_on_another_chain_is_a_different_alert(self):
        self.assertEqual(self.dispatch(result(chain="base")), 1)
        self.assertEqual(self.dispatch(result(chain="bsc")), 1)
        self.assertEqual(len(self.rows()), 2)


class TestDedupeStateSurvivesAFailedWrite(DedupeTestBase):
    def test_a_failed_upsert_does_not_repeat_within_the_run(self):
        """db_record_alert logs and swallows; without the in-memory fallback a
        single failed write re-alerted on every subsequent scan."""
        with mock.patch.object(bot, "db_record_alert", return_value=False) as upsert:
            self.assertEqual(self.dispatch(result()), 1)
            self.assertEqual(self.dispatch(result()), 0)
            # The second run never reaches the upsert: it is suppressed by the
            # in-memory copy precisely because no record could be written.
            self.assertEqual(upsert.call_count, 1)
        self.assertEqual(len(self.sent), 1)

    def test_the_fallback_does_not_block_a_legitimate_re_alert(self):
        """When the table HAS a record it is authoritative — the in-memory set
        must only cover the write-failure case, or RE_ALERTS could never fire
        again inside one process."""
        bot.RE_ALERTS_ENABLED = True
        self.assertEqual(self.dispatch(result()), 1)
        bot.db_conn.execute("UPDATE alerts SET alert_time = ?", (time.time() - 5 * 3600,))
        bot.db_conn.commit()
        self.assertEqual(self.dispatch(result(score=90.0)), 1)
        self.assertEqual(len(self.sent), 2)


class TestAlertsTableMigration(DedupeTestBase):
    def _legacy_table(self):
        """A table created before UNIQUE(chain, token_address) existed: every
        upsert against it fails, so nothing is ever deduped."""
        bot.db_conn.close()
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute("DROP TABLE alerts")
        conn.execute(
            "CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chain TEXT NOT NULL, token_address TEXT NOT NULL, symbol TEXT, "
            "total_score INTEGER, alert_time REAL)"
        )
        conn.execute(
            "INSERT INTO alerts (chain, token_address, symbol, total_score, alert_time) "
            "VALUES ('base', ?, 'X', 61, 1000), ('base', ?, 'X', 70, 1000)",
            (TOKEN.upper(), TOKEN),
        )
        conn.commit()
        conn.close()
        bot.db_conn = bot.init_db()

    def test_migration_collapses_case_variants_and_restores_the_upsert(self):
        self._legacy_table()
        self.assertEqual(len(self.rows()), 1, "case variants must collapse")

        # The upsert that used to be rejected now lands, and still one row.
        self.assertTrue(bot.db_record_alert("base", TOKEN.upper(), "X", 70))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][1], TOKEN)

        # And the read side sees the record, so the token is not re-alerted.
        self.assertIsNotNone(bot.db_get_last_alert("base", TOKEN.upper()))
        self.assertEqual(self.dispatch(result()), 0)

    def test_migration_survives_a_table_that_already_has_the_unique_constraint(self):
        """The realistic case, and the one the ordering gets wrong.

        Every schema in this repo ships `UNIQUE(chain, token_address)`, so a
        deployment that ever stored a checksummed address has two rows that are
        distinct under BINARY collation but identical once lower-cased. Running
        the lower-casing UPDATE first collides with its own twin, the statement
        aborts, and the collapse + index never happen — leaving exactly the
        duplicate-alert behaviour the migration exists to remove.
        """
        # init_db already created the table WITH the unique constraint.
        self.assertEqual(len(self.rows()), 0)
        bot.db_conn.execute(
            "INSERT INTO alerts (chain, token_address, symbol, total_score, alert_time) "
            "VALUES ('base', ?, 'X', 61, 1000), ('base', ?, 'X', 70, 2000)",
            (TOKEN.upper(), TOKEN),
        )
        bot.db_conn.commit()
        self.assertEqual(len(self.rows()), 2, "both spellings fit before migration")

        bot.db_conn = bot.init_db()

        self.assertEqual(len(self.rows()), 1, "case variants must collapse")
        self.assertEqual(self.rows()[0][1], TOKEN)
        self.assertTrue(bot.db_record_alert("base", TOKEN.upper(), "X", 90))
        self.assertEqual(len(self.rows()), 1, "upsert must not re-split the key")

    def test_address_case_is_migrated_for_the_other_keyed_tables(self):
        """The comment in evaluate_token promises watchlist/feature joins too."""
        bot.db_conn.execute(
            "INSERT INTO watchlist (chain, token_address, symbol, first_seen) "
            "VALUES ('base', ?, 'X', 1), ('base', ?, 'X', 2)",
            (TOKEN.upper(), TOKEN),
        )
        bot.db_conn.execute(
            "INSERT INTO features (chain, token_address, symbol, ts_epoch) "
            "VALUES ('base', ?, 'X', 1), ('base', ?, 'X', 2)",
            (TOKEN.upper(), TOKEN),
        )
        bot.db_conn.commit()

        bot.db_conn = bot.init_db()

        wl = bot.db_conn.execute(
            "SELECT token_address FROM watchlist ORDER BY first_seen"
        ).fetchall()
        self.assertEqual(len(wl), 1, "one watchlist row per contract")
        self.assertEqual(wl[0][0], TOKEN)
        feats = bot.db_conn.execute(
            "SELECT token_address FROM features ORDER BY ts_epoch"
        ).fetchall()
        # features must be lower-cased but NOT collapsed: those rows are the
        # price series label_outcomes.py turns into forward returns.
        self.assertEqual(len(feats), 2, "training history must not be deleted")
        self.assertEqual({r[0] for r in feats}, {TOKEN})


class TestFailedSendDoesNotConsumeTheAlert(DedupeTestBase):
    def test_a_failed_send_leaves_no_dedupe_state_and_retries_next_cycle(self):
        """A Telegram outage must not be recorded as 'the user was told'.

        With RE_ALERTS=false the record is permanent, so recording a failed send
        would silence that token for the life of the table.
        """
        attempts = []

        async def failing_send(alert):
            attempts.append(alert["token_address"])
            return False

        bot.send_alert = failing_send
        self.assertEqual(self.dispatch(result()), 0, "no message = no alert")
        self.assertEqual(len(self.rows()), 0, "dedupe state must stay empty")
        self.assertEqual(bot.ALERTED_THIS_RUN, set())
        # It must be attempted again on the next cycle, not silently skipped.
        self.assertEqual(self.dispatch(result()), 0)
        self.assertEqual(len(attempts), 2)

        # And once Telegram recovers, the token goes out exactly once.
        bot.send_alert = self.fake_send
        self.assertEqual(self.dispatch(result()), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_a_failed_send_is_still_not_retried_twice_within_one_cycle(self):
        """The batch blocks a same-cycle retry even though nothing was recorded."""
        attempts = []

        async def failing_send(alert):
            attempts.append(alert["token_address"])
            return False

        bot.send_alert = failing_send
        shared = set()
        self.assertEqual(asyncio.run(bot.dispatch_alerts([result()], shared)), 0)
        self.assertEqual(asyncio.run(bot.dispatch_alerts([result()], shared)), 0)
        self.assertEqual(len(attempts), 1, "one attempt per cycle, not per lane")


class TestSharedBatchAcrossLanes(DedupeTestBase):
    def test_one_cycle_one_message_even_when_the_lanes_disagree_on_score(self):
        """Discovery and the watchlist dispatch separately in the same cycle.

        With RE_ALERTS=true a ≥12-point gap between the two readings used to buy
        a second message seconds after the first, because each call built its
        own batch set.
        """
        bot.RE_ALERTS_ENABLED = True
        shared = set()
        discovery = result(score=61.0, pair="0x" + "11" * 20)
        watchlist = result(score=75.0, pair="0x" + "22" * 20)

        self.assertEqual(asyncio.run(bot.dispatch_alerts([discovery], shared)), 1)
        self.assertEqual(asyncio.run(bot.dispatch_alerts([watchlist], shared)), 0)
        self.assertEqual(len(self.sent), 1)

        # A later cycle (fresh set) may legitimately re-alert under the policy.
        self.assertEqual(
            asyncio.run(bot.dispatch_alerts([result(score=90.0)], set())), 1
        )
        self.assertEqual(len(self.sent), 2)


class TestDiscoveryUnionDedupe(DedupeTestBase):
    def test_get_all_pairs_keeps_one_pool_per_token(self):
        gt_pool = {
            "chainId": "base", "pairAddress": "0x" + "11" * 20,
            "baseToken": {"address": TOKEN, "symbol": "X", "name": "X"},
            "volume": {"m5": 100.0}, "liquidity": {"usd": 5000.0},
            "source": "geckoterminal:new_pools",
        }
        ds_pool = {
            "chainId": "base", "pairAddress": "0x" + "22" * 20,
            # Same contract, checksummed spelling, deeper liquidity.
            "baseToken": {"address": TOKEN.upper(), "symbol": "X", "name": "X"},
            "volume": {"m5": 900.0}, "liquidity": {"usd": 9000.0},
            "source": "dexscreener:boost",
        }
        other_token = "0x" + "ef" * 20
        other_pool = {
            "chainId": "base", "pairAddress": "0x" + "33" * 20,
            "baseToken": {"address": other_token, "symbol": "Y", "name": "Y"},
            "volume": {"m5": 10.0}, "liquidity": {"usd": 100.0},
        }

        async def gt(_session, _network):
            return [gt_pool]

        async def ds(_session, _network):
            return [ds_pool, dict(other_pool)]

        with mock.patch.object(bot, "USE_GECKOTERMINAL", True), \
             mock.patch.object(bot, "DEXSCREENER_SOURCES", ["boosts"]), \
             mock.patch.object(bot, "get_geckoterminal_pairs", gt), \
             mock.patch.object(bot, "get_dexscreener_pairs", ds):
            pairs = asyncio.run(bot.get_all_pairs(None, "base"))

        addresses = sorted(p["pairAddress"] for p in pairs)
        self.assertEqual(addresses, sorted(["0x" + "22" * 20, "0x" + "33" * 20]),
                         "the deeper pool of the duplicate token must win")


if __name__ == "__main__":
    unittest.main()
