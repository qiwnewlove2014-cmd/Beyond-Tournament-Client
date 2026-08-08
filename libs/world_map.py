import contextlib
from . import audio_manager, consts, options
from .objects import entity, motorcycle
import cyal.exceptions
from .speech import speak

from math import sqrt, trunc, floor


class Map:
    def __init__(self, game, minx=0, miny=0, minz=0, maxx=0, maxy=0, maxz=0):
        """Constructs a basic map:
        params:
        minx (int): Minimum x of the map
        miny (int): Minimum y of the map
        minz (int): Minimum Z of the map
        maxx (int): Maximum x of the map
        maxy (int): Maximum y of the map
        maxz (int): Maximum z of the map"""
        self.game = game
        self.player = None
        self.minx, self.miny, self.minz = minx, miny, minz
        self.maxx = maxx
        self.maxy = maxy
        self.maxz = maxz
        # Store our tiles
        self.tile_list = []
        self.door_list = []
        # Store our zones
        self.zone_list = []
        # ambience list
        self.ambience_list = []
        self.pannable_list = []
        self.source_list = []
        self.music_list = []
        self.reverb_list = []
        self.megaphone_speakers = []
        self.entities = {}
        # Triggerable items (synchronised from server for tracking/scan)
        self.wallbuy_list = []
        self.interactable_list = []
        self.perk_machine_list = []
        self.minigame_table_list = []

    def valid_straight_path(self, position1, position2):
        x1, y1, z1 = position1
        x2, y2, z2 = position2
        x1, y1, z1 = trunc(x1), trunc(y1), trunc(z1)
        x2, y2, z2 = trunc(x2), trunc(y2), trunc(z2)
        dist = round(sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2) + (z2 - z1) ** 2) + 1
        result = None
        for n in range(0, dist):
            tile = self.get_tile_at(x1, y1, z1)
            if tile.startswith("wall"):
                result = False
                break
            if tile == "underwater":
                result = None
                break
            if x1 == x2 and y1 == y2 and z1 == z2:
                result = True
                break
            if x1 < x2:
                x1 += 1
            elif trunc(x1) > x2:
                x1 -= 1
            if y1 < y2:
                y1 += 1
            elif y1 > y2:
                y1 -= 1
            if z1 < z2:
                z1 += 1
            elif z1 > z2:
                z1 -= 1
        return result

    def in_bound(self, x, y, z):
        """verifies whether the hole map covers a certain coordinate
        params:
        x (int) the x of the coordinate
        y (int) the y of the coordinate
        z (int) the z of the coordinate
        returns:
        (bool) true if the objects covers this coordinate, or false if otherwise
        """
        try:
            return (
                x >= self.minx
                and x <= self.maxx
                and y >= self.miny
                and y <= self.maxy
                and z >= self.minz
                and z <= self.maxz
            )
        except ValueError as e:
            print(e)
            speak(str(e))
            return False

    def loop(self):
        for i in self.entities.values():
            i.loop()
            i.water_check()
            if not self.player.dead:
                i.soundgroup.aclude_check(self)
        for i in self.source_list:
            if not self.player.dead:
                i.soundgroup.aclude_check(self)
        for i in self.pannable_list:
            if not self.player.dead:
                i.soundgroup.aclude_check(self)

    def destroy(self, destroy_entities=True):
        # audio.set_global_reverb(None)
        # Map-owned effect slots must not be released while a specialized
        # entity still has an auxiliary send attached to them.
        for entity_obj in self.entities.values():
            detach_effects = getattr(entity_obj, "detach_environment_effects", None)
            if callable(detach_effects):
                with contextlib.suppress(Exception):
                    detach_effects()
        for i in self.reverb_list.copy():
            i.destroy()
        if destroy_entities:
            for i in self.entities.values():
                i.destroy()
            self.entities.clear()
        self.reverb_list.clear()
        self.tile_list.clear()
        self.zone_list.clear()
        self.door_list.clear()
        self.wallbuy_list.clear()
        self.interactable_list.clear()
        self.perk_machine_list.clear()
        self.minigame_table_list.clear()
        for i in self.ambience_list.copy():
            i.leave(destroy=True)
        self.ambience_list.clear()
        for i in self.pannable_list.copy():
            i.destroy()
        self.pannable_list.clear()
        for i in self.source_list.copy():
            i.destroy()
        self.source_list.clear()
        for i in self.music_list.copy():
            i.leave(destroy=True)
        self.music_list.clear()
        self.megaphone_speakers.clear()

    def get_ambiences_at(self, x, y, z):
        for i in self.ambience_list:
            if i.in_bound(x, y, z):
                yield i

    def get_musics_at(self, x, y, z):
        for i in self.music_list:
            if i.in_bound(x, y, z):
                yield i

    def get_tile_at(self, x, y, z):
        """Returns a tile at a specified coordinates
        params:
        x (int): The x coordinate from which a tile will be retrieved
        y (int): The y coordinate from which a tile will be retrieved
        z (int): The z coordinate from which a tile will be retrieved
        Return Value:
        A blank string if a tile wasn't found or a tiletype which is within the x, y, and z coordinate
        """
        found_responses = ""
        for i in self.tile_list:
            if i.in_bound(x, y, z):
                found_responses = i.tiletype
        return found_responses

    def get_zone_at(self, x, y, z):
        """Same as get_tile_at, except deals with zones.
        When multiple zones overlap, the smallest zone (by volume) wins.
        On equal volume, the first match in zone_list is kept."""
        best_zone = ""
        best_volume = float('inf')
        for i in self.zone_list:
            if i.in_bound(x, y, z):
                volume = ((i.maxx - i.minx + 1)
                        * (i.maxy - i.miny + 1)
                        * (i.maxz - i.minz + 1))
                if volume < best_volume:
                    best_volume = volume
                    best_zone = i.zonename
        return best_zone

    def spawn_reverb(
        self,
        minx=0,
        maxx=0,
        miny=0,
        maxy=0,
        minz=0,
        maxz=0,
        decayTime=1.49,
        density=1.0,
        diffusion=1.0,
        gain=0.32,
        gainhf=0.89,
        gainlf=1.0,
        hfratio=0.83,
        lfratio=1.0,
        reflectionsGain=0.05,
        reflectionsDelay=0.007,
        reflectionsPan=(0.0, 0.0, 0.0),
        lateReverbGain=1.26,
        lateReverbDelay=0.011,
        lateReverbPan=(0.0, 0.0, 0.0),
        echoTime=0.25,
        echoDepth=0.0,
        modulationTime=0.25,
        modulationDepth=0.0,
        airAbsorptionGainhf=0.994,
        hfrefference=5000.0,
        lfrefference=250.0,
        roomRolloffFactor=0.0,
        id="",
        **kwargs,
    ):
        rev = Reverb(
            self,
            id,
            minx,
            maxx,
            miny,
            maxy,
            minz,
            maxz,
            decayTime,
            density,
            diffusion,
            gain,
            gainhf,
            gainlf,
            hfratio,
            lfratio,
            reflectionsGain,
            reflectionsDelay,
            reflectionsPan,
            lateReverbGain,
            lateReverbDelay,
            lateReverbPan,
            echoTime,
            echoDepth,
            modulationTime,
            modulationDepth,
            airAbsorptionGainhf,
            hfrefference,
            lfrefference,
            roomRolloffFactor,
        )
        index = -1
        for i, element in enumerate(self.reverb_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.reverb_list[index].destroy()
            self.reverb_list[index] = rev
        else:
            self.reverb_list.append(rev)

    def get_reverb_at(self, x, y, z):
        reverb_to_use = None
        for i in self.reverb_list:
            if i.in_bound(x, y, z):
                reverb_to_use = i
        return reverb_to_use

    def spawn_music(self, minx, maxx, miny, maxy, minz, maxz, sound, volume=50, id="", **kwargs):
        music = Ambience(
            self,
            id,
            minx,
            maxx,
            miny,
            maxy,
            minz,
            maxz,
            "music/" + str(sound),
            int(volume),
            type="music",
        )
        index = -1
        for i, element in enumerate(self.music_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.music_list[index].leave()
            self.music_list[index] = music
        else:
            self.music_list.append(music)

    def spawn_ambience(
        self, minx, maxx, miny, maxy, minz, maxz, sound, volume=100, id="", **kwargs
    ):
        ambience = Ambience(self, id, minx, maxx, miny, maxy, minz, maxz, sound, volume)

        index = -1
        for i, element in enumerate(self.ambience_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.ambience_list[index].leave()
            self.ambience_list[index] = ambience
        else:
            self.ambience_list.append(ambience)

    def spawn_soundSource(
        self, minx, maxx, miny, maxy, minz, maxz, sound, volume=100, id="", **kwargs
    ):
        with contextlib.suppress(Exception):
            source = SoundSource(
                self, id, minx, maxx, miny, maxy, minz, maxz, sound, volume
            )
            index = -1
            for i, element in enumerate(self.source_list):
                if element.id == id:
                    index = i
                    break
            if index > -1:
                self.source_list[index].destroy()
                self.source_list[index] = source
            else:
                self.source_list.append(source)

    def spawn_megaphoneSpeaker(self, minx=0, maxx=0, miny=0, maxy=0, minz=0, maxz=0, volume=60, id="", x=None, y=None, z=None, delay=0.0, reverb_decay=2.0, reverb_diffusion=0.8, aim_yaw=None, aim_pitch=-30, inner_cone_angle=45, outer_cone_angle=90, outer_cone_gain=0.2, hearing_range=0.0, eq_bass=50, eq_mid=50, eq_treble=50, **kwargs):
        # Volume comes in as 0-100, convert to 0.0-1.0
        volume = float(volume) / 100.0
        
        # If x, y, z provided directly (from new XML format), use them
        if x is not None and y is not None and z is not None:
            position = (float(x), float(y), float(z))
        else:
            # Fallback to bounds (old format or default)
            position = (float(minx), float(miny), float(minz))
            
        self.megaphone_speakers.append({
            'x': position[0],
            'y': position[1],
            'z': position[2],
            'volume': volume,
            'delay': float(delay),
            'reverb_decay': float(reverb_decay),
            'reverb_diffusion': float(reverb_diffusion),
            # Cone properties for realistic audio
            # An explicitly saved 0 means North.  Only legacy speakers with
            # no aim_yaw field retain the former auto-aim-to-map-centre mode.
            'aim_yaw': float(aim_yaw) if aim_yaw is not None else 0.0,
            'auto_aim': aim_yaw is None or str(kwargs.get('auto_aim', False)).lower() == 'true',
            'aim_pitch': float(aim_pitch),
            'inner_cone_angle': float(inner_cone_angle),
            'outer_cone_angle': float(outer_cone_angle),
            'outer_cone_gain': float(outer_cone_gain),
            'hearing_range': float(hearing_range),
            # EQ properties (0-100)
            'eq_bass': float(eq_bass),
            'eq_mid': float(eq_mid),
            'eq_treble': float(eq_treble)
        })


    def spawn_pannable(self, x, y, z, sound, volume=100, **kwargs):
        with contextlib.suppress(Exception):
            self.pannable_list.append(Pannable(self.game, x, y, z, sound, volume))

    def spawn_zone(
        self,
        minx=0,
        maxx=0,
        miny=0,
        maxy=0,
        minz=0,
        maxz=0,
        innerText="",
        id="",
        **kwargs,
    ):
        """Spawns a zone
        Params:
        minx (int): The minimum x of the zone
        maxx (int): The maximum x of the zone
        miny (int): The minimum y of the zone
        maxy (int): The maximum y of the zone
        minz (int): The minimum z of the zone
        maxz (int): The maximum z of the zone"""
        zone = Zone(id, minx, maxx, miny, maxy, minz, maxz, innerText)
        index = -1
        for i, element in enumerate(self.zone_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.zone_list[index] = zone
        else:
            self.zone_list.append(zone)

    def spawn_platform(
        self, minx=0, maxx=0, miny=0, maxy=0, minz=0, maxz=0, type="", id="", **kwargs
    ):
        """Spawns a platform
        Params:
        minx (int): The minimum x of the tile
        maxx (int): The maximum x of the tile
        miny (int): The minimum y of the tile
        maxy (int): The maximum y of the tile
        minz (int): The minimum z of the tile
        maxz (int): The maximum z of the tile"""
        tile = Tile(id, minx, maxx, miny, maxy, minz, maxz, type)
        index = -1
        for i, element in enumerate(self.tile_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.tile_list[index] = tile
        else:
            self.tile_list.append(tile)

    def spawn_door(
        self,
        minx=0,
        maxx=0,
        miny=0,
        maxy=0,
        minz=0,
        maxz=0,
        id="",
        **kwargs,
    ):
        """Spawns a door
        Params:
        minx (int): The minimum x of the tile
        maxx (int): The maximum x of the tile
        miny (int): The minimum y of the tile
        maxy (int): The maximum y of the tile
        minz (int): The minimum z of the tile
        maxz (int): The maximum z of the tile
        walltype (str) the type of the wall when the door is closed
        tiletype (str) the tile of of the door when it is open
        minpoints (int) the minimum number of points you need to open the door
        """
        doorobj = Door(id, minx, maxx, miny, maxy, minz, maxz)
        self.door_list.append(doorobj)

    def spawn_wallbuy(
        self,
        minx=0,
        maxx=0,
        miny=0,
        maxy=0,
        minz=0,
        maxz=0,
        id="",
        weaponName="",
        weaponCost=0,
        ammoCost=0,
        **kwargs,
    ):
        """Spawns a wallbuy (weapon buy station). Uses id-based replace so it
        is rebuild-safe."""
        wb = Wallbuy(id, minx, maxx, miny, maxy, minz, maxz, weaponName, weaponCost, ammoCost)
        index = -1
        for i, element in enumerate(self.wallbuy_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.wallbuy_list[index] = wb
        else:
            self.wallbuy_list.append(wb)

    def spawn_interactable(
        self,
        minx=0,
        maxx=0,
        miny=0,
        maxy=0,
        minz=0,
        maxz=0,
        id="",
        innerText="",
        **kwargs,
    ):
        """Spawns a generic interactable object."""
        obj = Interactable(id, minx, maxx, miny, maxy, minz, maxz, innerText)
        index = -1
        for i, element in enumerate(self.interactable_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.interactable_list[index] = obj
        else:
            self.interactable_list.append(obj)

    def spawn_perkMachine(
        self,
        minx=0,
        maxx=0,
        miny=0,
        maxy=0,
        minz=0,
        maxz=0,
        id="",
        perk="",
        name="",
        **kwargs,
    ):
        """Spawns a perk machine."""
        obj = PerkMachine(id, minx, maxx, miny, maxy, minz, maxz, perk, name)
        index = -1
        for i, element in enumerate(self.perk_machine_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.perk_machine_list[index] = obj
        else:
            self.perk_machine_list.append(obj)

    def spawn_minigameTable(
        self,
        minx=0,
        maxx=0,
        miny=0,
        maxy=0,
        minz=0,
        maxz=0,
        id="",
        game_type="pong",
        **kwargs,
    ):
        """Spawns an arcade/minigame table."""
        obj = MinigameTable(id, minx, maxx, miny, maxy, minz, maxz, game_type)
        index = -1
        for i, element in enumerate(self.minigame_table_list):
            if element.id == id:
                index = i
                break
        if index > -1:
            self.minigame_table_list[index] = obj
        else:
            self.minigame_table_list.append(obj)

    def get_min_x(self):
        """Returns the minimum x"""
        return self.minx

    def get_min_y(self):
        """Returns the minimum y"""
        return self.miny

    def get_min_z(self):
        """Returns the minimum z"""
        return self.minz

    def get_max_x(self):
        """Returns the maximum x"""
        return self.maxx

    def get_max_y(self):
        """Returns the maximum y"""
        return self.maxy

    def get_max_z(self):
        """Returns the maximum z"""
        return self.maxz

    def spawn_playerSpawn(self, **kwargs):
        pass

    def spawn_zombieSpawn(self, **kwargs):
        pass

    def spawn_entity(self, name, x, y, z, hp=100, entity_type=None):
        if self.entities.get(name):
            self.entities[name].destroy()
        if entity_type == "motorcycle":
            spawned = motorcycle.Motorcycle(self.game, self, x, y, z, hp, name=name)
        else:
            spawned = entity.Entity(self.game, self, x, y, z, hp, name=name)
        self.entities[name] = spawned
        return self.entities[name]

    def get_entities_at(self, x, y, z):
        for i in self.entities.values():
            if i.x == x and i.y == y and i.z == z:
                yield i

    def remove_entity(self, name):
        if entity := self.entities.get(name):
            entity.destroy()
            del self.entities[name]


class BaseMapObj:
    """base map object
    this object is the base class from where tiles, zones and custom map objects inherit
    """

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, type):
        """the BaseMapObj constructor
        params:
        minx (int) the minimum x, from where  the object starts
        maxx (int) the maximum x of the map
        miny (int) the minimum y of the map
        maxy (int) the maximum y of the map
        minz (int) the minimum z of the map
        maxz (int) the maximum z of the map
        type (str) the type of the map object, tile, zone, or whatever
        """
        self.id = id
        self.minx = minx
        self.maxx = maxx
        self.miny = miny
        self.maxy = maxy
        self.minz = minz
        self.maxz = maxz
        self.type = type

    def in_bound(self, x, y, z):
        """verifies whether the current object covers a certain coordinate
        params:
        x (int) the x of the coordinate
        y (int) the y of the coordinate
        z (int) the z of the coordinate
        returns:
        (bool) true if the objects covers this coordinate, or false if otherwise
        """
        # 🛡️ Protection: Return false if any coordinate is None
        if x is None or y is None or z is None:
            return False
        try:
            ix = floor(x)
            iy = floor(y)
            iz = floor(z)
            return (
                ix >= floor(self.minx)
                and ix <= floor(self.maxx)
                and iy >= floor(self.miny)
                and iy <= floor(self.maxy)
                and iz >= floor(self.minz)
                and iz <= floor(self.maxz)
            )
        except TypeError as e:
            return False


class Reverb(BaseMapObj):
    def __init__(
        self,
        map,
        id,
        minx,
        maxx,
        miny,
        maxy,
        minz,
        maxz,
        t60,
        density,
        diffusion,
        gain,
        gainhf,
        gainlf,
        hfratio,
        lfratio,
        reflections_gain,
        reflections_delay,
        reflections_pan,
        late_reverb_gain,
        late_reverb_delay,
        late_reverb_pan,
        echo_time,
        echo_depth,
        modulation_time,
        modulation_depth,
        air_absorption_gainhf,
        hfrefference,
        lfrefference,
        room_rolloff_factor,
    ):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "reverb")
        self.map = map
        self.decay_time = t60
        self.reverb = self.map.game.audio_mngr.gen_effect(
            "EAXREVERB",
            ("decay_time", float(t60)),
            ("density", float(density)),
            ("diffusion", float(diffusion)),
            ("gain", float(gain)),
            ("gainhf", float(gainhf)),
            ("gainlf", float(gainlf)),
            ("decay_hfratio", float(hfratio)),
            ("decay_lfratio", float(lfratio)),
            ("reflections_gain", float(reflections_gain)),
            ("reflections_delay", float(reflections_delay)),
            ("reflections_pan", tuple(reflections_pan)),
            ("late_reverb_gain", float(late_reverb_gain)),
            ("late_reverb_delay", float(late_reverb_delay)),
            ("late_reverb_pan", tuple(late_reverb_pan)),
            ("echo_time", float(echo_time)),
            ("echo_depth", float(echo_depth)),
            ("modulation_time", float(modulation_time)),
            ("modulation_depth", float(modulation_depth)),
            ("air_absorption_gainhf", float(air_absorption_gainhf)),
            ("hfreference", float(hfrefference)),
            ("lfreference", float(lfrefference)),
            ("room_rolloff_factor", float(room_rolloff_factor)),
        )

    def destroy(self):
        with contextlib.suppress(Exception):
            if self.reverb:
                # Detach from audio manager sends if it was the active reverb
                am = self.map.game.audio_mngr
                for send_idx, slot in enumerate(am.sends):
                    if slot == self.reverb:
                        am.apply_effect(None, send_idx)
                
                am.release_effect_slot(self.reverb)
                self.reverb = None


class Ambience(BaseMapObj):
    def __init__(
        self,
        map,
        id,
        minx,
        maxx,
        miny,
        maxy,
        minz,
        maxz,
        sound,
        volume=100,
        type="ambience",
    ):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, type)
        self.map = map
        self.file = sound if sound is not str else ""
        self.volume = volume
        self.fade_time = 0.5
        self.soundgroup = self.map.game.audio_mngr.create_soundgroup(
            direct=True, filterable=True if type == "ambience" else False
        )
        self.sound = self.soundgroup.play(self.file, True, cat=type, volume=self.volume)

        self.playing = False
        with contextlib.suppress(AttributeError):
            self.sound.source.gain = 0.0
            self.sound.muted = True

    def enter(self):
        if not self.playing:
            self.playing = True
            if self.sound and self.sound.source is not None:
                try:
                    self.map.game.automate(
                        self.sound.source,
                        "gain",
                        (self.volume / 100)
                        * (self.map.game.audio_mngr.volume_categories[self.type][0] / 100),
                        500,
                        callback=lambda: setattr(self.sound, "muted", False),
                    )
                except cyal.exceptions.InvalidOperationError:
                    pass

    def leave(self, destroy=False):
        def _on_fade_complete():
            if self.sound:
                setattr(self.sound, "muted", True)
                if destroy:
                    self.sound.destroy()

        if self.playing:
            self.playing = False
            if self.sound:
                try:
                    self.map.game.automate(
                        self.sound.source,
                        "gain",
                        0.0,
                        500,
                        callback=_on_fade_complete,
                    )
                except cyal.exceptions.InvalidOperationError:
                    if destroy:
                        self.sound.destroy()
        else:
            if self.sound and destroy:
                self.sound.destroy()


class Tile(BaseMapObj):
    """An internal tile class. You do not need to create any objects with this type externally"""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, type):
        super(Tile, self).__init__(id, minx, maxx, miny, maxy, minz, maxz, "tile")
        self.tiletype = type


class Door(BaseMapObj):
    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "door")


class Zone(BaseMapObj):
    """an internal zone class"""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, name):
        super(Zone, self).__init__(id, minx, maxx, miny, maxy, minz, maxz, "zone")
        self.zonename = name


class Wallbuy(BaseMapObj):
    """A wall-mounted weapon buy station. Mirrors server Wallbuy bounds (±1)."""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, weaponName="", weaponCost=0, ammoCost=0):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "wallbuy")
        self.weaponName = weaponName
        self.weaponCost = weaponCost
        self.ammoCost = ammoCost

    def in_bound(self, x, y, z):
        # Match server: wallbuy is interactable from ±1 tile around its bounds
        if x is None or y is None or z is None:
            return False
        try:
            ix = floor(x); iy = floor(y); iz = floor(z)
            return (
                ix >= floor(self.minx) - 1 and ix <= floor(self.maxx) + 1
                and iy >= floor(self.miny) - 1 and iy <= floor(self.maxy) + 1
                and iz >= floor(self.minz) - 1 and iz <= floor(self.maxz) + 1
            )
        except TypeError:
            return False


class Interactable(BaseMapObj):
    """A generic interactable object (lever, switch, button, etc.)."""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, innerText=""):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "interactable")
        self.label = innerText or "Interactable"


class PerkMachine(BaseMapObj):
    """A perk-a-cola machine. Display name comes from server 'name' or 'perk'."""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, perk="", name=""):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "perkMachine")
        self.perk = perk
        self.label = name or perk or "Perk Machine"


class MinigameTable(BaseMapObj):
    """An arcade/minigame table (e.g. Pong, Blackjack, Multi-Game)."""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, game_type="pong"):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "minigameTable")
        self.game_type = game_type or "pong"
        if self.game_type == "blackjack":
            self.label = "Blackjack Arcade Table"
        elif self.game_type == "all":
            self.label = "Multi-Game Arcade Cabinet"
        else:
            self.label = "Pong Arcade Table"


class Pannable(BaseMapObj):
    def __init__(self, game, x, y, z, sound, volume=100):
        super().__init__(x, x, y, y, z, z, sound)
        self.game = game
        self.soundgroup = self.game.audio_mngr.create_soundgroup()
        self.soundgroup.position = (x, y, z)
        self.map = map
        self.decay_time = t60
        self.reverb = self.map.game.audio_mngr.gen_effect(
            "EAXREVERB",
            ("decay_time", float(t60)),
            ("density", float(density)),
            ("diffusion", float(diffusion)),
            ("gain", float(gain)),
            ("gainhf", float(gainhf)),
            ("gainlf", float(gainlf)),
            ("decay_hfratio", float(hfratio)),
            ("decay_lfratio", float(lfratio)),
            ("reflections_gain", float(reflections_gain)),
            ("reflections_delay", float(reflections_delay)),
            ("reflections_pan", tuple(reflections_pan)),
            ("late_reverb_gain", float(late_reverb_gain)),
            ("late_reverb_delay", float(late_reverb_delay)),
            ("late_reverb_pan", tuple(late_reverb_pan)),
            ("echo_time", float(echo_time)),
            ("echo_depth", float(echo_depth)),
            ("modulation_time", float(modulation_time)),
            ("modulation_depth", float(modulation_depth)),
            ("air_absorption_gainhf", float(air_absorption_gainhf)),
            ("hfreference", float(hfrefference)),
            ("lfreference", float(lfrefference)),
            ("room_rolloff_factor", float(room_rolloff_factor)),
        )

    def destroy(self):
        with contextlib.suppress(Exception):
            if self.reverb:
                # Detach from audio manager sends if it was the active reverb
                am = self.map.game.audio_mngr
                for send_idx, slot in enumerate(am.sends):
                    if slot == self.reverb:
                        am.apply_effect(None, send_idx)
                
                am.release_effect_slot(self.reverb)
                self.reverb = None


class Ambience(BaseMapObj):
    def __init__(
        self,
        map,
        id,
        minx,
        maxx,
        miny,
        maxy,
        minz,
        maxz,
        sound,
        volume=100,
        type="ambience",
    ):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, type)
        self.map = map
        self.file = sound if sound is not str else ""
        self.volume = volume
        self.fade_time = 0.5
        self.soundgroup = self.map.game.audio_mngr.create_soundgroup(
            direct=True, filterable=True if type == "ambience" else False
        )
        self.sound = self.soundgroup.play(self.file, True, cat=type, volume=self.volume)

        self.playing = False
        with contextlib.suppress(AttributeError):
            self.sound.source.gain = 0.0
            self.sound.muted = True

    def enter(self):
        if not self.playing:
            self.playing = True
            if self.sound and self.sound.source is not None:
                try:
                    self.map.game.automate(
                        self.sound.source,
                        "gain",
                        (self.volume / 100)
                        * (self.map.game.audio_mngr.volume_categories[self.type][0] / 100),
                        500,
                        callback=lambda: setattr(self.sound, "muted", False),
                    )
                except cyal.exceptions.InvalidOperationError:
                    pass

    def leave(self, destroy=False):
        def _on_fade_complete():
            if self.sound:
                setattr(self.sound, "muted", True)
                if destroy:
                    self.sound.destroy()

        if self.playing:
            self.playing = False
            if self.sound:
                try:
                    self.map.game.automate(
                        self.sound.source,
                        "gain",
                        0.0,
                        500,
                        callback=_on_fade_complete,
                    )
                except cyal.exceptions.InvalidOperationError:
                    if destroy:
                        self.sound.destroy()
        else:
            if self.sound and destroy:
                self.sound.destroy()


class Tile(BaseMapObj):
    """An internal tile class. You do not need to create any objects with this type externally"""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, type):
        super(Tile, self).__init__(id, minx, maxx, miny, maxy, minz, maxz, "tile")
        self.tiletype = type


class Door(BaseMapObj):
    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "door")


class Zone(BaseMapObj):
    """an internal zone class"""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, name):
        super(Zone, self).__init__(id, minx, maxx, miny, maxy, minz, maxz, "zone")
        self.zonename = name


class Wallbuy(BaseMapObj):
    """A wall-mounted weapon buy station. Mirrors server Wallbuy bounds (±1)."""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, weaponName="", weaponCost=0, ammoCost=0):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "wallbuy")
        self.weaponName = weaponName
        self.weaponCost = weaponCost
        self.ammoCost = ammoCost

    def in_bound(self, x, y, z):
        # Match server: wallbuy is interactable from ±1 tile around its bounds
        if x is None or y is None or z is None:
            return False
        try:
            ix = floor(x); iy = floor(y); iz = floor(z)
            return (
                ix >= floor(self.minx) - 1 and ix <= floor(self.maxx) + 1
                and iy >= floor(self.miny) - 1 and iy <= floor(self.maxy) + 1
                and iz >= floor(self.minz) - 1 and iz <= floor(self.maxz) + 1
            )
        except TypeError:
            return False


class Interactable(BaseMapObj):
    """A generic interactable object (lever, switch, button, etc.)."""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, innerText=""):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "interactable")
        self.label = innerText or "Interactable"


class PerkMachine(BaseMapObj):
    """A perk-a-cola machine. Display name comes from server 'name' or 'perk'."""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, perk="", name=""):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "perkMachine")
        self.perk = perk
        self.label = name or perk or "Perk Machine"


class MinigameTable(BaseMapObj):
    """An arcade/minigame table (e.g. Pong, Blackjack, Multi-Game)."""

    def __init__(self, id, minx, maxx, miny, maxy, minz, maxz, game_type="pong"):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, "minigameTable")
        self.game_type = game_type or "pong"
        if self.game_type == "blackjack":
            self.label = "Blackjack Arcade Table"
        elif self.game_type == "all":
            self.label = "Multi-Game Arcade Cabinet"
        else:
            self.label = "Pong Arcade Table"


class Pannable(BaseMapObj):
    def __init__(self, game, x, y, z, sound, volume=100):
        super().__init__(x, x, y, y, z, z, sound)
        self.game = game
        self.soundgroup = self.game.audio_mngr.create_soundgroup()
        self.soundgroup.position = (x, y, z)
        self.sound = self.soundgroup.play(sound, looping=True, volume=volume)

    def destroy(self):
        with contextlib.suppress(Exception):
            if self.sound:
                self.sound.destroy()


class SoundSource(BaseMapObj):
    def __init__(self, map, id, minx, maxx, miny, maxy, minz, maxz, sound, volume=100):
        super().__init__(id, minx, maxx, miny, maxy, minz, maxz, sound)
        self.map = map
        self.soundgroup = self.map.game.audio_mngr.create_soundgroup(radius=10.0, filterable=True)
        self.sound = None
        self.path = sound
        self.volume = volume
        self.playing = False
        self.fade_range = 25.0
        self.current_gain = 0.0

    def loop(self, player_x, player_y, player_z):
        if not self.soundgroup:
            return

        # Calculate 3D distance to the nearest point on the bounding box (with 3.5x Z altitude weighting)
        dx = max(0.0, self.minx - player_x, player_x - self.maxx)
        dy = max(0.0, self.miny - player_y, player_y - self.maxy)
        dz = max(0.0, self.minz - player_z, player_z - self.maxz) * 3.5
        distance = (dx * dx + dy * dy + dz * dz) ** 0.5

        # Calculate distance-attenuated target gain using Smoothstep (S-Curve) for realistic auditory falloff
        t = max(0.0, min(1.0, 1.0 - (distance / self.fade_range)))
        fade_factor = t * t * (3.0 - 2.0 * t)
        
        ui_cat_vol = self.map.game.audio_mngr.volume_categories.get("sound_source", [100])[0] / 100.0
        base_gain = (self.volume / 100.0) * ui_cat_vol
        target_gain = base_gain * fade_factor

        # Smooth frame-by-frame interpolation for buttery smooth fade in/out (eliminates pops and thread conflicts!)
        if self.current_gain < target_gain:
            self.current_gain = min(target_gain, self.current_gain + 0.05)
        elif self.current_gain > target_gain:
            self.current_gain = max(target_gain, self.current_gain - 0.05)

        # Once completely silent and out of range, cleanly pause to prevent leaks
        if self.current_gain <= 0.0 and target_gain <= 0.0:
            if self.playing:
                self.playing = False
                if self.sound and hasattr(self.sound, "source") and self.sound.source:
                    with contextlib.suppress(Exception):
                        self.sound.source.gain = 0.0
                        self.sound.source.pause()
            return

        # Position sound source at closest boundary point for 3D spatial panning when turning
        cx = self.check_out_x(player_x)
        cy = self.check_out_y(player_y)
        cz = self.check_out_z(player_z)

        if distance == 0:
            self.soundgroup.position = (cx, cy + 0.5, cz)
        else:
            self.soundgroup.position = (cx, cy, cz)

        if not self.playing:
            self.playing = True
            if not self.sound:
                self.sound = self.soundgroup.play(
                    self.path, True, cat="sound_source", volume=self.volume
                )
                if self.sound and hasattr(self.sound, "source") and self.sound.source:
                    with contextlib.suppress(Exception):
                        self.sound.source.gain = self.current_gain
                        self.sound.source.spatialize = True
                        self.sound.source.direct_channels = False
                        if hasattr(self.sound.source, "radius"):
                            self.sound.source.radius = 15.0
                        if hasattr(self.sound.source, "reference_distance"):
                            self.sound.source.reference_distance = 10.0
            else:
                with contextlib.suppress(Exception):
                    if self.sound and self.sound.source:
                        self.sound.source.gain = self.current_gain
                        self.sound.source.play()

        if self.sound and hasattr(self.sound, "source") and self.sound.source:
            with contextlib.suppress(Exception):
                self.sound.source.gain = self.current_gain
                if hasattr(self.map.game.audio_mngr, "efx"):
                    # Get reverb at the sound source's spatialized position, NOT the player's position,
                    # so the echo naturally bleeds out of the zone when the player walks away.
                    reverb_zone = self.map.get_reverb_at(cx, cy, cz)
                    reverb = reverb_zone.reverb if reverb_zone and hasattr(reverb_zone, "reverb") else None
                    self.map.game.audio_mngr.efx.send(self.sound.source, 0, reverb)

    def check_out_x(self, x):
        if self.minx <= x <= self.maxx:
            return x
        return self.minx if x < self.minx else self.maxx

    def check_out_y(self, y):
        if self.miny <= y <= self.maxy:
            return y
        return self.miny if y < self.miny else self.maxy

    def check_out_z(self, z):
        if self.minz <= z <= self.maxz:
            return z
        return self.minz if z < self.minz else self.maxz

    def destroy(self):
        with contextlib.suppress(Exception):
            if self.sound:
                self.sound.destroy()
