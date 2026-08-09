from .vehicle import Vehicle


class Motorcycle(Vehicle):
    """Backward-compatible constructor for legacy motorcycle spawn paths."""

    entity_type = "motorcycle"

    def __init__(self, game, map, x, y, z, hp=500, name="motorcycle"):
        super().__init__(
            game,
            map,
            x,
            y,
            z,
            hp,
            name=name,
            vehicle_type="motorcycle",
            sound_profile="motorcycle",
        )
