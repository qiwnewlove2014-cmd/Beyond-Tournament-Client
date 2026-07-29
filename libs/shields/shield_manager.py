import os
import random
from .. import consts, path_utils


class ShieldManager:
    """Manages Client Shield Audio, Hold Stance, and Acoustic Bandpass Filtering."""

    def __init__(self, gameplay):
        self.gameplay = gameplay
        self.game = gameplay.game
        self.equipped_shield = None
        self.is_raising = False
        self.acoustic_filter = None

    def equip_shield(self, shield_data):
        """Equip a shield on the client side.

        shield_data dict example:
        {
            "id": "wood_shield_1",
            "name": "Wooden Shield",
            "material": "wood1",
            "sounds_path": "shields/wood1",
            "durability": 400,
            "max_durability": 400
        }
        """
        self.equipped_shield = shield_data

    def unequip_shield(self):
        if self.is_raising:
            self.lower_shield()
        self.equipped_shield = None

    def get_sound_path(self, sound_name):
        """Get the relative sound path for a given shield sound."""
        if not self.equipped_shield:
            sounds_path = "shields/wood1"
        else:
            sounds_path = self.equipped_shield.get("sounds_path", "shields/wood1")
        return f"{sounds_path}/{sound_name}"

    def raise_shield(self):
        """Play raise sound and activate raising stance."""
        if self.is_raising:
            return
        self.is_raising = True
        
        sound_path = self.get_sound_path("raise.ogg")
        self.game.audio_mngr.play_unbound(sound_path, 0, 0, 0, direct=True)

    def lower_shield(self):
        """Play lower sound and deactivate raising stance."""
        if not self.is_raising:
            return
        self.is_raising = False

        sound_path = self.get_sound_path("lower.ogg")
        self.game.audio_mngr.play_unbound(sound_path, 0, 0, 0, direct=True)

    def play_impact_sound(self, x=None, y=None, z=None, is_local=True):
        """Play impact sound (impact1.ogg - impact3.ogg) when shield takes hit."""
        impact_idx = random.randint(1, 3)
        sound_path = self.get_sound_path(f"impact{impact_idx}.ogg")

        if is_local or x is None:
            self.game.audio_mngr.play_unbound(sound_path, 0, 0, 0, direct=True)
        else:
            self.game.audio_mngr.play_unbound(sound_path, x, y, z, direct=False)

    def play_break_sound(self, x=None, y=None, z=None, is_local=True):
        """Play break sound when shield durability reaches 0."""
        sound_path = self.get_sound_path("break.ogg")

        if is_local or x is None:
            self.game.audio_mngr.play_unbound(sound_path, 0, 0, 0, direct=True)
        else:
            self.game.audio_mngr.play_unbound(sound_path, x, y, z, direct=False)

        self.unequip_shield()

    def enable_acoustic_filter(self):
        pass

    def disable_acoustic_filter(self):
        pass
