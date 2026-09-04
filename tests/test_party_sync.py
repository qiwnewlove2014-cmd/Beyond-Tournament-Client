import json
import os
import time
import unittest
from types import SimpleNamespace

from libs.party_sync import (
    PartySyncState,
    clear_direct_mode,
    parse_invite_request,
    parse_player_list,
    parse_session_event,
    parse_state,
    same_player,
    clear_all_party_direct,
    clear_voice_direct_mode,
    party_member_channels,
    session_roster,
    set_direct_mode,
    set_voice_direct_mode,
    stereo_upload_eligible,
    upload_should_send,
)


class TestParsing(unittest.TestCase):
    def test_same_player_case_insensitive(self):
        self.assertTrue(same_player("Alice", "alice"))
        self.assertTrue(same_player("  Bob ", "bob"))
        self.assertFalse(same_player("Alice", "Bob"))
        self.assertFalse(same_player("", "Bob"))

    def test_invite_request_valid(self):
        invite = parse_invite_request({
            "session_id": "Alice:1234",
            "host_name": "Alice",
            "host_voice_channel": 33,
            "expires_ms": 30000,
        })
        self.assertEqual(invite["host_name"], "Alice")
        self.assertEqual(invite["session_id"], "Alice:1234")
        self.assertEqual(invite["host_voice_channel"], 33)
        self.assertEqual(invite["expires_ms"], 30000)

    def test_invite_request_invalid(self):
        self.assertIsNone(parse_invite_request(None))
        self.assertIsNone(parse_invite_request({}))
        self.assertIsNone(parse_invite_request({"session_id": "x"}))  # no host
        self.assertIsNone(parse_invite_request({"session_id": "", "host_name": "A"}))
        self.assertIsNone(parse_invite_request("junk"))

    def test_invite_request_cleans_fields(self):
        invite = parse_invite_request({
            "session_id": "x" * 500,
            "host_name": "  A" + "b" * 200,
            "host_voice_channel": True,  # bool rejected
            "expires_ms": True,          # bool rejected -> default
        })
        self.assertEqual(len(invite["session_id"]), 96)
        self.assertEqual(len(invite["host_name"]), 32)
        self.assertIsNone(invite["host_voice_channel"])
        self.assertEqual(invite["expires_ms"], 30000)

    def test_invite_request_expiry_clamped(self):
        invite = parse_invite_request({
            "session_id": "s",
            "host_name": "A",
            "expires_ms": 10 ** 9,
        })
        self.assertEqual(invite["expires_ms"], 120000)
        invite = parse_invite_request({
            "session_id": "s",
            "host_name": "A",
            "expires_ms": 1,
        })
        self.assertEqual(invite["expires_ms"], 1000)

    def test_session_event(self):
        for kind in ("party_sync_joined", "party_sync_kicked", "party_sync_ended"):
            ev = parse_session_event({
                "session_id": "Alice:1",
                "host_name": "Alice",
            })
            self.assertEqual(ev, {"session_id": "Alice:1", "host_name": "Alice"})
            self.assertIsNone(parse_session_event({"session_id": "Alice:1"}))
            self.assertIsNone(parse_session_event(None))

    def test_state_valid(self):
        state = parse_state({
            "session_id": "Alice:1",
            "status": "active",
            "host": {"name": "Alice", "voice_channel": 20},
            "guests": [{"name": "Bob", "voice_channel": 21}],
            "max_guests": 8,
        })
        self.assertEqual(state["host_name"], "Alice")
        self.assertEqual(state["host_voice_channel"], 20)
        self.assertEqual(state["guests"], [{"name": "Bob", "voice_channel": 21}])
        self.assertEqual(state["max_guests"], 8)

    def test_state_rejects_garbage(self):
        self.assertIsNone(parse_state(None))
        self.assertIsNone(parse_state({}))
        self.assertIsNone(parse_state({"session_id": "s"}))  # no host dict
        self.assertIsNone(parse_state({"session_id": "s", "host": {}}))  # host unnamed
        # status that is not active/absent is rejected
        self.assertIsNone(parse_state({
            "session_id": "s",
            "status": "ended",
            "host": {"name": "Alice"},
        }))
        # non-dict guests are ignored, invalid entries filtered
        state = parse_state({
            "session_id": "s",
            "host": {"name": "Alice", "voice_channel": True},
            "guests": ["Bob", {"name": "Carl", "voice_channel": 300}, {"name": "Dan"}],
            "max_guests": "many",
        })
        self.assertIsNone(state["host_voice_channel"])
        self.assertEqual(state["guests"], [
            {"name": "Carl", "voice_channel": None},
            {"name": "Dan", "voice_channel": None},
        ])
        self.assertEqual(state["max_guests"], 8)

    def test_player_list(self):
        self.assertEqual(parse_player_list(None), [])
        self.assertEqual(
            parse_player_list({"players": [{"name": "Bob"}, {"name": "Bob"}, "x"]}),
            ["Bob"],
        )


class TestStateTransitions(unittest.TestCase):
    STATE = {
        "session_id": "Alice:1",
        "status": "active",
        "host": {"name": "Alice", "voice_channel": 20},
        "guests": [{"name": "Bob", "voice_channel": 21}],
        "max_guests": 8,
    }

    def test_apply_state_host_and_guest(self):
        ps = PartySyncState()
        self.assertEqual(ps.apply_state(self.STATE, "alice"), "host")
        self.assertEqual(ps.role, "host")
        self.assertEqual(ps.host_name, "Alice")
        self.assertEqual([g["name"] for g in ps.guests], ["Bob"])

        ps2 = PartySyncState()
        self.assertEqual(ps2.apply_state(self.STATE, "BOB"), "guest")
        self.assertEqual(ps2.role, "guest")

        ps3 = PartySyncState()
        self.assertIsNone(ps3.apply_state({"junk": 1}, "alice"))
        self.assertIsNone(ps3.role)

    def test_pending_lifecycle(self):
        ps = PartySyncState()
        invite = parse_invite_request({
            "session_id": "Alice:1",
            "host_name": "Alice",
            "expires_ms": 30000,
        })
        ps.set_pending(invite)
        self.assertTrue(ps.pending_valid())
        ps.pending["expires_at"] = time.monotonic() - 1
        self.assertFalse(ps.pending_valid())  # expiry clears it
        self.assertIsNone(ps.pending)

        ps.set_pending(invite)
        ps.clear_pending()
        self.assertIsNone(ps.pending)
        self.assertFalse(ps.pending_valid())

    def test_end_session_clears_everything(self):
        ps = PartySyncState()
        ps.apply_state(self.STATE, "alice")
        ps.set_pending(parse_invite_request({
            "session_id": "Alice:1",
            "host_name": "Alice",
            "expires_ms": 30000,
        }))
        ps.end_session()
        self.assertIsNone(ps.role)
        self.assertEqual(ps.host_name, "")
        self.assertEqual(ps.session_id, "")
        self.assertEqual(ps.guests, [])
        self.assertIsNone(ps.pending)

    def test_is_host_helper(self):
        ps = PartySyncState()
        ps.apply_state(self.STATE, "alice")
        self.assertTrue(ps.is_host("ALICE"))
        ps2 = PartySyncState()
        ps2.role = "guest"
        ps2.host_name = "Alice"
        self.assertFalse(ps2.is_host("bob"))


class TestUploadRule(unittest.TestCase):
    def bot(self, broadcast=False, mega=False, party=False):
        return SimpleNamespace(
            broadcast_enabled=broadcast,
            broadcast_to_megaphone=mega,
            party_sync_force_upload=party,
        )

    def test_private_listening_sends_nothing(self):
        self.assertFalse(upload_should_send(self.bot()))
        self.assertFalse(upload_should_send(None))

    def test_public_broadcast_or_megaphone_sends(self):
        self.assertTrue(upload_should_send(self.bot(broadcast=True)))
        self.assertTrue(upload_should_send(self.bot(mega=True)))
        self.assertTrue(upload_should_send(self.bot(broadcast=True, mega=True)))

    def test_party_sync_force_uploads_even_when_private(self):
        # Host in an active Party Sync session uploads even with every public
        # toggle off — the server relays only to session guests.
        self.assertTrue(upload_should_send(self.bot(party=True)))
        self.assertTrue(upload_should_send(self.bot(party=True, broadcast=True)))


class TestLeaveKeyConfig(unittest.TestCase):
    def test_default_keyconfig_binds_party_sync_leave(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "default_keyconfig.json",
        )
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertEqual(config["bindings"].get("party_sync_leave"), "f8")


class TestDirectMode(unittest.TestCase):
    def fake_entity(self):
        src = SimpleNamespace(
            spatialize=True, relative=False, position=(1.0, 2.0, 3.0),
            direct_channels=False,
        )
        return SimpleNamespace(music_source=src)

    def test_set_and_clear_direct_mode(self):
        e = self.fake_entity()
        self.assertTrue(set_direct_mode(e, 0.8))
        self.assertTrue(e._party_sync_direct)
        self.assertAlmostEqual(e._party_sync_direct_gain, 0.8)
        self.assertFalse(e.music_source.spatialize)
        self.assertTrue(e.music_source.relative)
        self.assertEqual(e.music_source.position, (0.0, 0.0, 0.0))
        self.assertTrue(e.music_source.direct_channels)  # matches host's bot

        clear_direct_mode(e)
        self.assertFalse(e._party_sync_direct)
        self.assertTrue(e.music_source.spatialize)          # restored
        self.assertFalse(e.music_source.relative)           # restored
        self.assertFalse(e.music_source.direct_channels)    # restored
        self.assertIsNone(getattr(e, "_party_sync_direct_restore", None))

    def test_set_direct_mode_is_idempotent(self):
        e = self.fake_entity()
        set_direct_mode(e, 0.5)
        self.assertTrue(set_direct_mode(e, 0.9))  # already direct: unchanged
        self.assertAlmostEqual(e._party_sync_direct_gain, 0.5)

    def test_set_direct_mode_without_source(self):
        e = SimpleNamespace(music_source=None)
        self.assertFalse(set_direct_mode(e))
        clear_direct_mode(e)  # must not raise


class TestStereoUpload(unittest.TestCase):
    def bot(self, party=False, mega=False, broadcast=False):
        return SimpleNamespace(
            broadcast_enabled=broadcast,
            broadcast_to_megaphone=mega,
            party_sync_force_upload=party,
        )

    def test_stereo_only_on_party_private_leg(self):
        self.assertTrue(stereo_upload_eligible(self.bot(party=True)))
        # Public-only or megaphone uploads stay mono.
        self.assertFalse(stereo_upload_eligible(self.bot(broadcast=True)))
        self.assertFalse(stereo_upload_eligible(self.bot(mega=True)))
        self.assertFalse(stereo_upload_eligible(self.bot(party=True, mega=True)))
        self.assertFalse(stereo_upload_eligible(None))

    def test_stereo_needs_stereo_decode_and_no_live_input(self):
        self.assertFalse(stereo_upload_eligible(self.bot(party=True), channels=1))
        self.assertFalse(
            stereo_upload_eligible(self.bot(party=True), live_input_pending=True)
        )
        self.assertTrue(
            stereo_upload_eligible(self.bot(party=True), channels=2)
        )


class TestSessionRoster(unittest.TestCase):
    def test_roster_host_first_then_guests(self):
        ps = PartySyncState()
        ps.role = "host"
        ps.host_name = "Mason"
        ps.guests = [{"name": "Alice", "voice_channel": 1},
                     {"name": "Bob", "voice_channel": 2}]
        self.assertEqual(session_roster(ps), [
            ("Mason", "host"), ("Alice", "guest"), ("Bob", "guest"),
        ])

    def test_roster_skips_invalid_and_duplicate_host(self):
        ps = PartySyncState()
        ps.role = "guest"
        ps.host_name = "Mason"
        ps.guests = [{"name": "Mason", "voice_channel": 1},
                     {"name": "", "voice_channel": 2},
                     "not-a-dict",
                     {"name": "Alice"}]
        self.assertEqual(session_roster(ps), [
            ("Mason", "host"), ("Alice", "guest"),
        ])

    def test_roster_empty_when_no_session(self):
        ps = PartySyncState()
        self.assertEqual(session_roster(ps), [])
        ps.host_name = "Mason"
        ps.guests = []
        self.assertEqual(session_roster(ps), [("Mason", "host")])


class TestPartyVoiceDirect(unittest.TestCase):
    def state(self):
        ps = PartySyncState()
        ps.role = "host"
        ps.host_name = "Mason"
        ps.host_voice_channel = 21
        ps.guests = [
            {"name": "Alice", "voice_channel": 22},
            {"name": "Bob", "voice_channel": 23},
        ]
        return ps

    def fake_entity(self):
        src = SimpleNamespace(
            spatialize=True, relative=False, position=(1.0, 2.0, 3.0),
            direct_channels=False,
        )
        return SimpleNamespace(music_source=None, vc_source=src)

    def test_member_channels_host_plus_guests(self):
        self.assertEqual(party_member_channels(self.state()), {21, 22, 23})
        self.assertEqual(party_member_channels(None), set())
        empty = PartySyncState()
        empty.role = "guest"
        empty.host_name = "Mason"
        self.assertEqual(party_member_channels(empty), set())
        g = self.state()
        g.guests.append("garbage")
        self.assertEqual(party_member_channels(g), {21, 22, 23})

    def test_set_and_clear_voice_direct_mode(self):
        e = self.fake_entity()
        self.assertTrue(set_voice_direct_mode(e))
        self.assertTrue(e._party_sync_voice_direct)
        self.assertFalse(e.vc_source.spatialize)
        self.assertTrue(e.vc_source.relative)
        self.assertEqual(e.vc_source.position, (0.0, 0.0, 0.0))
        self.assertTrue(e.vc_source.direct_channels)

        clear_voice_direct_mode(e)
        self.assertFalse(e._party_sync_voice_direct)
        self.assertTrue(e.vc_source.spatialize)
        self.assertFalse(e.vc_source.relative)
        self.assertFalse(e.vc_source.direct_channels)
        self.assertIsNone(getattr(e, "_party_sync_voice_direct_restore", None))

    def test_voice_direct_idempotent_and_missing_source(self):
        e = self.fake_entity()
        set_voice_direct_mode(e)
        self.assertTrue(set_voice_direct_mode(e))  # already direct
        no_src = SimpleNamespace(music_source=None, vc_source=None)
        self.assertFalse(set_voice_direct_mode(no_src))
        clear_voice_direct_mode(no_src)  # must not raise

    def test_clear_all_party_direct_restores_both_modes(self):
        e = self.fake_entity()
        set_voice_direct_mode(e)
        e.music_source = SimpleNamespace(
            spatialize=True, relative=False, position=(1.0, 0.0, 0.0),
            direct_channels=False,
        )
        from libs.party_sync import set_direct_mode
        set_direct_mode(e, 0.7)
        gameplay = SimpleNamespace(
            voice_channels={22: e},
        )
        clear_all_party_direct(gameplay)
        self.assertFalse(getattr(e, "_party_sync_voice_direct", False))
        self.assertFalse(getattr(e, "_party_sync_direct", False))
        self.assertTrue(e.vc_source.spatialize)
        self.assertTrue(e.music_source.spatialize)


class TestPartyChatKeyConfig(unittest.TestCase):
    def test_default_keyconfig_binds_party_sync_chat(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "default_keyconfig.json",
        )
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        # Same physical key as map_chat (plain = map chat, Shift = party chat)
        self.assertEqual(config["bindings"].get("party_sync_chat"), "/")


if __name__ == "__main__":
    unittest.main()
