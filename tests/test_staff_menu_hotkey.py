import json
import pathlib
import unittest


CLIENT_ROOT = pathlib.Path(__file__).resolve().parents[1]


class StaffMenuHotkeyTests(unittest.TestCase):
    def test_default_binding_uses_f8_not_f12(self):
        config = json.loads((CLIENT_ROOT / "default_keyconfig.json").read_text(encoding="utf-8"))
        self.assertEqual("f8", config["bindings"]["open_staff_menu"])
        self.assertNotEqual("f12", config["bindings"]["open_staff_menu"])

    def test_gameplay_gates_request_to_staff(self):
        source = (CLIENT_ROOT / "libs" / "gameplay.py").read_text(encoding="utf-8")
        self.assertIn('kc.get("open_staff_menu", pygame.K_F8): self.open_staff_menu', source)
        self.assertIn('if getattr(self, "is_staff", False):', source)
        self.assertIn('self.game.network.send(consts.CHANNEL_MENUS, "staff_menu_open", {})', source)


if __name__ == "__main__":
    unittest.main()
