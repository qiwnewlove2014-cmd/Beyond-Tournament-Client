import unittest


class _Tile:
    """Minimal stand-in for a map tile region.

    `occlusion` is left off entirely unless a test passes one, on purpose: an
    older stand-in (this file's own is imported by test_jukebox_responsiveness)
    and a real map written before the Builder could set a wall's sound filter
    both look exactly like this to the ray.
    """

    def __init__(self, minx, maxx, miny, maxy, minz, maxz, tiletype, occlusion=None):
        self.tiletype = tiletype
        if occlusion is not None:
            self.occlusion = occlusion
        self.minx, self.maxx = minx, maxx
        self.miny, self.maxy = miny, maxy
        self.minz, self.maxz = minz, maxz

    def in_bound(self, x, y, z):
        return (
            self.minx <= x <= self.maxx
            and self.miny <= y <= self.maxy
            and self.minz <= z <= self.maxz
        )


def make_map(tiles):
    from libs.world_map import Map

    m = Map.__new__(Map)
    m.tile_list = tiles
    return m


class TestWallOcclusionRatio(unittest.TestCase):
    def test_clear_path_is_fully_clear(self):
        m = make_map([])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.0)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 0)

    def test_single_pillar_partially_occludes(self):
        # One lone pillar tile between source and listener: light muffling.
        m = make_map([_Tile(3, 3, 0, 0, 0, 0, "wallwood")])
        ratio = m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0))
        self.assertAlmostEqual(ratio, 1.0 / 3.0)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 1)

    def test_two_tile_wall_medium(self):
        m = make_map([_Tile(3, 4, 0, 0, 0, 0, "wallwood")])
        ratio = m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0))
        self.assertAlmostEqual(ratio, 2.0 / 3.0)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 1)

    def test_three_tile_wall_full_standard_occlusion(self):
        # A real wall (>= 3 tiles) behaves exactly like the old hard boolean.
        m = make_map([_Tile(3, 5, 0, 0, 0, 0, "wallwood")])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 1.0)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 2)

    def test_two_separate_thin_walls_stack(self):
        # Crossing two pillars absorbs more than crossing one.
        m = make_map([
            _Tile(3, 3, 0, 0, 0, 0, "wallwood"),
            _Tile(7, 7, 0, 0, 0, 0, "wallwood"),
        ])
        self.assertAlmostEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 2.0 / 3.0)

    def test_underwater_partial_floor(self):
        m = make_map([_Tile(0, 10, -5, 5, -5, 5, "underwater")])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.5)

    def test_underwater_never_reduces_a_wall_hit(self):
        m = make_map([
            _Tile(0, 10, -5, 5, -5, 5, "underwater"),
            _Tile(3, 3, 0, 0, 0, 0, "wallwood"),
        ])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.5)

    def test_diagonal_path_counts_crossed_tiles(self):
        # Diagonal walk visits every tile the legacy raycast would visit.
        tiles = [_Tile(4, 4, 4, 4, 0, 0, "wallbrick")]
        m = make_map(tiles)
        ratio = m.wall_occlusion_ratio((0, 0, 0), (8, 8, 0))
        self.assertAlmostEqual(ratio, 1.0 / 3.0)

    def test_listener_inside_wall_still_light_for_single_tile(self):
        # Standing inside/at a lone pillar tile no longer triggers the full
        # heavy filter — only the gentle one.
        m = make_map([_Tile(5, 5, 0, 0, 0, 0, "wallwood")])
        self.assertEqual(m.occlusion_tier((5, 0, 0), (6, 0, 0)), 1)


def real_map(tiles=None):
    """A real Map, with the real Tile class `spawn_platform` builds."""
    from libs.world_map import Map

    m = Map.__new__(Map)
    m.tile_list = tiles if tiles is not None else []
    return m


class WallSoundFilterTests(unittest.TestCase):
    """A wall may carry the Builder's sound filter of its own.

    The Builder writes it as `occlusion` (0-100) on the wall's own element.
    A wall that carries none is judged by how many tiles deep the ray crosses
    it -- the rule every map was written under -- so these tests are about the
    walls that *do* carry one, and about the promise that the others read the
    same as they always did (TestWallOcclusionRatio above, unchanged).
    """

    def test_a_wall_that_declares_full_occlusion_blocks_on_its_own(self):
        # One tile of wall, drawn thin: it says 100, so it is a full wall.
        m = make_map([_Tile(3, 3, 0, 0, 0, 0, "wallbrick", 100)])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 1.0)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 2)

    def test_a_wall_that_lets_sound_through_is_not_a_wall_to_the_ear(self):
        # Glass that is a barrier to the body and to nothing else.
        m = make_map([_Tile(3, 3, 0, 0, 0, 0, "wallglass", 0)])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.0)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 0)

    def test_a_declared_half_is_half(self):
        m = make_map([_Tile(3, 3, 0, 0, 0, 0, "wallwood", 50)])
        self.assertAlmostEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.5)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 1)

    def test_two_declared_walls_add_up(self):
        m = make_map([
            _Tile(3, 3, 0, 0, 0, 0, "wallbrick", 50),
            _Tile(7, 7, 0, 0, 0, 0, "wallbrick", 50),
        ])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 1.0)

    def test_a_declared_wall_sits_beside_a_standard_one(self):
        # 50 + the third a wall of plain thickness is worth is still under a
        # full wall, and it is the third that lifts it there.
        m = make_map([
            _Tile(3, 3, 0, 0, 0, 0, "wallbrick", 50),
            _Tile(7, 7, 0, 0, 0, 0, "wallwood"),
        ])
        self.assertAlmostEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.5 + 1.0 / 3.0)

    def test_a_wall_that_lets_sound_through_adds_no_thickness(self):
        # A "leave the sound alone" wall is worth nothing; the standard wall
        # beside it is still the third it always was.
        m = make_map([
            _Tile(3, 3, 0, 0, 0, 0, "wallglass", 0),
            _Tile(7, 7, 0, 0, 0, 0, "wallwood"),
        ])
        self.assertAlmostEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 1.0 / 3.0)

    def test_a_hand_written_value_is_clamped_rather_than_trusted(self):
        m = make_map([_Tile(3, 3, 0, 0, 0, 0, "wallwood", 250)])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 1.0)
        m = make_map([_Tile(3, 3, 0, 0, 0, 0, "wallwood", -40)])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.0)

    def test_the_water_floor_still_applies_to_a_declared_wall(self):
        # Going under water costs at least half of the sound, whatever the
        # walls on the way are worth.
        m = make_map([
            _Tile(0, 10, -5, 5, -5, 5, "underwater"),
            _Tile(3, 3, 0, 0, 0, 0, "wallglass", 0),
        ])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.5)

    def test_a_floor_with_a_filter_is_not_a_wall(self):
        # The rule keys on the word `wall` in the material's name, exactly as
        # every other behavioural rule in the game does.
        m = make_map([_Tile(3, 3, 0, 0, 0, 0, "grass", 100)])
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 0.0)

    def test_garbage_reads_as_standard_rather_than_as_a_number(self):
        from libs.world_map import _wall_occlusion_percent

        for junk in (None, "", "wallbrick", float("nan"), float("inf"), []):
            self.assertIsNone(_wall_occlusion_percent(junk), repr(junk))
        self.assertEqual(_wall_occlusion_percent("100"), 100.0)
        self.assertEqual(_wall_occlusion_percent(0), 0.0)

    def test_the_real_tile_carries_the_filter_from_the_map_packet(self):
        # The Client spawns elements straight out of the Server's payload
        # (`getattr(map, "spawn_platform")(**element["data"])`), so this is the
        # shape the attribute really arrives in.
        m = real_map()
        m.spawn_platform(minx=3, maxx=3, miny=0, maxy=0, minz=0, maxz=0,
                         type="wallbrick", id="w1", occlusion=100)
        self.assertEqual(m.get_tile_at(3, 0, 0), "wallbrick")
        self.assertEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 1.0)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 2)

    def test_the_real_tile_of_a_map_that_sets_nothing_is_still_standard(self):
        m = real_map()
        m.spawn_platform(minx=3, maxx=3, miny=0, maxy=0, minz=0, maxz=0,
                         type="wallbrick", id="w1")
        self.assertIsNone(m._tile_object_at(3, 0, 0).occlusion)
        self.assertAlmostEqual(m.wall_occlusion_ratio((0, 0, 0), (10, 0, 0)), 1.0 / 3.0)
        self.assertEqual(m.occlusion_tier((0, 0, 0), (10, 0, 0)), 1)

    def test_an_old_style_tile_with_no_attribute_at_all_is_standard(self):
        class Bare:
            tiletype = "wallwood"
            minx = maxx = miny = maxy = minz = maxz = 0

            def in_bound(self, x, y, z):
                return x == y == z == 0

        m = real_map([Bare()])
        self.assertAlmostEqual(m.wall_occlusion_ratio((0, 0, 0), (4, 0, 0)), 1.0 / 3.0)


if __name__ == "__main__":
    unittest.main()
