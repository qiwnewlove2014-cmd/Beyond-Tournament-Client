"""The run footstep follows the run sample folder -- packed build included.

``Entity.move`` plays ``steps/<tile>/<mode>`` and downgrades a sprint to that
surface's walk sample when ``data/steps/<tile>/run/`` is not there, so that
folder is the whole condition.  It has to answer the same in both ways the
game is run: from source (a real ``data/`` folder) and compiled (the
``sounds.dat`` pack, whose folders are pack-index entries materialized
lazily).  A compiled client used to read every pack folder as absent and
played the walk sample on every sprinting surface while a source run sounded
right.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT))

from libs import consts  # noqa: E402
from libs import vfs  # noqa: E402
from libs.objects.entity import Entity  # noqa: E402

spec_pack = importlib.util.spec_from_file_location(
    "fixture_pack_data", CLIENT / "tools/pack_data.py")
pack_data = importlib.util.module_from_spec(spec_pack)
spec_pack.loader.exec_module(pack_data)


class _Map:
    def __init__(self, tile):
        self.tile = tile
        self.player = None

    def get_tile_at(self, x, y, z):
        return self.tile

    def get_reverb_at(self, x, y, z):
        return None


class _Game:
    pong_mode = False


class _SprintProbe:
    """Enough of an Entity for ``Entity.move`` to reach the step sound."""

    def __init__(self, tile):
        self.game = _Game()
        self.map = _Map(tile)
        self.x = self.y = self.z = 0
        self.player = False  # Skips the voice/music source block.
        self.is_user = True
        self.name = "player1"
        self.falling = False
        self.on_move = None
        self.soundgroup = SimpleNamespace(position=None)
        self.played = []
        # Nothing worn: this probe is about the surface's own run/walk choice,
        # and a step in armor is the surface step plus a cloth layer over it.
        self.armor_sounds_path = None
        self.armor_cloth_volume = 60
        self._play_armor_cloth = Entity._play_armor_cloth.__get__(self)

    def sync_reverb(self):
        return True

    def play_sound(self, path, **kwargs):
        self.played.append(path)


def _sprint_step(tile):
    probe = _SprintProbe(tile)
    Entity.move(probe, probe.x, probe.y, probe.z, play_sound=True, mode="run")
    return probe.played[-1]


def _write_surfaces(root, surfaces):
    """``root``/steps/<tile>/{walk,run}/...; the folder is the condition."""
    for tile, has_run in surfaces:
        walk = root / "steps" / tile / "walk"
        walk.mkdir(parents=True)
        (walk / "walk-01.ogg").write_bytes(b"OGG-WALK")
        if has_run:
            run = root / "steps" / tile / "run"
            run.mkdir()
            (run / "run-01.ogg").write_bytes(b"OGG-RUN")


class RunFootstepSourceTests(unittest.TestCase):
    """Running from source: the real data/ folder decides."""

    def test_a_surface_with_run_samples_keeps_the_run_step(self):
        root = Path(tempfile.mkdtemp(prefix="bt-steps-src-"))
        saved = consts.SOUNDPREPEND
        try:
            _write_surfaces(root, [("wood", True)])
            consts.SOUNDPREPEND = str(root) + "/"
            self.assertEqual(_sprint_step("wood"), "steps/wood/run")
        finally:
            consts.SOUNDPREPEND = saved
            shutil.rmtree(root, ignore_errors=True)

    def test_a_surface_without_run_samples_falls_back_to_walking(self):
        root = Path(tempfile.mkdtemp(prefix="bt-steps-src-"))
        saved = consts.SOUNDPREPEND
        try:
            _write_surfaces(root, [("grass", False)])
            consts.SOUNDPREPEND = str(root) + "/"
            self.assertEqual(_sprint_step("grass"), "steps/grass/walk")
        finally:
            consts.SOUNDPREPEND = saved
            shutil.rmtree(root, ignore_errors=True)


class RunFootstepPackedBuildTests(unittest.TestCase):
    """A compiled run: the sounds.dat pack decides, as the game ships."""

    def test_a_pack_folder_counts_as_a_folder_for_the_footstep(self):
        root = Path(tempfile.mkdtemp(prefix="bt-steps-pack-"))
        old_cwd = os.getcwd()
        saved_prepend = consts.SOUNDPREPEND
        saved_sprepend = consts.SOUNDSPREPEND
        try:
            assets = root / "assets"
            _write_surfaces(assets, [("wood", True), ("grass", False)])
            pack_data.pack_data(assets, root / "sounds.dat",
                                "official.example", 13000)
            os.chdir(root)
            vfs._reset_for_tests()
            vfs.init_vfs()
            try:
                self.assertEqual(_sprint_step("wood"), "steps/wood/run")
                self.assertEqual(_sprint_step("grass"), "steps/grass/walk")
            finally:
                vfs._reset_for_tests()
        finally:
            os.chdir(old_cwd)
            consts.SOUNDPREPEND = saved_prepend
            consts.SOUNDSPREPEND = saved_sprepend
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
