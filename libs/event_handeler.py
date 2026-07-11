import time
import random
import os
import base64
import cyal.exceptions
import pyperclip
import functools
import contextlib
import webbrowser
import cyal
from . import audio_manager, buffer, gameplay, menu, menus, options, consts
from .speech import speak
from .weapons import weapon
from . import tickets
from pyogg import OpusDecoder

class EventHandeler:
    def __init__(self, client, game):
        self.client = client
        self.game = game
        self.gameplay = gameplay.Gameplay(self.game)
        self.tickets = tickets.Tickets(self.game)

    def create_fail(self, data):
        menus.main_menu(self.game)
        speak("Account creation failed.", False)

    def create_done(self, data):
        menus.main_menu(self.game)
        speak(
            "Account creation finished. You can now login using the given information",
            False,
        )

    def connected(self, data):
        self.game.reconnecting = False
        self.client.put(("connected", True))
        self.game.replace(self.gameplay)
        self.gameplay.player.name = data["username"]
        if hasattr(self.game, 'instance_mngr'):
            self.game.instance_mngr.set_character(data["username"])
        
        self.game.available_languages = data.get("available_languages", {})
        self.game.current_language = data.get("current_language", "th")

        # Store staff status for PA Test Mode (with safe fallback)
        try:
            self.gameplay.is_staff = bool(data.get("is_staff", False))
            self.gameplay.is_builder = bool(data.get("is_builder", False))
        except Exception:
            self.gameplay.is_staff = False
            self.gameplay.is_builder = False
            
        # Reset PA Test Mode state
        if hasattr(self.gameplay, 'pa_test_mode'):
            self.gameplay.pa_test_mode = False
        
        # Cleanup old voice chat instance to prevent stale state crash
        if hasattr(self.gameplay, 'voice_chat') and self.gameplay.voice_chat:
            try:
                if self.gameplay.voice_chat.recording:
                    self.gameplay.voice_chat.audio_input.stop()
                self.gameplay.voice_chat.close()
            except Exception:
                pass
            self.gameplay.voice_chat = None
            
        # Cleanup stale voice channels (especially Megaphone which holds compression threads)
        if hasattr(self.gameplay, 'voice_channels') and isinstance(self.gameplay.voice_channels, dict):
             self.gameplay.voice_channels.clear()
            
        speak("Welcome. You are now online")

    def speak(self, data):
        if data["buffer"]:
            buffer.add_item(
                self.game,
                data["buffer"],
                data["text"],
                True,
                sound=data.get("sound", ""),
            )
            speak(data["text"], silent=True, id=f'buffer_{data["buffer"]}')
        else:
            speak(data["text"], data["interupt"], not data["buffer"])
            if data["sound"]:
                self.game.direct_soundgroup.play(data["sound"])

    def online(self, data):
        buffer.add_item(
            self.game,
            "players",
            f'{data["username"]} came online.',
            sound="ui/online.ogg",
        )

    def offline(self, data):
        buffer.add_item(
            self.game,
            "players",
            f'{data["username"]} went offline.',
            sound="ui/offline.ogg",
        )

    def kick(self, data):
        buffer.add_item(
            self.game, "players", f'{data["username"]} was kicked by a moderator. '
        )

    def ping(self, data):
        if self.gameplay:
            speak(
                f"The ping took {int((time.time() - self.gameplay.last_ping_time)*1000)}ms"
            )
            self.gameplay.pingging = False

    def parse_map(self, data):
        self.game.automations.clear()
        self.game.audio_mngr.apply_filter(
            None, exclude=self.game.exclude_water, clear=True
        )
        self.gameplay.parser.load(data["data"])
        self.gameplay.player.move(data["x"], data["y"], data["z"], play_sound=False)
        # Setup megaphone speakers after map data is loaded
        self.gameplay.setup_megaphone_speakers()
        # === Load Music Bot playlist for this map ===
        if hasattr(self.gameplay, 'music_bot') and self.gameplay.music_bot:
            self.gameplay.music_bot.load_map_music(data["data"])

    def update_map(self, data):
        for a in self.game.automations.copy():
            if a.cancelable:
                self.game.automations.pop(self.game.automations.index(a))
        self.game.audio_mngr.apply_filter(
            None, exclude=self.game.exclude_water, clear=True
        )
        self.gameplay.player.in_water = False
        self.game.ignore_others_water = False
        self.game.exclude_water.clear()
        for i in self.gameplay.map.entities.values():
            i.in_water = False
            i.water_check()

        self.gameplay.parser.load(data["data"], False)
        self.gameplay.player.move(
            self.gameplay.player.x, self.gameplay.player.y, self.gameplay.player.z
        )
        self.gameplay.setup_megaphone_speakers()
        # === Reload Music Bot playlist for updated map ===
        if hasattr(self.gameplay, 'music_bot') and self.gameplay.music_bot:
            self.gameplay.music_bot.load_map_music(data["data"])

    def rebuild_elements(self, data):
        elements = data["elements"]
        map = self.gameplay.map
        has_megaphone = False
        for element in elements:
            type = element["type"]
            id = element["data"]["id"]
            if type == "megaphoneSpeaker":
                has_megaphone = True
            if hasattr(map, f"spawn_{type}"):
                getattr(map, f"spawn_{type}")(**element["data"])
        if has_megaphone:
            self.gameplay.setup_megaphone_speakers()

    def spawn_entity(self, data):
        entity = self.gameplay.map.spawn_entity(
            data["name"], data["x"], data["y"], data["z"]
        )
        if data.get("voice_channel", None) != None:
            self.gameplay.voice_channels[data["voice_channel"]] = entity
        if data.get("player", False):
            entity.player = True
            
        if data["name"] == "ball":
            entity.soundgroup.play("Pong/rolling.ogg", looping=True, id="ball_roll", cat="miscelaneous")
            
        if data.get("beacon", False) and options.get("beacons"):
            try: 
                entity.beacon = entity.play_sound(
                "ui/beacon.ogg", looping=True, cat="players"
            )
                entity.beacon.force_to_destroy = True
                try:
                    entity.beacon.source.pitch = random.randint(98, 102) / 100
                except AttributeError as e:
                    print(e)
            except:
                pass

        # Auto-focus spectator camera if this is the target player we were spectating
        if getattr(self.gameplay, "spectator_mode", False) and data["name"] == getattr(self.gameplay, "spectator_target_name", ""):
            self.gameplay.camera.set_focus_object(entity)
            try:
                if hasattr(entity, 'soundgroup') and entity.soundgroup:
                    entity.soundgroup.volume = 1.0
            except Exception:
                pass
            try:
                if hasattr(entity, 'vc_source') and entity.vc_source:
                    entity.vc_source.gain = 1.0
            except Exception:
                pass

    def remove_entity(self, data):
        self.gameplay.voice_channels = { k: v for k, v in self.gameplay.voice_channels.items() if v.name != data["name"] }
        self.gameplay.map.remove_entity(data["name"])

    def play_sound(self, data):
        if entity := (
            self.gameplay.player
            if data["name"] == self.gameplay.player.name
            else self.gameplay.map.entities.get(data["name"])
        ):
            entity.play_sound(
                data["sound"],
                data["looping"],
                id=data.get("id", ""),
                cat=data.get("cat", "miscelaneous"),
                volume=data.get("volume", 100),
                pitch=data.get("pitch", 1.0)
            )
            if data.get("dist_path"):
                entity.play_sound_dist(
                    data["dist_path"],
                    data["looping"],
                    data["volume"],
                    data.get("id", ""),
                    cat=data.get("cat", "miscelaneous"),
                    pitch=data.get("pitch", 1.0)
                )

    def play_direct(self, data):
        from .logger import log
        log(f"[DEBUG.AUDIO] play_direct received: {data['sound']}")
        self.game.direct_soundgroup.play(
            data["sound"], data["looping"], data["id"], volume=data["volume"], cat=data.get("cat", "miscelaneous")
        )

    def play_unbound(self, data):
        snd = self.game.audio_mngr.play_unbound(
            data["sound"], data["x"], data["y"], data["z"], False, volume=data["volume"], cat=data.get("cat", "miscelaneous")
        )
        if snd and getattr(self, 'gameplay', None) and getattr(self.gameplay, 'map', None):
            reverb = self.gameplay.map.get_reverb_at(data["x"], data["y"], data["z"])
            if reverb and reverb.reverb:
                try:
                    self.game.audio_mngr.efx.send(snd.source, 0, reverb.reverb)
                except Exception:
                    pass

    def set_game_mode(self, data):
        """Receive game mode from server (e.g. 'pong' or 'normal') and update game state."""
        mode = data.get("mode", "normal")
        self.game.pong_mode = (mode == "pong")
        self.game.pong_arcade = data.get("arcade", False)
        self.game.pong_training = data.get("training", False)
        self.game.pong_speed = data.get("speed", 60)
        
        # If entering a competition or training match, forcefully disable music bot broadcast
        if self.game.pong_mode and not self.game.pong_arcade:
            if hasattr(self.gameplay, 'music_bot') and self.gameplay.music_bot:
                if self.gameplay.music_bot.broadcast_enabled:
                    self.gameplay.music_bot.broadcast_enabled = False
                    from .speech import speak
                    if self.game.pong_training:
                        speak("Music broadcast was disabled because you entered a training match.")
                    else:
                        speak("Music broadcast was disabled because you entered a competition match.")

    def move(self, data):
        entity = self.gameplay.map.entities.get(data["name"])
        if not entity and data["name"] == self.gameplay.player.name:
            entity = self.gameplay.player
        if entity:
            if "angle" not in data:
                data["angle"] = 0
            if "mode" not in data:
                data["mode"] = "walk"
            entity.move(
                data["x"], data["y"], data["z"], data["play_sound"], data["mode"]
            )
            entity.face(data["angle"], entity.vfacing, entity.bfacing, force=True)

    def quit(self, data):
        self.game.put(lambda: self.gameplay.quit("quit"))
        speak(data.get("message", "your connection was closed."), True)

    def typing(self, data):
        if options.get("typing") == True:
            speak(data["message"], False)

    def copy(self, data):
        pyperclip.copy(data["data"])
        speak(data.get("message", "Coppied"))

    def make_menu(self, data):
        menu_id = f"{data.get('event', '')}_{data.get('title', '')}"
        is_memory_enabled = data.get("event", "").startswith("weapon_")
        
        if not hasattr(self.game, "menu_memory"):
            self.game.menu_memory = {}

        def on_select(value, close, index):
            if is_memory_enabled:
                self.game.menu_memory[menu_id] = index
            if close:
                self.gameplay.pop_last_substate()
            self.client.send(consts.CHANNEL_MENUS, data["event"], {"value": value})

        def on_close():
            if menu_id in self.game.menu_memory:
                del self.game.menu_memory[menu_id]
            self.gameplay.pop_last_substate()

        m = menu.Menu(self.game, data["title"])
        options = []
        for idx, i in enumerate(data["options"]):
            options.append(
                (i["title"], functools.partial(on_select, i["value"], i["close"], idx), i.get("preview_sound"))
            )
        options.append(("Close", on_close, None))
        m.add_items(options)
        
        if is_memory_enabled:
            saved_pos = self.game.menu_memory.get(menu_id, 0)
            if 0 <= saved_pos < len(m.items):
                m.pos = saved_pos
            else:
                m.pos = -1
        else:
            m.pos = -1
        m.sound_browse_mode = bool(data.get("sound_browse_mode", False))
        m.block_space = data.get("event", "").startswith("builder_")
        # Store menu context so Ctrl+C / Ctrl+V shortcuts know which event and
        # selected value to act on (used by the builder copy/paste clipboard).
        m.menu_event = data.get("event", "")
        m.menu_values = [i["value"] for i in data["options"]]
        menus.set_default_sounds(m)
        self.gameplay.add_substate(m)

    def add_weapon(self, data):
        self.gameplay.wmanager.add(weapon.weapon(self.game, self.gameplay, **data))

    def modify_weapon(self, data):
        self.gameplay.wmanager.modify(data["num"], data["data"])

    def clear_weapons(self, data):
        self.gameplay.wmanager.clear()

    def replace_weapon(self, data):
        self.gameplay.wmanager.replace(
            weapon.weapon(self.game, self.gameplay, **data["weapon_data"]), data["num"]
        )

    def open_rules(self, data):
        webbrowser.open("https://final-hour.net/agreement")

    def death(self, data):  # sourcery skip: avoid-builtin-shadow
        if data["dead"] == True:
            fall_direction = random.randint(1, 2)
            player = self.gameplay.player
            if fall_direction == 1:
                player.face(player.hfacing, -90, random.randint(-45, 45))
                speak("you fall on to your front")
            elif fall_direction == 2:
                player.face(player.hfacing, 90, random.randint(-45, 45))
                speak("you fall on to your back")

            if self.gameplay.wmanager.activeWeapon != None:
                self.gameplay.wmanager.activeWeapon.locked = True
            self.game.direct_soundgroup.play("death/start.ogg", False)
            self.gameplay.player.dead = True
            self.gameplay.camera.move(
                self.gameplay.player.x, self.gameplay.player.y, self.gameplay.player.z
            )
            filter = self.game.audio_mngr.gen_filter("lowpass", ("GAINHF", 1.0))
            self.gameplay.player.death_filter = filter
            for i in self.gameplay.map.get_ambiences_at(
                self.gameplay.player.x, self.gameplay.player.y, self.gameplay.player.z
            ):
                i.leave()

            def automation_death(value):
                filter.set("GAINHF", value)
                self.game.audio_mngr.apply_filter(filter, replace=True)

            self.game.automate(
                None, None, 0.05, 1000, step_callback=automation_death, start_value=1.0
            )
            self.game.direct_soundgroup.play("death/loop.ogg", True, "death", volume=20)
            self.gameplay.player.locked = True
        elif data["dead"] == False:
            self.gameplay.player.face(0, 0, 0)
            if self.gameplay.wmanager.activeWeapon != None:
                self.gameplay.wmanager.activeWeapon.locked = False
            self.gameplay.player.death_filter = None
            for i in self.gameplay.map.get_ambiences_at(
                self.gameplay.player.x, self.gameplay.player.y, self.gameplay.player.z
            ):
                i.enter()
            self.game.audio_mngr.apply_filter(None)
            self.gameplay.player.drown_clock.restart()
            self.gameplay.player.drownable = False

            self.gameplay.player.dead = False
            self.gameplay.camera.move(
                self.gameplay.player.x, self.gameplay.player.y, self.gameplay.player.z
            )
            self.game.direct_soundgroup.play("death/end.ogg", False, "death")
            self.gameplay.player.locked = False

    def set_hp(self, data):
        if self.gameplay.player.lock_weapon:
            return
        self.gameplay.player.hp = data["amount"]

    def open_door(self, data):
        if door := self.gameplay.map.get_door_at(data["x"], data["y"], data["z"]):
            door.switch_state(data["locked"], to_open=True, silent=data["silent"])
        else:
            speak("error opening door")

    def close_door(self, data):
        if door := self.gameplay.map.get_door_at(data["x"], data["y"], data["z"]):
            door.switch_state(data["locked"], to_open=False)
        else:
            speak("error closing door")

    def switch_weapon(self, data):
        self.gameplay.wmanager.switchWeapon(data["slot"])

    def make_input(self, data):
        def online_submit(value):
            self.gameplay.pop_last_substate()
            self.client.send(consts.CHANNEL_MENUS, data["event"], {"value": value, "data": data["data"]})

        stage = data.get("data", {}).get("stage", "")
        input_type = data.get("data", {}).get("type", "")
        msg_length = data.get("data", {}).get("msg_length", 200)
        min_val = data.get("data", {}).get("min_val", None)
        max_val = data.get("data", {}).get("max_val", None)
        
        if input_type in ["createMap", "expandMap"]:
            if stage.endswith('X') or stage.endswith('Y') or stage.endswith('Z'):
                min_val = -999999999
                max_val = 999999999
        elif hasattr(self.gameplay, 'map') and self.gameplay.map:
            if stage.endswith('X'):
                min_val = self.gameplay.map.minx
                max_val = self.gameplay.map.maxx
            elif stage.endswith('Y'):
                min_val = self.gameplay.map.miny
                max_val = self.gameplay.map.maxy
            elif stage.endswith('Z'):
                min_val = self.gameplay.map.minz
                max_val = self.gameplay.map.maxz

        if stage == 'volume':
            min_val, max_val = 0, 100
        elif stage == 'delay':
            min_val, max_val = 0.0, 0.5
        elif stage in ['reverb_decay', 'decayTime']:
            min_val, max_val = 0.1, 20.0
        elif stage in ['reverb_diffusion', 'diffusion']:
            min_val, max_val = 0.0, 1.0
        elif stage in ['price', 'cost', 'weaponCost', 'ammoCost', 'minpoints']:
            min_val, max_val = 0, 999999999

        self.gameplay.add_substate(self.game.input.run(
            data["prompt"], 
            handeler=online_submit, 
            default=data.get("default", ""),
            min_val=min_val,
            max_val=max_val,
            msg_length=msg_length
        ))

    def tickets_menu(self, data):
        if not data:
            return
        self.tickets.view_tickets(data["tickets"])

    def view_closed_tickets(self, data):
        if not data:
            return
        self.tickets.view_tickets(data["tickets"])

    def enter_match(self, data):
        self.gameplay.player.lock_weapon = False
        self.gameplay.game_started = True  # Block PA Test Mode during match
        self.gameplay.pa_test_mode = False  # Disable PA Test Mode if it was on
        
        # Stop any active voice recording and reset to default channel
        if hasattr(self.gameplay, 'voice_chat') and self.gameplay.voice_chat:
            if self.gameplay.voice_chat.recording:
                try:
                    self.gameplay.voice_chat.audio_input.stop()
                    self.gameplay.voice_chat.recording = False
                except Exception:
                    pass
            # Reset vc_compression to default channel
            if hasattr(self.gameplay, '_default_vc_compression'):
                self.gameplay.voice_chat.vc_compression = self.gameplay._default_vc_compression

    def exit_match(self, data):
        self.gameplay.player.lock_weapon = True
        self.gameplay.game_started = False  # Allow PA Test Mode again in exploration

    def login_failed(self, data):
        if not data:
            return
        self.game.pop()
        menus.main_menu(self.game)
        speak(data["message"])


    def double_tap_root_beer(self, data):
        if not data:
            return
        if "value" not in data:
            data["value"] = False
        self.gameplay.player.double_tap_root_beer = data["value"]

    def speed_cola(self, data):
        if not data:
            return
        if "value" not in data:
            data["value"] = False
        self.gameplay.player.speed_cola = data["value"]



    def process_voice_data(self, data, channelID):
        if not options.get("voice_chat", True): return
        if channelID == consts.CHANNEL_MEGAPHONE:
            # Per-player megaphone: first byte = sender's voice_channel ID
            if len(data) < 2: return
            sender_id = data[0]
            opus_data = data[1:]
            if channelID in self.gameplay.voice_channels:
                channel = self.gameplay.voice_channels[channelID]
                # Get or create per-player speaker sources (separate from shared physical speakers)
                player_sources = self.gameplay.get_megaphone_player_sources(sender_id)
                if player_sources:
                    channel.vc_compression.recieve(opus_data, player_sources, None, channelID, self.gameplay, sender_id)
        elif channelID in self.gameplay.voice_channels.keys():
            vc_source = self.gameplay.voice_channels[channelID].vc_source
            radio_source = self.gameplay.voice_channels[channelID].radio_source
            self.gameplay.voice_channels[channelID].vc_compression.recieve(data, vc_source, radio_source, channelID, self.gameplay)

    def process_music_data(self, data):
        # Data format: [1 byte Entity VoiceChannel ID] + [Opus Packet]
        if len(data) < 2: return
        entity_channel_id = data[0]
        opus_data = data[1:]
        
        if entity_channel_id in self.gameplay.voice_channels:
            entity = self.gameplay.voice_channels[entity_channel_id]
            if hasattr(entity, 'music_source'):
                # We need a dedicated decoder and jitter buffer for music per entity
                if not hasattr(entity, 'music_compression') or not entity.music_compression:
                    from .voice_chat import MusicCompression
                    entity.music_compression = MusicCompression(self.game)
                
                entity.music_compression.recieve(opus_data, entity.music_source, None, entity_channel_id, self.gameplay)

    def has_radio(self, data):
        if data["channel"] not in self.gameplay.voice_channels.keys(): return
        self.gameplay.voice_channels[data["channel"]].has_radio = data["enable"]
    
    def has_radio_self(self, data):
        self.gameplay.player.has_radio = data["enable"]
    
    def megaphone_settings_response(self, data):
        """Handle server response for megaphone settings permission"""
        if data.get("allowed", False):
            # Player is builder - open menu
            from . import megaphone_settings
            self.gameplay.push_substate(megaphone_settings.megaphone_settings(self.game, self.gameplay))
        else:
            # Player is not builder - deny access
            speak("You must be a builder to access megaphone settings.")
    
    def open_megaphone_settings(self, data):
        """Open megaphone settings menu (triggered from builder menu)"""
        from . import megaphone_settings
        self.gameplay.add_substate(megaphone_settings.megaphone_settings(self.game, self.gameplay))

    def buffer(self, data):
        """Handle buffer notifications from server (e.g., powerup messages)"""
        buffer.add_item(
            self.game,
            data.get("category", "misc"),
            data["message"],
            sound="",
        )

    def ban(self, data):
        if data["message"]:
            self.game.put(lambda: self.gameplay.quit("quit"))
            speak(data["message"])

    def spectator_join(self, data):
        self.gameplay.spectator_mode = True
        self.gameplay.running = False
        # If spectating a Pong match, the server sends the arena bounds so we can
        # offer sideline camera angles (east/west edge of the field).
        self.gameplay.pong_arena = data.get("pong_arena") if data else None
        # Reset any previous spectator cam mode when (re)entering.
        self.gameplay.camera.reset_spectator_cam_mode()
        # The server already speaks the spectating hint (including the Pong P-key
        # hint when applicable), so the client stays quiet here to avoid overlap.

    def spectator_leave(self, data):
        self.gameplay.spectator_mode = False
        self.gameplay.running = True
        self.gameplay.pong_arena = None
        # Reset sideline cam mode and return camera to local player
        self.gameplay.camera.reset_spectator_cam_mode()
        self.gameplay.camera.set_focus_object(self.gameplay.player)

        speak("You have left spectator mode.")

    def spectator_update(self, data):
        if not self.gameplay.spectator_mode:
            return

        # Pong matches include team names + game mode so the spectator client can
        # announce which team is on which side when parked at a sideline angle.
        # Other game types don't send these, so default to plain labels.
        self.gameplay.pong_team1 = data.get("team1_name", "Team 1")
        self.gameplay.pong_team2 = data.get("team2_name", "Team 2")

        for p_data in data["players"]:
            name = p_data["name"]
            if name == self.gameplay.player.name:
                continue
            
            # Use get to avoid errors if entity not found
            entity = self.gameplay.map.entities.get(name)
            
            # If we are focused on this entity, do we update it?
            # If the server says it moved, we should update it so the camera follows.
            # BUT, if updating it causes a crash (e.g. sound conflict), handle it.
            # Re-enabling updates effectively but with safeguards.
            
            if not entity:
                continue
            if not entity:
                # If entity doesn't exist, we might need to wait for spawn_entity or spawn it?
                # For now, let's assume spawn_entity is handled separately or we skip.
                # Actually, forcing spawn might be good if we entered late.
                # But we lack model info here.
                continue
                
            # Update position directly or via move?
            # Since this is a snapshot, we can use move to interpolate if the client entity supports it.
            # But move expects mode and play_sound.
            
            # Simple position update for now
            # entity.x = p_data["x"]
            # entity.y = p_data["y"]
            # entity.z = p_data["z"]
            
            # Better: use move() without sound
            entity.move(p_data["x"], p_data["y"], p_data["z"], False, "walk")
            
            # Orientation
            if "hfacing" in p_data:
                entity.face(p_data["hfacing"], p_data.get("vfacing", 0), 0)

            # HP update
            entity.hp = p_data["hp"]

    def switch_spectator_target(self, data):
        target_name = data["target"]
        self.gameplay.spectator_target_name = target_name
        target = self.gameplay.map.entities.get(target_name)
        if target:
            target.muted_by_spectator = False
            self.gameplay.camera.set_focus_object(target)
            speak(f"Spectating {target_name}")
            # Ensure audio volume is restored if it was faded?
            try:
                if hasattr(target, 'soundgroup') and target.soundgroup:
                    target.soundgroup.volume = 1.0
            except Exception:
                pass
            try:
                if hasattr(target, 'vc_source') and target.vc_source:
                    target.vc_source.gain = 1.0
            except Exception:
                pass
        else:
            speak(f"Target {target_name} not found")

    def open_language_menu(self, data):
        available_langs = data.get("available_languages", {})
        language_counts = data.get("language_counts", {})
        current = data.get("current_language", "th")
        self.gameplay.show_language_menu(available_langs, language_counts, current)
