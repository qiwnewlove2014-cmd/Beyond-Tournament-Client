"""Sound system: which rendering the card grants, and how a listener asks for one.

Everything here runs without a sound card.  The driver is faked at the same
seam the game uses (``device.reset`` / ``hrtf.use`` / ``context.get_attrs``),
so the behaviour that matters is pinned:

* ``default`` is bit-for-bit the shipped behaviour: force the saved HRTF model.
* a card that refuses a request never leaves the listener downgraded -- the
  previous rendering is restored and the saved choice is left alone.
* what a line says is what the card *granted*, not what the option asked for.
* the HRTF model picker is not this module's business and is unchanged, except
  that choosing a model also means choosing HRTF.
"""
import ctypes
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import menus, options
from libs.audio import output_system as osys


class FakeDevice():
    """``cyal.Device`` as far as this feature can tell, with a scripted driver."""

    def __init__(self, extensions=(b"ALC_SOFT_output_mode",), grants=None,
                 hrtf_usable=True):
        self._extensions = set(extensions)
        self.grants = dict(grants or {})
        self.hrtf_usable = hrtf_usable
        self.calls = []
        self.mode = osys.MODE_STEREO_BASIC
        self.hrtf = False
        self.status = 0

    def is_extension_present(self, name):
        return name in self._extensions

    def reset(self, **kwargs):
        self.calls.append(kwargs)
        if "output_mode_soft" in kwargs:
            requested = kwargs["output_mode_soft"]
            self.mode = self.grants.get(requested, requested)
            self.hrtf = False
            self.status = 1 if self.mode == osys.MODE_STEREO_HRTF else 0
            if not self.hrtf_usable:
                self.mode = osys.MODE_STEREO_BASIC
                self.status = 2
            return
        if kwargs.get("hrtf_soft") == osys.DONT_CARE:
            self.mode, self.hrtf, self.status = osys.MODE_STEREO_BASIC, False, 0
            return
        if kwargs.get("hrtf_soft") == 0:
            self.mode, self.hrtf, self.status = osys.MODE_STEREO_BASIC, False, 0


class FakeHrtf():
    """``cyal.hrtf.HrtfExtension``: the model list plus what ``use`` decides."""

    def __init__(self, device, models=("default-44100", "oalsoft_hrtf_48000")):
        self.device = device
        self._models = tuple(models)
        self.current_model = None
        self.used = []

    def models(self):
        # cyal hands back an iterator; the menu only ever iterates it.
        return list(self._models)

    def use(self, model):
        self.used.append(model)
        if model is None:
            self.device.mode, self.device.hrtf = osys.MODE_STEREO_BASIC, False
            self.device.status, self.current_model = 0, None
        elif self.device.hrtf_usable:
            self.device.mode, self.device.hrtf = osys.MODE_STEREO_HRTF, True
            self.device.status, self.current_model = 1, model
        else:
            self.device.mode, self.device.hrtf = osys.MODE_STEREO_BASIC, False
            self.device.status, self.current_model = 2, None


class FakeContext():
    def __init__(self, device):
        self.device = device

    def get_attrs(self):
        return SimpleNamespace(
            output_mode_soft=self.device.mode,
            hrtf_soft=1 if self.device.hrtf else 0,
            hrtf_status_soft=self.device.status,
        )


def make_system(**kwargs):
    device = FakeDevice(**kwargs)
    hrtf = FakeHrtf(device)
    return osys.OutputSystem(FakeContext(device), hrtf), device, hrtf


class TestTheShippedDriverAgreesWithOurNumbers(unittest.TestCase):
    """The mode constants are the only magic numbers here, so prove them."""

    EXPECTED = (
        ("ALC_MONO_SOFT", 0x1500),
        ("ALC_STEREO_SOFT", 0x1501),
        ("ALC_QUAD_SOFT", 0x1503),
        ("ALC_SURROUND_5_1_SOFT", 0x1504),
        ("ALC_SURROUND_6_1_SOFT", 0x1505),
        ("ALC_SURROUND_7_1_SOFT", 0x1506),
        ("ALC_STEREO_BASIC_SOFT", 0x19AE),
        ("ALC_STEREO_UHJ_SOFT", 0x19AF),
        ("ALC_STEREO_HRTF_SOFT", 0x19B2),
        ("ALC_OUTPUT_MODE_SOFT", 0x19AC),
        ("ALC_DONT_CARE_SOFT", 0x2),
    )

    def test_our_values_are_the_drivers_own(self):
        # Never opening a device: alcGetEnumValue(NULL, ...) answers from the
        # build itself, so this runs headless like every other test here.
        if sys.platform != "win32":
            self.skipTest("the shipped OpenAL Soft build is Windows-only")
        import cyal

        dll = os.path.join(os.path.dirname(cyal.__file__), "OpenAL32.dll")
        if not os.path.isfile(dll):
            self.skipTest("cyal's OpenAL32.dll is not installed")
        lib = ctypes.WinDLL(dll)
        lib.alcGetEnumValue.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.alcGetEnumValue.restype = ctypes.c_int
        for name, value in self.EXPECTED:
            with self.subTest(name=name):
                self.assertEqual(lib.alcGetEnumValue(None, name.encode()), value)

    def test_the_table_covers_every_key_it_promises(self):
        self.assertEqual(osys.SYSTEMS["default"][1], None)
        self.assertEqual(osys.SYSTEMS["auto"][1], osys.DONT_CARE)
        for key, (_, request) in osys.SYSTEMS.items():
            if key in ("default", "auto"):
                continue
            with self.subTest(key=key):
                self.assertIn(key, osys.GRANTED_AS)
                self.assertEqual(osys.SYSTEMS[key][1], osys.GRANTED_AS[key][0])


class TestStartupKeepsTheShippedBehaviour(unittest.TestCase):
    def test_default_forces_the_saved_hrtf_model_and_nothing_else(self):
        # The behaviour the game shipped with: the model, and no device call
        # that a player who never opens the new picker could ever notice.
        system, device, hrtf = make_system()
        self.assertTrue(system.supported())
        attempt = system.start(osys.DEFAULT_SYSTEM, model="oalsoft_hrtf_48000")
        self.assertTrue(attempt.granted)
        self.assertEqual(hrtf.used, ["oalsoft_hrtf_48000"])
        self.assertEqual(device.calls, [])
        self.assertEqual(device.mode, osys.MODE_STEREO_HRTF)

    def test_a_saved_system_a_build_cannot_honour_falls_back_to_the_model(self):
        system, device, hrtf = make_system(extensions=())
        attempt = system.start("surround71", model="built-in")
        self.assertFalse(attempt.granted)
        self.assertEqual(hrtf.used, ["built-in"], "the turn must keep the old sound")
        self.assertEqual(device.calls, [], "nothing may be asked of a build that cannot answer")
        self.assertIn("cannot be asked", attempt.detail)

    def test_the_default_option_is_the_default(self):
        self.assertEqual(options.SOUND_SYSTEM_DEFAULT, osys.DEFAULT_SYSTEM)

    def test_a_driver_without_the_mode_extension_lists_only_the_model(self):
        system, _, _ = make_system(extensions=())
        self.assertFalse(system.supported())
        self.assertEqual([key for key, _ in osys.menu_entries(system)], ["default"])


class TestTheCardIsAskedAndItsAnswerIsRead(unittest.TestCase):
    def test_automatic_lets_the_card_decide_and_reports_its_own_word(self):
        system, device, hrtf = make_system()
        attempt = system.apply("auto")
        self.assertTrue(attempt.granted)
        self.assertEqual(device.calls, [{"hrtf_soft": osys.DONT_CARE}])
        self.assertEqual(hrtf.used, [])
        self.assertEqual(attempt.label, "Stereo")
        self.assertEqual(device.status, 0)
        self.assertIn("HRTF is off", system.describe())

    def test_headphones_detected_is_said_the_way_the_card_says_it(self):
        system, device, hrtf = make_system()
        device.mode, device.hrtf, device.status = osys.MODE_STEREO_HRTF, True, 4
        hrtf.current_model = "oalsoft_hrtf_48000"
        self.assertIn("headphones detected", system.describe())
        self.assertIn("oalsoft_hrtf_48000", system.describe())

    def test_a_speaker_system_is_asked_for_with_hrtf_given_up_in_the_same_call(self):
        system, device, hrtf = make_system()
        attempt = system.apply("uhj")
        self.assertTrue(attempt.granted)
        self.assertEqual(device.calls,
                         [{"hrtf_soft": 0, "output_mode_soft": osys.MODE_STEREO_UHJ}])
        self.assertEqual(device.mode, osys.MODE_STEREO_UHJ)
        self.assertEqual(attempt.label, "Stereo UHJ (super stereo)")
        self.assertEqual(hrtf.used, [], "the model list is not the way to change systems")

    def test_asking_for_plain_stereo_is_granted_as_stereo_basic(self):
        # Measured: ALC_STEREO_SOFT is answered as ALC_STEREO_BASIC_SOFT, so a
        # strict equality check would report a refusal for a mode we were given.
        system, device, _ = make_system(grants={osys.MODE_STEREO: osys.MODE_STEREO_BASIC})
        attempt = system.apply("stereo")
        self.assertTrue(attempt.granted)
        self.assertEqual(attempt.label, "Stereo")

    def test_a_refused_surround_mode_restores_what_the_listener_had(self):
        # The measured trap: a stereo endpoint answers *success* to a 7.1
        # request and grants stereo with HRTF switched off.  Leaving that in
        # place would silently downgrade a listener who asked a question.
        system, device, hrtf = make_system(
            grants={osys.MODE_SURROUND_7_1: osys.MODE_STEREO_BASIC})
        system.start(osys.DEFAULT_SYSTEM, model="oalsoft_hrtf_48000")
        self.assertEqual(device.mode, osys.MODE_STEREO_HRTF)
        before = list(hrtf.used)

        attempt = system.apply("surround71", model="oalsoft_hrtf_48000",
                               previous=osys.DEFAULT_SYSTEM)
        self.assertFalse(attempt.granted)
        self.assertEqual(hrtf.used, before + ["oalsoft_hrtf_48000"],
                         "the HRTF the listener had must come back")
        self.assertEqual(device.mode, osys.MODE_STEREO_HRTF)
        self.assertIn("not available", attempt.detail)
        self.assertIn("Stereo", attempt.detail)
        self.assertNotIn("7.1", attempt.label, "the line reports what was granted")

    def test_a_refused_change_from_one_speaker_system_to_another_puts_it_back(self):
        system, device, _ = make_system(
            grants={osys.MODE_SURROUND_7_1: osys.MODE_STEREO_BASIC})
        system.apply("uhj")
        self.assertEqual(device.mode, osys.MODE_STEREO_UHJ)
        attempt = system.apply("surround71", previous="uhj")
        self.assertFalse(attempt.granted)
        self.assertEqual(device.mode, osys.MODE_STEREO_UHJ,
                         "the previous system is what stays playing")

    def test_a_driver_that_refuses_hrtf_is_reported_not_hidden(self):
        system, device, _ = make_system(hrtf_usable=False)
        system.start(osys.DEFAULT_SYSTEM, model="oalsoft_hrtf_48000")
        self.assertEqual(device.status, 2)
        self.assertIn("it refused HRTF", system.describe())

    def test_a_raising_device_is_reported_and_restores(self):
        system, device, hrtf = make_system()
        system.start(osys.DEFAULT_SYSTEM, model="built-in")

        def explode(**kwargs):
            raise RuntimeError("device lost")

        device.reset = explode
        attempt = system.apply("uhj", model="built-in", previous=osys.DEFAULT_SYSTEM)
        self.assertFalse(attempt.granted)
        self.assertIn("could not ask", attempt.detail)
        self.assertEqual(hrtf.used[-1], "built-in")

    def test_an_unknown_key_changes_nothing(self):
        system, device, _ = make_system()
        attempt = system.apply("turbo")
        self.assertFalse(attempt.granted)
        self.assertEqual(device.calls, [])


class TestPickerAndOptionsAreHonest(unittest.TestCase):
    def test_the_picker_lists_every_system_the_build_can_ask_for(self):
        system, _, _ = make_system()
        keys = [key for key, _ in osys.menu_entries(system)]
        self.assertEqual(keys, list(osys.SYSTEMS))
        self.assertEqual(keys[0], "default")

    def test_a_refused_system_says_what_this_machine_gives_instead(self):
        system, _, _ = make_system()
        entries = dict(osys.menu_entries(system, {"surround71": "Stereo"}))
        self.assertIn("this machine gives Stereo", entries["surround71"])
        self.assertNotIn("this machine gives", entries["uhj"])

    def test_no_sound_manager_offers_only_the_model_choice(self):
        self.assertEqual([key for key, _ in osys.menu_entries(None)], ["default"])

    def test_the_option_clamps_to_the_known_set(self):
        with mock.patch.object(options, "get", return_value="banana"):
            self.assertEqual(options.get_sound_system(), "default")
        with mock.patch.object(options, "set"), \
                mock.patch.object(options, "get_sound_system_refusals", return_value={}):
            self.assertEqual(options.set_sound_system("banana"), "default")
            self.assertEqual(options.set_sound_system("uhj"), "uhj")

    def test_refusals_are_kept_and_filtered(self):
        with mock.patch.object(options, "get", return_value={"uhj": "Stereo",
                                                            "nonsense": "x",
                                                            "surround71": 5}), \
                mock.patch.object(options, "set") as save:
            self.assertEqual(options.get_sound_system_refusals(),
                             {"uhj": "Stereo"})
            options.set_sound_system_refusal("quad", "Stereo")
        self.assertEqual(save.call_args.args[1], {"uhj": "Stereo", "quad": "Stereo"})

    def test_a_refusal_is_forgotten_when_the_system_is_granted(self):
        with mock.patch.object(options, "get", return_value={"uhj": "Stereo"}), \
                mock.patch.object(options, "set") as save:
            options.set_sound_system_refusal("uhj", None)
        self.assertEqual(save.call_args.args[1], {})


class _FakeMenu():
    def __init__(self, *args, **kwargs):
        self.items = []
        self.music_paths = []
        # Read by ``options_menu`` while it builds the list (the turning items
        # live on the real menu object).
        self.turning_sensitivity_item_text = "Turning sensitivity"
        self.turning_mode_item_text = "Turning mode"
        self.voice_chat_mode_item_text = "Voice chat mode"

    def add_items(self, items):
        self.items.extend(items)

    def set_music(self, path):
        self.music_paths.append(path)


class _Game():
    def __init__(self, system=None, hrtf=None):
        self.audio_mngr = SimpleNamespace(
            hrtf=hrtf if hrtf is not None else SimpleNamespace(
                current_model="default", models=lambda: ["a", "b"],
                used=[], use=lambda model: None),
            output_system=system,
        )
        self.replaced = []

    def replace(self, menu):
        self.replaced.append(menu)

    def toggle_item(self, name, *args):
        return name


class TestTheMenuAsksAndSpeaksTheAnswer(unittest.TestCase):
    def _run(self, action, system, refused=None):
        game = _Game(system)
        created = []

        def make_menu(*args, **kwargs):
            menu = _FakeMenu(*args, **kwargs)
            created.append(menu)
            return menu

        with mock.patch("libs.menus.menu.Menu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch.object(menus.options, "get_sound_system_refusals",
                                  return_value=refused or {}), \
                mock.patch.object(menus.options, "get", return_value="built-in"), \
                mock.patch.object(menus.options, "set") as save, \
                mock.patch.object(menus.options, "set_sound_system") as keep, \
                mock.patch.object(menus.options, "set_sound_system_refusal") as note, \
                mock.patch.object(menus.speech, "speak") as speak:
            menus.sound_system_menu(game, lambda: None)
            action(created[0], game)
        return created[0], save, keep, note, speak

    def _pick(self, key):
        index = list(osys.SYSTEMS).index(key)

        def pick(menu, game, index=index):
            menu.items[index][1]()

        return pick

    def test_the_picker_lists_the_systems_and_a_way_back(self):
        system, _, _ = make_system()
        menu, _, _, _, _ = self._run(lambda menu, game: None, system)
        self.assertEqual([item[0] for item in menu.items],
                         [label for _, label in osys.menu_entries(system)] + ["go back"])

    def test_a_system_this_machine_refused_is_labelled_with_what_it_gave(self):
        system, _, _ = make_system()
        menu, _, _, _, _ = self._run(lambda menu, game: None, system,
                                     refused={"surround71": "Stereo"})
        label = menu.items[list(osys.SYSTEMS).index("surround71")][0]
        self.assertIn("7.1 Surround", label)
        self.assertIn("this machine gives Stereo", label)

    def test_a_granted_choice_is_saved_and_spoken(self):
        system, _, _ = make_system()
        _, _, keep, note, speak = self._run(self._pick("uhj"), system)
        keep.assert_called_once_with("uhj")
        note.assert_called_once_with("uhj", None)
        self.assertIn("using Stereo UHJ", speak.call_args.args[0])

    def test_a_refused_choice_is_not_saved_and_the_answer_is_spoken(self):
        system, _, _ = make_system(
            grants={osys.MODE_SURROUND_7_1: osys.MODE_STEREO_BASIC})
        _, _, keep, note, speak = self._run(self._pick("surround71"), system)
        keep.assert_not_called()
        note.assert_called_once_with("surround71", "Stereo")
        self.assertIn("not available", speak.call_args.args[0])

    def test_the_options_line_says_what_the_card_is_doing(self):
        system, device, _ = make_system()
        device.mode, device.hrtf, device.status = osys.MODE_STEREO_HRTF, True, 4
        device.current_model = "oalsoft_hrtf_48000"
        line = menus.sound_system_line(_Game(system))
        self.assertIn("Headphone HRTF", line)
        self.assertIn("headphones detected", line)

    def test_the_options_line_falls_back_to_the_saved_choice(self):
        with mock.patch.object(menus.options, "get_sound_system_label",
                               return_value="7.1 Surround"):
            line = menus.sound_system_line(_Game(None))
        self.assertIn("7.1 Surround", line)

    def test_a_host_without_a_sound_manager_still_saves_the_choice(self):
        game = _Game(None)
        with mock.patch.object(menus.options, "set_sound_system") as keep, \
                mock.patch.object(menus.options, "get_sound_system_label",
                                  return_value="7.1 Surround"), \
                mock.patch.object(menus.speech, "speak") as speak:
            menus.set_sound_system("surround71", game, lambda: None)
        keep.assert_called_once_with("surround71")
        self.assertIn("next time the game starts", speak.call_args.args[0])


class TestTheHrtfModelMenuIsUntouched(unittest.TestCase):
    def test_the_model_picker_still_lists_every_model_and_disabling(self):
        game = _Game(hrtf=SimpleNamespace(models=lambda: ["a", "b"]))
        created = []

        def make_menu(*args, **kwargs):
            menu = _FakeMenu(*args, **kwargs)
            created.append(menu)
            return menu

        with mock.patch("libs.menus.menu.Menu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"):
            menus.hrtf_model_menu(game, lambda: None)
        labels = [item[0] for item in created[0].items]
        self.assertEqual(labels, ["a", "b", "Disable HRTF", "go back"])

    def test_choosing_a_model_also_means_choosing_hrtf(self):
        system, _, hrtf = make_system()
        game = _Game(system, hrtf)
        with mock.patch.object(menus.options, "set") as save, \
                mock.patch.object(menus.options, "set_sound_system") as keep, \
                mock.patch.object(menus.speech, "speak"):
            menus.set_hrtf_model("oalsoft_hrtf_48000", game, lambda: None)
        save.assert_called_once_with("hrtf_model", "oalsoft_hrtf_48000")
        keep.assert_called_once_with(osys.DEFAULT_SYSTEM)
        self.assertEqual(game.audio_mngr.hrtf.used, ["oalsoft_hrtf_48000"])

    def test_options_offer_both_the_model_and_the_system_lines(self):
        class _OptionsMenu(_FakeMenu):
            pass

        created = []

        def make_menu(*args, **kwargs):
            menu = _OptionsMenu(*args, **kwargs)
            created.append(menu)
            return menu

        with mock.patch("libs.menus.OptionsMenu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.menus.server_config.is_production_build",
                           return_value=True):
            menus.options_menu(_Game(make_system()[0]), lambda: None,
                               replace_call=lambda _menu: None)
        items = created[0].items
        model_index = next(i for i, item in enumerate(items)
                           if isinstance(item[0], str)
                           and item[0].startswith("Set which HRTF Model"))
        following = items[model_index + 1]
        label = following[0]() if callable(following[0]) else following[0]
        self.assertTrue(label.startswith(
            "Set which sound system you would like to use. Currently:"))
        self.assertIn("Stereo", label, "the line reads the card, not the option")

    def test_switching_audio_device_asks_for_the_saved_system_again(self):
        system, device, hrtf = make_system()
        game = _Game(system, hrtf)
        game.audio_mngr.context = SimpleNamespace(
            device=SimpleNamespace(reopen=mock.Mock()))
        with mock.patch.object(menus.options, "get_sound_system", return_value="default"), \
                mock.patch.object(menus.options, "get", return_value="built-in"), \
                mock.patch.object(menus.options, "set"):
            menus.set_device(game, "OpenAL Soft on Speakers", lambda: None)
        self.assertEqual(hrtf.used, ["built-in"])


if __name__ == "__main__":
    unittest.main()
