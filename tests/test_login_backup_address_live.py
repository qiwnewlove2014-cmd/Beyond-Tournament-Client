"""The gate on ``tests/login_backup_address_live.py``.

The rig makes one player whose resolver cannot answer the server's *name* out of
this machine and watches a real ENet transport open on the address the build
embedded beside it. This file asserts what that player must get, whatever the
machine it runs on: the doors that were dialled, the words they heard, the line
the log kept, and what the server on the other end really received.

Nothing here is written from the code -- every assertion reads the rig's own
transcript (which doors, which sentences, which log line, which datagram), and
the two numbers a player is told are checked against the log's ``tried=`` list
rather than against a constant, because a notice that says "2 of 2" over a
single door is exactly the kind of sentence this rig exists to catch.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts, login_attempts

import login_backup_address_live as rig


CONNECTING = "Connecting to the server. Please wait..."
LOGGING_IN = "Logging in. Please wait..."


def fields(line):
    """One ``[LOGIN]`` line as its key=value pairs."""

    body = line.split("[LOGIN] ", 1)[1]
    return dict(part.split("=", 1) for part in body.split(" ") if "=" in part)


class LiveBackupAddressTests(unittest.TestCase):
    """Both players, once, on this machine's real sockets."""

    @classmethod
    def setUpClass(cls):
        cls.reached = rig.play_a_login_that_gets_in()
        cls.silent = rig.play_a_login_that_gets_nothing()
        cls.public = rig.play_a_login_that_only_the_public_resolver_can_reach()

    def test_a_public_resolver_that_answers_is_used_before_the_packed_address(self):
        """The layer the packed address is only a fallback for: the name is put to
        a public resolver over HTTPS, its answer is what is dialled, and the
        address in the pack is never needed -- which is what keeps a login
        working after the server's address has moved."""
        report = self.public
        self.assertEqual(report["public_resolver_asked"], [rig.UNANSWERABLE_NAME])
        self.assertEqual(report["public_answer"], rig.BACKUP_ADDRESS)
        self.assertEqual(report["opened_host"], rig.BACKUP_ADDRESS)
        self.assertEqual(
            [host for host, _ in report["dialled"]],
            [rig.UNANSWERABLE_NAME],
            "the door dialled is the name, answered by the public resolver",
        )
        self.assertEqual(
            report["backup"], "192.0.2.1", "a packed address nothing could be behind"
        )
        self.assertFalse(
            report["backup_tried"], "and the login never needed to use it"
        )

    def test_a_login_a_public_resolver_saved_says_so_on_the_line(self):
        report = self.public
        self.assertEqual(len(report["log_lines"]), 1, report["log_text"])
        line = fields(report["log_lines"][0])
        self.assertEqual(line["lookup"], "public")
        self.assertEqual(line["resolved"], rig.BACKUP_ADDRESS)
        self.assertEqual(line["outcome"], "answered")
        self.assertNotIn("backup", line, "the packed address was not the door")
        self.assertEqual(
            report["said"], [CONNECTING, LOGGING_IN],
            "a login that works says nothing about retrying",
        )

    def test_the_public_resolvers_answer_reached_the_real_server(self):
        report = self.public
        self.assertTrue(
            report["server_connects"],
            "a listener on the answer saw the handshake complete",
        )
        packets = [packet for _, packet in report["server_packets"]]
        self.assertEqual([packet.get("event") for packet in packets], ["login"])
        self.assertEqual(packets[0]["data"].get("username"), rig.PLAYER_NAME)

    def test_the_rig_asked_for_a_name_this_machine_cannot_answer(self):
        for report in (self.reached, self.silent):
            with self.subTest(report=report["resolver"]):
                self.assertEqual(report["unresolvable"], [rig.UNANSWERABLE_NAME])
                self.assertTrue(report["lookup_failed"])
                if report["resolver"].startswith("this machine"):
                    self.assertIsNone(rig.live_lookup(rig.UNANSWERABLE_NAME))

    def test_the_name_is_never_dialled_on_any_of_its_ports(self):
        for report in (self.reached, self.silent):
            with self.subTest(port=report["port"]):
                self.assertNotIn(
                    rig.UNANSWERABLE_NAME,
                    [host for host, _ in report["dialled"]],
                    "a name with no answer is not a door: nothing was ever sent",
                )

    def test_the_transport_opened_on_the_address_the_pack_embedded(self):
        report = self.reached
        self.assertEqual(report["opened_host"], rig.BACKUP_ADDRESS)
        self.assertEqual(report["opened_port"], report["port"])
        self.assertTrue(report["backup_tried"], "and the login knows it was a backup")

    def test_the_server_really_took_the_connection(self):
        report = self.reached
        self.assertEqual(report["server_port"], report["port"])
        self.assertTrue(
            report["server_connects"],
            "a listener on the backup address saw the handshake complete",
        )
        self.assertTrue(all(host == rig.BACKUP_ADDRESS for host, _ in report["server_connects"]))

    def test_the_server_received_the_players_login_request(self):
        packets = [packet for _, packet in self.reached["server_packets"]]
        self.assertEqual(len(packets), 1, self.reached["server_packets"])
        self.assertEqual(packets[0].get("event"), "login")
        data = packets[0].get("data") or {}
        self.assertEqual(data.get("username"), rig.PLAYER_NAME)
        self.assertEqual(data.get("password"), rig.PLAYER_PASSWORD)
        self.assertTrue(data.get("version"), "the request carries the client's build")
        self.assertTrue(data.get("capabilities"))

    def test_what_the_player_heard_when_it_worked(self):
        self.assertEqual(
            self.reached["said"],
            [CONNECTING, LOGGING_IN],
            "a login that only had to fall back says nothing about retrying",
        )
        self.assertFalse(self.reached["back_at_menu"])

    def test_the_one_line_the_server_cannot_keep(self):
        report = self.reached
        self.assertEqual(len(report["log_lines"]), 1, report["log_text"])
        line = fields(report["log_lines"][0])
        self.assertEqual(line["host"], rig.UNANSWERABLE_NAME)
        self.assertEqual(line["resolved"], rig.BACKUP_ADDRESS)
        self.assertEqual(line["outcome"], "answered")
        self.assertEqual(line["answered"], f"{rig.BACKUP_ADDRESS}:{report['port']}")
        self.assertEqual(line["tried"], f"{rig.BACKUP_ADDRESS}:{report['port']}")
        self.assertEqual(
            line["lookup"], "literal",
            "the address was dialled as it was given, not resolved at all",
        )
        self.assertEqual(
            line["backup"], "yes",
            "the line is where a player's DNS problem is known first",
        )

    def test_the_name_is_put_to_a_public_resolver_before_the_packed_address(self):
        """The second way of answering the one question this machine cannot, and
        the one that keeps working when the server's address changes: it is asked
        first, and only a fallback when it answers nothing is the address the pack
        carries used."""
        for report in (self.reached, self.silent):
            with self.subTest(port=report["port"]):
                self.assertEqual(
                    report["public_resolver_asked"],
                    [rig.UNANSWERABLE_NAME],
                    "the name this machine could not answer is the one asked",
                )
                self.assertEqual(
                    report["opened_host"], rig.BACKUP_ADDRESS,
                    "and the login fell back to the packed address afterwards",
                )

    def test_the_console_line_and_the_file_line_are_the_same_words(self):
        self.assertEqual(
            self.reached["console_lines"],
            self.reached["log_lines"],
            "the helper the player reads is the file the staff read",
        )

    def test_the_rig_writes_its_own_log_and_never_the_players(self):
        for report in (self.reached, self.silent):
            with self.subTest(log=report["log_path"]):
                self.assertTrue(report["log_path"].endswith("client_debug.log"))
                self.assertNotEqual(
                    os.path.abspath(report["log_path"]),
                    os.path.abspath("client_debug.log"),
                    "a test suite must never rewrite the running client's log",
                )

    def test_a_login_with_nothing_behind_the_backup_says_which_failure_it_was(self):
        report = self.silent
        self.assertEqual(
            report["said"][-1],
            login_attempts.resolution_message(
                backup_tried=True, public_resolver_tried=True
            ),
            "both ways of answering the name failed, and the sentence says so",
        )
        self.assertIn("name could not be looked up", report["said"][-1])
        self.assertIn("public resolver over the internet", report["said"][-1])
        self.assertIn("try a mobile hotspot", report["said"][-1])
        self.assertNotIn(
            "No answer from the server", report["said"][-1],
            "a lookup that never sent a packet is not a silent server",
        )
        self.assertTrue(report["back_at_menu"], "and the player is back at the menu")

    def test_the_failed_log_line_names_the_lookup_and_what_was_tried_instead(self):
        report = self.silent
        self.assertEqual(len(report["log_lines"]), 1, report["log_text"])
        line = fields(report["log_lines"][0])
        self.assertEqual(line["host"], rig.UNANSWERABLE_NAME)
        self.assertEqual(line["resolved"], "none")
        self.assertEqual(line["outcome"], "name-not-looked-up")
        self.assertEqual(
            line["lookup"], "none",
            "neither this machine's resolver nor a public one could answer",
        )
        self.assertEqual(line["backup"], "yes")
        self.assertNotIn("answered", line, "nothing answered")

    def test_the_two_doors_walked_are_the_endpoints_port_and_the_one_above_it(self):
        report = self.silent
        self.assertEqual(
            report["dialled"],
            [
                (rig.BACKUP_ADDRESS, report["port"]),
                (rig.BACKUP_ADDRESS, report["port"] + login_attempts.FALLBACK_OFFSET),
            ],
            "the port above the configured one is a door on the backup too",
        )

    def test_the_number_the_player_hears_is_the_number_of_doors_dialled(self):
        """The one number a player reads out to staff has to describe the same
        walk the log's ``tried=`` list does."""
        report = self.silent
        notice = report["said"][1]
        told = re.fullmatch(r"Trying the connection again \((\d+) of (\d+)\)\.", notice)
        self.assertIsNotNone(told, notice)
        attempt, total = (int(value) for value in told.groups())
        tried = fields(report["log_lines"][0])["tried"].split(",")
        self.assertEqual(total, len(tried), "two doors said, two doors dialled")
        self.assertEqual(attempt, total, "and the last one is the last one")
        self.assertEqual(
            report["said"][0], CONNECTING,
            "the sentence about connecting comes before any sentence about retrying",
        )
        self.assertEqual(
            tried[0], f"{rig.BACKUP_ADDRESS}:{report['port']}",
            "the first door is the configured port, and it is dialled in silence",
        )
        self.assertEqual(
            tried[-1],
            f"{rig.BACKUP_ADDRESS}:{report['port'] + login_attempts.FALLBACK_OFFSET}",
            "the notice is spoken for the door the login really moved to",
        )

    def test_a_login_that_had_nothing_to_fall_back_on_is_not_told_about_retrying(self):
        """The failure the rig found: with the configured port filtered and the
        name unanswerable there is only one door left, and the player must be
        told the truth about it rather than "2 of 2"."""
        report = self.silent
        self.assertEqual(
            len(report["dialled"]), 2,
            "the walk the notice counted is the walk that happened",
        )
        self.assertEqual(
            [sentence for sentence in report["said"] if sentence.startswith("Trying")],
            ["Trying the connection again (2 of 2)."],
            "spoken once, for the door the login really moved to",
        )

    def test_the_rig_never_duplicated_the_name_of_the_endpoint_into_a_port(self):
        """Sanity on the rig itself: the two reports differ, so the assertions
        above are not both reading the same run."""
        self.assertNotEqual(self.reached["port"], self.silent["port"])
        self.assertNotEqual(self.reached["dialled"], self.silent["dialled"])
        self.assertNotEqual(
            self.reached["dialled"], self.public["dialled"],
            "one login fell back to the address, the other did not have to",
        )
        self.assertEqual(consts.DEFAULT_PORT, 13000)


if __name__ == "__main__":
    unittest.main()
