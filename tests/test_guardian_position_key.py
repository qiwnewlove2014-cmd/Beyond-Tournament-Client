import json
import pathlib
import unittest


CLIENT_ROOT = pathlib.Path(__file__).resolve().parents[1]


class GuardianPositionKeyTests(unittest.TestCase):
    """The fort round's locator is a key, not a command.

    A fort draws nothing on the map and a living creature is never sent to the
    Client as an object, so the Client cannot answer this itself: one press asks
    the Server, which speaks back to whoever asked.
    """

    def test_default_binding_is_a_and_therefore_rebindable(self):
        config = json.loads((CLIENT_ROOT / "default_keyconfig.json").read_text(encoding="utf-8"))
        self.assertEqual("a", config["bindings"]["check_guardian"])

    def test_the_binding_asks_the_server_without_a_payload(self):
        source = (CLIENT_ROOT / "libs" / "gameplay.py").read_text(encoding="utf-8")
        self.assertIn('kc.get("check_guardian", pygame.K_a): self.check_guardian', source)
        self.assertIn('self.game.network.send(consts.CHANNEL_MISC, "check_guardian", {})', source)

    def test_the_request_is_not_gated_by_rank(self):
        """Every player in a match may ask as often as they like: it is a read."""
        source = (CLIENT_ROOT / "libs" / "gameplay.py").read_text(encoding="utf-8")
        method = source.split("def check_guardian")[1].split("\n    def ")[0]
        self.assertNotIn("is_staff", method)
        self.assertNotIn("cooldown", method.lower())


if __name__ == "__main__":
    unittest.main()
