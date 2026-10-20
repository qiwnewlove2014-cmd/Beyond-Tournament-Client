"""A login run is bounded: five doors, and then the main menu.

The report: with the Server closed, a client that had been in a game kept
saying "Connecting to the server. Please wait..." and "Trying the connection
again (2 of 2)" for minutes and never came back to the menu. The walk is two
doors, but ``reconnect_state`` rebuilt that walk on every 3 s cycle, so ten
cycles were twenty doors -- each one a whole silence window
(``consts.TIMEOUT``, 8 s) -- and every cycle started its count again, which is
what a player reads as a client that reconnects forever.

Pinned here:

1. One run -- a fresh login *and* every reconnect cycle that follows it -- dials
   at most ``login_attempts.ATTEMPT_LIMIT`` doors, then hands the main menu
   back with the run's own words (the silence report, which names the
   connection log, not "could not be reached").
2. A reconnect cycle continues the run instead of starting a new one: the doors
   it has already knocked on are forgotten (a retry is a fresh socket on the
   same door -- a new local port repairs a stale NAT mapping) and the budget is
   not.
3. The numbers a player hears count that one run: "(2 of 2)", "(3 of 4)",
   "(4 of 4)", "(5 of 5)" -- never "of 10", and never an attempt above its own
   total.
4. A fresh login is a new run with a new budget, and so is a dropped session
   (``disconnected`` clears the walk): a player who logged in once does not
   reconnect on the leftovers of that login.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import game as game_module
from libs import login_attempts


class SilentNetwork:
    """A transport that never answers: one login frame is one silence window."""

    resolved_host = None

    def login_timed_out(self):
        return True

    def loop(self):
        return "silent-loop"


class AlwaysThreeSeconds:
    """A clock that is always due, so a reconnect cycle is one frame."""

    elapsed = 3000

    def restart(self):
        self.elapsed = 3000


def walk(*entries, host="server.example"):
    return tuple(
        (host, entry) if isinstance(entry, int) else tuple(entry)
        for entry in entries
    )


class RunGame:
    """The Game slice one login run uses, with the real methods bound on."""

    RECONNECT_ATTEMPTS = game_module.Game.RECONNECT_ATTEMPTS

    login = game_module.Game.login
    _open_first_login_attempt = game_module.Game._open_first_login_attempt
    _open_candidate = game_module.Game._open_candidate
    _login_walk = game_module.Game._login_walk
    _login_run_spent = game_module.Game._login_run_spent
    _retry_login = game_module.Game._retry_login
    _silence_report = game_module.Game._silence_report
    _login_failure_words = game_module.Game._login_failure_words
    _report_login_failure = game_module.Game._report_login_failure
    _connection_failure_message = game_module.Game._connection_failure_message
    reconnect_state = game_module.Game.reconnect_state
    connection_error = game_module.Game.connection_error

    def __init__(self, candidates=None, reconnecting=True):
        self.reconnecting = reconnecting
        self.logging_out = False
        self._reconnect_attempts = 0
        self.network = None
        self.stack = []
        self.states = []
        self.dialled = []
        self.menu_calls = 0
        self._login_candidates = (
            walk(13000, 13002) if candidates is None else tuple(candidates)
        )
        self._login_name = "server.example"
        self._login_try = 0

    # The transport a login opens on, and its silence: both are the fake's,
    # because what is under test is what the walk does with them.
    def _new_network_client(self, candidate):
        self.dialled.append(candidate)
        return SilentNetwork()

    def login2(self):
        """One frame of a login whose transport is silent (the real retry)."""
        if self.network.login_timed_out():
            game_module.Game._retry_login(self)

    def _close_network(self, polite=True):
        self.network = None

    def new_clock(self):
        return AlwaysThreeSeconds()

    def replace(self, state):
        self.stack = [state]
        self.states.append(state)

    def pop(self):
        self.stack = []
        return None


def run_frames(game, menus, frames=60):
    """Pump the state stack until the menu is handed back (or frames run out)."""
    for _ in range(frames):
        if not game.stack or menus:
            return
        game.stack[-1]()


class AttemptBudgetTests(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.menus = []
        patches = [
            mock.patch.object(
                game_module, "speak", lambda text, *rest: self.said.append(text)
            ),
            # The log line is written for real in other suites; here only the
            # words matter, and a log file is not this test's business.
            mock.patch.object(game_module, "log", lambda *args, **kwargs: None),
            mock.patch.object(
                game_module.menus, "main_menu", lambda game: self.menus.append(game)
            ),
            mock.patch.object(
                game_module.server_config, "get_server_endpoint",
                return_value=("server.example", 13000),
            ),
            mock.patch.object(
                game_module.server_config, "get_fallback_addresses", return_value=()
            ),
            mock.patch.object(game_module.options, "get_login_port", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def make(self, **kwargs):
        return RunGame(**kwargs)

    def notices(self):
        """The (attempt, total) pairs the player was read, in order."""
        pairs = []
        for line in self.said:
            if not line.startswith("Trying the connection again ("):
                continue
            inside = line[len("Trying the connection again ("):].rstrip(").")
            attempt, total = inside.split(" of ")
            pairs.append((int(attempt), int(total)))
        return pairs


class TheRunIsBoundedTests(AttemptBudgetTests):
    def test_a_reconnect_dials_the_limit_and_then_the_menu_returns(self):
        """Ten cycles of two doors was the bug; five doors is the rule."""
        game = self.make()
        game.replace(game.reconnect_state)
        run_frames(game, self.menus)
        self.assertEqual(
            len(game.dialled), login_attempts.ATTEMPT_LIMIT,
            "the run spends its budget once -- a fresh login plus its "
            "reconnect cycles together, not per cycle",
        )
        self.assertEqual(len(self.menus), 1, "and then the player is handed the menu")
        self.assertFalse(game.reconnecting, "the reconnect is over")
        self.assertIn("You returned to the main menu.", self.said[-1])

    def test_the_last_words_are_the_run_s_own_report(self):
        """"Could not be reached" does not tell a player who to tell; the
        silence report names the connection log and the ports."""
        game = self.make()
        game.replace(game.reconnect_state)
        run_frames(game, self.menus)
        last = self.said[-1]
        self.assertIn("No answer from the server after 5 tries", last)
        self.assertIn("connection log", last)

    def test_the_numbers_count_one_run_and_never_pass_the_limit(self):
        game = self.make()
        game.replace(game.reconnect_state)
        run_frames(game, self.menus)
        pairs = self.notices()
        self.assertEqual(
            pairs, [(2, 2), (3, 4), (4, 4), (5, 5)],
            "the walk's second door, then the reconnect's doors counted on: a "
            "player reading out \"5 of 5\" describes the run they waited on",
        )
        for attempt, total in pairs:
            self.assertLessEqual(total, login_attempts.ATTEMPT_LIMIT)
            self.assertLessEqual(attempt, total)

    def test_a_walk_refuses_to_dial_once_the_run_is_spent(self):
        game = self.make()
        game._login_facts = game._login_walk()
        game._login_facts["attempted"] = login_attempts.ATTEMPT_LIMIT
        self.assertFalse(game_module.Game._open_candidate(game, 0))
        self.assertEqual(game.dialled, [], "no sixth socket is opened")

    def test_a_fresh_login_is_a_new_run_with_a_whole_budget(self):
        game = self.make()
        game._login_facts = game._login_walk()
        game._login_facts["attempted"] = login_attempts.ATTEMPT_LIMIT
        game_module.Game._open_first_login_attempt(game)
        self.assertEqual(len(game.dialled), 1, "the login button dials again")
        self.assertEqual(game._login_walk()["attempted"], 1)

    def test_a_dropped_session_begins_a_new_run(self):
        """Otherwise a reconnect would spend what the login that worked left."""
        game = game_module.Game.__new__(game_module.Game)
        game.network = None
        game.reconnecting = False
        game.logging_out = False
        game._reconnect_attempts = 7
        game._login_facts = {"attempted": login_attempts.ATTEMPT_LIMIT}
        game.gameplay = SimpleNamespace()
        game.stack = [game.gameplay]
        game.replace = lambda state: None
        game.reconnect_state = lambda: None
        with mock.patch.object(game_module, "speak"):
            game_module.Game.disconnected(game)
        self.assertTrue(game.reconnecting)
        self.assertIsNone(game._login_facts, "the new run has spent nothing")
        self.assertEqual(game._reconnect_attempts, 0)

    def test_asking_whether_a_run_is_spent_never_starts_one(self):
        """A drop clears the walk; the next cycle must build its own list."""
        game = self.make()
        self.assertFalse(game_module.Game._login_run_spent(game))
        self.assertFalse(
            hasattr(game, "_login_facts"),
            "no walk is created just by asking whether the budget is spent",
        )
        game_module.Game._open_first_login_attempt(game, continuing=True)
        self.assertEqual(len(game.dialled), 1)

    def test_a_reconnect_keeps_the_run_s_budget_and_forgets_only_the_doors(self):
        game = self.make()
        game._login_facts = game._login_walk()
        game._login_facts["attempted"] = 3
        game._login_facts["doors"].add(("server.example", 13000))
        game._login_facts["dialled"].append(("server.example", 13000))
        game_module.Game._open_first_login_attempt(game, continuing=True)
        facts = game._login_walk()
        self.assertEqual(facts["attempted"], 4, "the budget continues")
        self.assertEqual(
            game.dialled, [("server.example", 13000)],
            "and the same door is dialled again: a retry is a new socket",
        )


class ThePolicyTests(unittest.TestCase):
    def test_the_limit_is_four_or_five_attempts(self):
        """The number the player asked for, and the one the words promise."""
        self.assertEqual(login_attempts.ATTEMPT_LIMIT, 5)

    def test_a_run_with_a_limit_never_promises_more_than_it_dials(self):
        candidates = walk(13000, 13002, ("backup.example", 13000), ("backup.example", 13002))
        attempt, total = login_attempts.retry_position(
            candidates, 0, (), (), run_attempts=4, limit=login_attempts.ATTEMPT_LIMIT
        )
        self.assertEqual((attempt, total), (4, 5))
        attempt, total = login_attempts.retry_position(
            candidates, 3, (), (), run_attempts=9, limit=login_attempts.ATTEMPT_LIMIT
        )
        self.assertLessEqual(attempt, total)
        self.assertLessEqual(total, login_attempts.ATTEMPT_LIMIT)


if __name__ == "__main__":
    unittest.main()
