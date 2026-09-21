class ArmorManager:
    """The armor the local player is wearing, and the cloth it makes on steps.

    The shield manager's own shape, with one deliberate difference: nothing here
    plays a sound for the armour itself. Putting a piece on, taking a hit and the
    piece coming apart are all *positions on the map*, so the Server already
    sends each of them to every player on it -- the wearer included -- and
    playing them here as well is how a single impact becomes two overlapping
    ones. What lives on this side is the state (what is worn, and how much of it
    is left as the server last reported it) and the cloth the wearer's own
    footsteps carry.

    A piece marked ``cloth: false`` in its definition is worn silently, which is
    a real choice for a builder: heavy plate can be heard coming, a padded
    jacket can be quiet.
    """

    def __init__(self, gameplay):
        self.gameplay = gameplay
        self.game = gameplay.game
        self.equipped_armor = None

    def equip_armor(self, data):
        """Remember the piece the Server says this player is wearing."""
        self.equipped_armor = data if isinstance(data, dict) else None
        self._apply_to_player()

    def unequip_armor(self):
        """The piece is off: a break, a death, or a round reset."""
        self.equipped_armor = None
        self._apply_to_player()

    def sounds_path(self):
        """The folder this piece plays from, or None."""
        if not self.equipped_armor:
            return None
        return self.equipped_armor.get("sounds_path") or None

    def _apply_to_player(self):
        """Hand the player entity the cloth its own steps should carry.

        The entity is the one that walks, and it is also where a *remote*
        player's armor arrives (with each step packet), so both sides of "whose
        steps sound like armor" read from the same two fields.
        """
        player = getattr(self.gameplay, "player", None)
        if player is None:
            return
        armor = self.equipped_armor
        if not armor or armor.get("cloth") is False:
            player.set_armor_cloth(None)
            return
        player.set_armor_cloth(armor.get("sounds_path"), armor.get("cloth_volume"))
