"""Saved-login refresh when the server reports a password change.

Covers the client half of /changepassword: the server sends the new password
back on channel_misc and the stored account entry plus the current login
credential must follow, or the next automatic login fails on the stale value.
"""
import unittest
from unittest import mock

from libs.event_handeler import EventHandeler


class PasswordChangedTests(unittest.TestCase):
    def run_handler(self, data, username="kan", accounts=None, existing_password="old"):
        store = {
            "username": username,
            "password": existing_password,
            "accounts": [] if accounts is None else [dict(account) for account in accounts],
        }
        handler = EventHandeler.__new__(EventHandeler)
        with mock.patch("libs.options.get", side_effect=lambda key, default=None: store.get(key, default)), \
                mock.patch("libs.options.set", side_effect=lambda key, value: store.__setitem__(key, value)):
            handler.password_changed(data)
        return store

    def test_updates_saved_account_and_current_credentials(self):
        store = self.run_handler(
            {"password": "NewPass123"},
            accounts=[{"username": "kan", "password": "old"}, {"username": "bob", "password": "keep"}],
        )
        self.assertEqual(store["password"], "NewPass123")
        self.assertEqual(store["accounts"][0], {"username": "kan", "password": "NewPass123"})
        self.assertEqual(store["accounts"][1], {"username": "bob", "password": "keep"})

    def test_appends_the_account_entry_when_it_is_missing(self):
        store = self.run_handler({"password": "Fresh1"}, accounts=[])
        self.assertEqual(
            store["accounts"], [{"username": "kan", "password": "Fresh1"}]
        )
        self.assertEqual(store["password"], "Fresh1")

    def test_ignores_malformed_payloads(self):
        for bad in (None, {}, {"password": ""}, {"password": 123}, "string"):
            with self.subTest(payload=bad):
                store = self.run_handler(
                    bad, accounts=[{"username": "kan", "password": "old"}]
                )
                self.assertEqual(store["password"], "old")
                self.assertEqual(store["accounts"][0]["password"], "old")

    def test_ignores_the_event_without_a_stored_username(self):
        store = self.run_handler(
            {"password": "NewPass123"}, username="", accounts=[{"username": "kan", "password": "old"}]
        )
        self.assertEqual(store["password"], "old")
        self.assertEqual(store["accounts"][0]["password"], "old")


if __name__ == "__main__":
    unittest.main()
