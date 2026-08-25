import unittest


class _Tile:
    """Minimal stand-in for a map tile region."""

    def __init__(self, minx, maxx, miny, maxy, minz, maxz, tiletype):
        self.tiletype = tiletype
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


if __name__ == "__main__":
    unittest.main()
