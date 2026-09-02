import queue
import unittest
from pathlib import Path
from types import SimpleNamespace

from libs.menu import Menu


class _Source:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.buffer = object()

    def stop(self):
        self.events.append("stop")

    def delete(self):
        self.events.append("delete")


class _Efx:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.calls = []

    def send(self, source, send_index, slot):
        self.calls.append((source, send_index, slot))
        self.events.append(("send", slot))


def _make_menu(reverb_at):
    efx = _Efx()
    player = SimpleNamespace(x=1, y=2, z=3)
    gameplay = SimpleNamespace(
        player=player,
        map=SimpleNamespace(get_reverb_at=reverb_at),
    )
    result = Menu.__new__(Menu)
    result.game = SimpleNamespace(audio_mngr=SimpleNamespace(efx=efx))
    result.parrent = gameplay
    result.environmental_preview = True
    result._preview_source = _Source()
    result._preview_reverb_slot = None
    result._preview_reverb_position = None
    return result, gameplay, efx


class BuilderPreviewReverbTests(unittest.TestCase):
    def test_wallbuy_preview_tracks_room_and_becomes_dry_outside(self):
        room_slot = object()

        def reverb_at(x, _y, _z):
            return SimpleNamespace(reverb=room_slot) if x < 5 else None

        preview, gameplay, efx = _make_menu(reverb_at)
        preview._sync_preview_reverb(force=True)
        self.assertIs(efx.calls[-1][2], room_slot)

        gameplay.player.x = 6
        preview._sync_preview_reverb()
        self.assertIsNone(efx.calls[-1][2])

        call_count = len(efx.calls)
        preview._sync_preview_reverb()
        self.assertEqual(len(efx.calls), call_count)

    def test_non_wallbuy_preview_stays_dry(self):
        room_slot = object()
        preview, _gameplay, efx = _make_menu(
            lambda *_args: SimpleNamespace(reverb=room_slot)
        )
        preview.environmental_preview = False
        preview._sync_preview_reverb(force=True)
        self.assertIsNone(efx.calls[-1][2])

    def test_cleanup_detaches_reverb_before_source_delete(self):
        events = []
        source = _Source(events)
        efx = _Efx(events)
        preview = Menu.__new__(Menu)
        preview.game = SimpleNamespace(audio_mngr=SimpleNamespace(efx=efx))
        preview.direct_soundgroup = SimpleNamespace(labeled_sources={})
        preview._preview_decode_queue = queue.Queue()
        preview._preview_result_queue = queue.Queue()
        preview._preview_decode_thread = None
        preview._preview_source = source
        preview._preview_buffer = object()
        preview._preview_reverb_slot = object()
        preview._preview_reverb_position = (1, 2, 3)

        preview._destroy_preview()

        self.assertLess(events.index(("send", None)), events.index("delete"))

    def test_wallbuy_menu_event_enables_environmental_preview(self):
        source = (Path(__file__).resolve().parents[1] / "libs" / "event_handeler.py").read_text(encoding="utf-8")
        self.assertIn(
            'm.environmental_preview = data.get("event", "") == "builder_weapon_select"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
