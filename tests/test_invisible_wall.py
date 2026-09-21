"""An invisible wall: a wall you cannot see, and its silence is the feature.

A wall in this game is nothing but a *name*.  The map stores the tile
(``wallinvisible``), every rule keys on the word "wall" inside it -- it blocks
movement and bullets, walls the AI off and muffles what is behind it -- and the
two sounds the game looks up by that name are ``walls/<name>.ogg`` for walking
into it and ``foley/bullet_impacts/<name>/`` for shooting it.  So an invisible
wall is a wall whose samples are silence, and that silence must be *shipped*:
a missing file logs "unable to load file" on every bump and every shot and
reads as a broken sound rather than as air.

These tests pin the two halves of that promise: it is a wall everywhere a wall
is asked for (the bump asks for its own sample, and the step is blocked), and it
is silent exactly where a wall is heard -- from source and from the compiled
``sounds.dat`` pack, whose folders are what the impact lookup asks for.
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
from libs import path_utils  # noqa: E402
from libs import safe_vorbis  # noqa: E402
from libs import vfs  # noqa: E402
from libs.objects.entity import Entity  # noqa: E402

spec_pack = importlib.util.spec_from_file_location(
    "fixture_pack_data", CLIENT / "tools/pack_data.py")
pack_data = importlib.util.module_from_spec(spec_pack)
spec_pack.loader.exec_module(pack_data)

INVISIBLE = "wallinvisible"
BUMP = CLIENT / "data" / "walls" / f"{INVISIBLE}.ogg"
IMPACT_DIR = CLIENT / "data" / "foley" / "bullet_impacts" / INVISIBLE


def _peak(sample):
    """The loudest sample in a file, as the game's own decoder reads it.

    The decoder hands the path to libogg as bytes, so a path carrying
    non-ASCII characters (this project lives under one) must be given relative,
    exactly as ``AudioManager.load_buffer`` gives it.
    """
    try:
        path = os.path.relpath(sample, os.getcwd())
    except ValueError:  # Another drive: nothing relative to give.
        path = str(sample)
    info = safe_vorbis.load_vorbis_pcm(path, max_pcm_bytes=32 * 1024 * 1024)
    pcm = bytes(info.buffer)
    return max((abs(int.from_bytes(pcm[i:i + 2], "little", signed=True))
                for i in range(0, len(pcm) - 1, 2)), default=0)


class _Clock:
    def __init__(self):
        self.elapsed = 10.0

    def restart(self):
        self.elapsed = 0.0


class _Map:
    """One tile everywhere: walking any way runs into the same one."""

    def __init__(self, tile):
        self.tile = tile
        self.player = None

    def get_tile_at(self, x, y, z):
        return self.tile

    def get_reverb_at(self, x, y, z):
        return None

    def in_bound(self, x, y, z):
        return True


class _Game:
    pong_mode = False


class _WalkProbe:
    """Enough of an Entity for ``Entity.walk`` to reach the wall branch."""

    def __init__(self, tile):
        self.game = _Game()
        self.map = _Map(tile)
        self.name = "player1"
        self.x = self.y = self.z = 0.0
        self.hfacing = 0.0
        self.player = False  # Skips the voice/music source block.
        self.is_user = True
        self.falling = False
        self.stunned = False
        self.stun_time = 1
        self.stun_clock = _Clock()
        self.fall_clock = _Clock()
        self.on_move = None
        self.soundgroup = SimpleNamespace(position=None)
        self.played = []
        # Nothing worn: the step path alone, which is what this file is about.
        self.armor_sounds_path = None
        self.armor_cloth_volume = 60
        self._play_armor_cloth = Entity._play_armor_cloth.__get__(self)
        # The real step, which the walk branch calls once the tile is free.
        self.move = Entity.move.__get__(self)

    def face(self, *args, **kwargs):
        return None

    def sync_reverb(self):
        return True

    def play_sound(self, path, **kwargs):
        self.played.append(path)


def _walk_into(tile):
    probe = _WalkProbe(tile)
    moved = Entity.walk(probe)
    return probe, moved


class WalkingIntoItTests(unittest.TestCase):
    """The bump sounds by the tile's own name, and the step is refused."""

    def test_a_bump_on_the_invisible_wall_asks_for_its_own_sample(self):
        probe, moved = _walk_into(INVISIBLE)
        self.assertFalse(moved)
        self.assertEqual(probe.played, [f"walls/{INVISIBLE}.ogg"])

    def test_it_blocks_the_step_like_any_wall(self):
        probe, _ = _walk_into(INVISIBLE)
        self.assertEqual((probe.x, probe.y, probe.z), (0.0, 0.0, 0.0))

    def test_an_ordinary_floor_still_moves_and_steps(self):
        probe, moved = _walk_into("grass")
        self.assertTrue(moved)
        self.assertNotEqual((probe.x, probe.y, probe.z), (0.0, 0.0, 0.0))
        self.assertEqual(probe.played, ["steps/grass/walk"])


class ItsSamplesAreShippedSilenceTests(unittest.TestCase):
    """Both samples the name reaches exist, and neither holds a sound."""

    def setUp(self):
        self._cwd = os.getcwd()
        os.chdir(CLIENT)

    def tearDown(self):
        os.chdir(self._cwd)

    def test_the_bump_sample_is_silent(self):
        self.assertTrue(BUMP.is_file())
        self.assertEqual(_peak(BUMP), 0)

    def test_the_bullet_impact_sample_is_silent(self):
        impacts = sorted(IMPACT_DIR.glob("*.ogg"))
        self.assertTrue(impacts, f"no impact sample under {IMPACT_DIR}")
        for sample in impacts:
            self.assertEqual(_peak(sample), 0, f"{sample.name} is not silent")

    def test_ordinary_walls_keep_their_own_sounds(self):
        """The guard that keeps a silent file from silencing the game."""
        for name in ("wall", "wallgeneric", "wallmetal", "wallwood"):
            sample = CLIENT / "data" / "walls" / f"{name}.ogg"
            self.assertTrue(sample.is_file(), f"missing {sample.name}")
            self.assertGreater(_peak(sample), 0, f"{sample.name} went silent")


class ACompiledBuildAnswersTheSameTests(unittest.TestCase):
    """Compiled: the pack carries the file *and* the impact folder."""

    def test_the_pack_carries_both_and_they_stay_silent(self):
        root = Path(tempfile.mkdtemp(prefix="bt-wall-pack-"))
        old_cwd = os.getcwd()
        saved_prepend = consts.SOUNDPREPEND
        saved_sprepend = consts.SOUNDSPREPEND
        try:
            assets = root / "assets"
            (assets / "walls").mkdir(parents=True)
            shutil.copyfile(BUMP, assets / "walls" / BUMP.name)
            impact = assets / "foley" / "bullet_impacts" / INVISIBLE
            impact.mkdir(parents=True)
            shutil.copyfile(IMPACT_DIR / "1.ogg", impact / "1.ogg")
            pack_data.pack_data(assets, root / "sounds.dat",
                                "official.example", 13000)
            os.chdir(root)
            vfs._reset_for_tests()
            vfs.init_vfs()
            try:
                bump = os.path.join(consts.SOUNDPREPEND, f"walls/{INVISIBLE}.ogg")
                self.assertTrue(os.path.isfile(bump))
                self.assertEqual(_peak(bump), 0)

                folder = os.path.join(consts.SOUNDPREPEND,
                                      f"foley/bullet_impacts/{INVISIBLE}")
                self.assertTrue(os.path.isdir(folder),
                                "a pack folder has to answer as a folder")
                picked = path_utils.get_next_cycle_item(folder)
                self.assertTrue(picked.endswith(".ogg"), picked)
                self.assertIn(INVISIBLE, picked)
                self.assertEqual(_peak(picked), 0)
            finally:
                vfs._reset_for_tests()
        finally:
            os.chdir(old_cwd)
            consts.SOUNDPREPEND = saved_prepend
            consts.SOUNDSPREPEND = saved_sprepend
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
