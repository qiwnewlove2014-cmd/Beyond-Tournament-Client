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
        self.game.gameplay = self.gameplay
        self.tickets = tickets.Tickets(self.game)

    def create_fail(self, data):
        msg = "Account creation failed. Press Enter to return."
        m = menu.Menu(self.game, "Account Creation")
        m.add_item(msg, lambda: menus.main_menu(self.game))
        m.pos = 0
        self.game.replace(m)
        speak(msg, False)

    def create_done(self, data):
        msg = "Account creation finished. You can now login using the given information. Press Enter to return."
        m = menu.Menu(self.game, "Account Creation")
        m.add_item(msg, lambda: menus.main_menu(self.game))
        m.pos = 0
        self.game.replace(m)
        speak(msg, False)

    def connected(self, data):
        self.game.reconnecting = False
        self.client.put(("connected", True))
        self.game.replace(self.gameplay)
        self.gameplay.player.name = data["username"]
        if hasattr(self.game, 'instance_mngr'):
            self.game.instance_mngr.set_character(data["username"])
        
        self.game.available_languages = data.get("available_languages", {})
        self.game.current_language = data.get("current_language", "th")
        self.game.presence_sounds.configure(data)

        # Store staff status for PA Test Mode (with safe fallback)
        try:
            self.gameplay.is_staff = bool(data.get("is_staff", False))
            self.gameplay.is_builder = bool(data.get("is_builder", False))
            self.gameplay.is_technician = bool(data.get("is_technician", False))
            # Server is the single source of truth for megaphone broadcast permission.
            # canBroadcastMegaphone() also gates the spectator lock; mirroring it client-side
            # keeps the menu and the server lock perfectly in sync.
            self.gameplay.can_broadcast_megaphone = bool(data.get("can_broadcast_megaphone", False))
        except Exception:
            self.gameplay.is_staff = False
            self.gameplay.is_builder = False
            self.gameplay.is_technician = False
            self.gameplay.can_broadcast_megaphone = False
            
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
            
        # Do not clear voice_channels here. The server constructs the player and
        # sends map/player spawn packets on CHANNEL_MAP before this connected
        # event on CHANNEL_MISC. ENet orders each channel independently, so the
        # current connection's mappings may already be present. A reconnect gets
        # a fresh EventHandeler/Gameplay instance and cannot inherit this dict.
        if hasattr(self.gameplay, 'megaphone') and self.gameplay.megaphone:
             self.gameplay.megaphone.setup_megaphone_speakers(force=True)

        # Crash reports are sent only after this connection has authenticated.
        # They stay on disk until the server acknowledges each report.
        try:
            from . import crash_reporting
            crash_reporting.send_pending(self.game)
        except Exception:
            pass
            
        speak("Welcome. You are now online")

    def client_crash_report_ack(self, data):
        from . import crash_reporting
        crash_reporting.acknowledge(data.get("id"))

    def speak(self, data):
        text = data.get("text", "")
        if text:
            if not hasattr(self.game, "match_history"):
                self.game.match_history = []
            self.game.match_history.append(text)
            if len(self.game.match_history) > 50:
                self.game.match_history.pop(0)

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
        )
        self.game.presence_sounds.notify("online", data.get("presence_sound_id", ""))

    def offline(self, data):
        buffer.add_item(
            self.game,
            "players",
            f'{data["username"]} went offline.',
        )
        self.game.presence_sounds.notify("offline", data.get("presence_sound_id", ""))

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

    def _reset_instruments_for_map_change(self):
        """Queue piano/drum cleanup onto the main thread during a map change.

        parse_map/update_map run on the network thread; the reset methods touch
        OpenAL (stop sources, detach EFX sends) which is only safe on the main
        thread where the context is current. The reset_for_map_change variants
        preserve the gameplay back-reference because Gameplay keeps running on
        the new map.
        """
        audio_mngr = getattr(self.game, 'audio_mngr', None)
        if audio_mngr is None:
            return
        def _do_reset():
            if hasattr(audio_mngr, 'piano') and audio_mngr.piano is not None:
                audio_mngr.piano.reset_for_map_change()
            if hasattr(audio_mngr, 'drums') and audio_mngr.drums is not None:
                audio_mngr.drums.reset_for_map_change()
        self.game.put(_do_reset)

    def parse_map(self, data):
        self.game.automations.clear()
        self.game.audio_mngr.apply_filter(
            None, exclude=self.game.exclude_water, clear=True
        )
        # A full map load destroys every entity referenced by this lookup.
        # Clear it here (on CHANNEL_MAP) so subsequent spawn_entity packets on
        # that same ordered channel rebuild only valid mappings for the new map.
        self.gameplay.voice_channels.clear()
        # Release preloaded instrument buffers and live voices from the previous
        # map so memory does not accumulate. Must run on the main thread because
        # reset() touches OpenAL sources/filters.
        self._reset_instruments_for_map_change()
        self.gameplay.parser.load(data["data"])
        raw_x = data.get("x")
        raw_y = data.get("y")
        raw_z = data.get("z")
        x = float(raw_x) if raw_x is not None else 0.0
        y = float(raw_y) if raw_y is not None else 0.0
        z = float(raw_z) if raw_z is not None else 0.0
        self.gameplay.player.move(x, y, z, play_sound=False)
        # Setup megaphone speakers after map data is loaded (with safety check)
        if hasattr(self.gameplay, 'megaphone') and self.gameplay.megaphone:
            self.gameplay.megaphone.setup_megaphone_speakers(force=True)
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
        # Same instrument cleanup as parse_map: a builder edit reloads the map
        # and would otherwise keep stale buffers around.
        self._reset_instruments_for_map_change()
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
        if hasattr(self.gameplay, 'megaphone') and self.gameplay.megaphone:
            self.gameplay.megaphone.setup_megaphone_speakers(force=True)
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
            self.gameplay.megaphone.setup_megaphone_speakers(force=True)

    def spawn_entity(self, data):
        from .logger import log, log_exception
        if not isinstance(data, dict) or not data.get("name"):
            log("[ENTITY] Ignored spawn_entity packet without a valid name")
            return
        if (
            data.get("type") in ("motorcycle", "vehicle")
            and not data.get("_vehicle_main_thread")
            and not data.get("_motorcycle_main_thread")
        ):
            spawn_data = dict(data)
            spawn_data["_vehicle_main_thread"] = True
            self.game.put(
                lambda spawn_data=spawn_data: self.spawn_entity(spawn_data)
            )
            return
        raw_x = data.get("x")
        raw_y = data.get("y")
        raw_z = data.get("z")
        x = float(raw_x) if raw_x is not None else 0.0
        y = float(raw_y) if raw_y is not None else 0.0
        z = float(raw_z) if raw_z is not None else 0.0
        existing = self.gameplay.map.entities.get(data["name"])
        was_camera_focus = (
            getattr(self.gameplay.camera, "focus_object", None) is existing
        )
        try:
            entity = self.gameplay.map.spawn_entity(
                data["name"],
                x,
                y,
                z,
                entity_type=data.get("type"),
                vehicle_type=data.get("vehicle_type"),
                sound_profile=data.get("sound_profile"),
                vehicle_audio=data.get("vehicle_audio"),
            )
        except Exception as e:
            log_exception(e, f"spawn_entity name={data['name']!r}")
            return
        if was_camera_focus:
            # A resync can replace an entity with the same name.  Never leave the
            # spectator camera bound to the just-destroyed entity/audio sources.
            self.gameplay.camera.set_focus_object(entity)
        log(f"[ENTITY] Spawned {data['name']!r} at ({x}, {y}, {z})")
        if data.get("voice_channel", None) != None:
            if not hasattr(self.gameplay, 'voice_channels'):
                self.gameplay.voice_channels = {}
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

        if getattr(entity, "is_vehicle", False):
            state_data = dict(data)
            state_data["_initial_spawn"] = True
            self.game.put(
                lambda state_data=state_data: self._apply_vehicle_state(state_data)
            )

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
        target_name = data.get("name")
        target_entity = self.gameplay.map.entities.get(target_name)
        if (getattr(target_entity, "is_vehicle", False) and
                not data.get("_vehicle_main_thread") and
                not data.get("_motorcycle_main_thread")):
            remove_data = dict(data)
            remove_data["_vehicle_main_thread"] = True
            self.game.put(
                lambda remove_data=remove_data: self.remove_entity(remove_data)
            )
            return
        if target_name is not None:
            piano = self.game.audio_mngr.piano
            if (
                str(target_name) in piano.chorus_states
                or str(target_name) in piano._chorus_slots
            ):
                self.game.put(
                    lambda target_name=target_name: (
                        self.game.audio_mngr.piano.remove_peer(target_name)
                    )
                )
            self.game.put(
                lambda target_name=target_name: (
                    self.game.audio_mngr.drums.remove_peer(target_name)
                )
            )
        if hasattr(self.gameplay, 'voice_channels') and isinstance(self.gameplay.voice_channels, dict):
            keys_to_remove = [k for k, v in self.gameplay.voice_channels.items() if getattr(v, 'name', None) == target_name]
            for k in keys_to_remove:
                del self.gameplay.voice_channels[k]
        if hasattr(self.gameplay, 'map') and self.gameplay.map:
            self.gameplay.map.remove_entity(data["name"])

    def play_sound(self, data):
        entity = (
            self.gameplay.player
            if data["name"] == self.gameplay.player.name
            else self.gameplay.map.entities.get(data["name"])
        )
        if (getattr(entity, "is_vehicle", False) and
                not data.get("_vehicle_main_thread") and
                not data.get("_motorcycle_main_thread")):
            sound_data = dict(data)
            sound_data["_vehicle_main_thread"] = True
            self.game.put(
                lambda sound_data=sound_data: self.play_sound(sound_data)
            )
            return
        if entity:
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
        occluded = False
        if data.get("is_stereo_spatial") and getattr(self, 'gameplay', None) and getattr(self.gameplay, 'player', None):
            # Piano notes go through the main-thread queue (piano_note field present).
            # OpenAL context is only current on the main thread, so we MUST NOT play
            # piano audio from this network-thread handler.
            if data.get("piano_note"):
                self.game.audio_mngr.piano.enqueue_remote_note(data)
                return
            lx, ly, lz = self.gameplay.player.x, self.gameplay.player.y, self.gameplay.player.z
            facing = getattr(self.gameplay.player, 'facing', 0.0)

            if getattr(self.gameplay, 'map', None):
                with contextlib.suppress(Exception):
                    los = self.gameplay.map.valid_straight_path((data["x"], data["y"], data["z"]), (lx, ly, lz))
                    if los is False:
                        occluded = True

            snd = self.game.audio_mngr.play_unbound_stereo_spatial(
                data["sound"], data["x"], data["y"], data["z"], lx, ly, lz,
                volume=data.get("volume", 300), cat=data.get("cat", "miscelaneous"),
                max_distance=data.get("max_distance", 25.0),
                facing_angle=facing,
                as_3d_stereo=False,
                occluded=occluded,
            )
        else:
            snd = self.game.audio_mngr.play_unbound(
                data["sound"], data["x"], data["y"], data["z"], False, volume=data.get("volume", 300), cat=data.get("cat", "miscelaneous"),
                reference_distance=data.get("reference_distance", 3.0), rolloff=data.get("rolloff", 1.0), max_distance=data.get("max_distance", 25.0)
            )
        if snd and getattr(self, 'gameplay', None) and getattr(self.gameplay, 'map', None):
            reverb = self.gameplay.map.get_reverb_at(data["x"], data["y"], data["z"])
            if reverb and reverb.reverb:
                s_list = snd if isinstance(snd, (list, tuple)) else [snd]
                for s in s_list:
                    if s and hasattr(s, 'source') and s.source:
                        with contextlib.suppress(Exception):
                            self.game.audio_mngr.efx.send(s.source, 0, reverb.reverb)

    def play_piano_note(self, data):
        """Queue a remote piano note for main-thread playback.

        Never touch OpenAL from the network thread: the OpenAL context is only
        current on the main thread. enqueue_remote_note validates and copies the
        packet; PianoAudio.update() drains and plays on the main thread.
        """
        self.game.audio_mngr.piano.enqueue_remote_note(data)

    def stop_piano_note(self, data):
        """Queue a remote piano note-off for main-thread processing."""
        self.game.audio_mngr.piano.enqueue_remote_stop(data)

    def set_piano_soft_pedal(self, data):
        """Apply the server-replicated realtime pedal state for one performer."""
        if data and data.get("peer_id") is not None and isinstance(data.get("enabled"), bool):
            peer_id = data["peer_id"]
            enabled = data["enabled"]
            self.game.put(
                lambda peer_id=peer_id, enabled=enabled: (
                    self.game.audio_mngr.piano.set_soft_pedal(peer_id, enabled)
                )
            )

    def set_piano_chorus(self, data):
        """Queue a server-replicated Chorus toggle onto the main thread."""
        if not data or data.get("peer_id") is None:
            return
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return
        peer_id = data["peer_id"]
        self.game.put(
            lambda peer_id=peer_id, enabled=enabled: (
                self.game.audio_mngr.piano.set_chorus(peer_id, enabled)
            )
        )

    def set_piano_pitch_bend(self, data):
        """Apply a server-validated continuous or legacy pitch bend state.

        Queued to the main thread because set_pitch_bend* mutates OpenAL source
        pitch and transition state that update() iterates on the main thread.
        """
        if not data or data.get("peer_id") is None:
            return
        peer_id = data["peer_id"]
        if "value" in data:
            value = data.get("value")
            self.game.put(
                lambda peer_id=peer_id, value=value: (
                    self.game.audio_mngr.piano.set_pitch_bend_14bit(peer_id, value, animate=True)
                )
            )
            return
        direction = data.get("direction")
        if not isinstance(direction, bool) and direction in (-1, 0, 1):
            self.game.put(
                lambda peer_id=peer_id, direction=direction: (
                    self.game.audio_mngr.piano.set_pitch_bend(peer_id, direction)
                )
            )

    def piano_start(self, data):
        """Enable piano mode to intercept keyboard input for playing piano notes and pre-load audio buffers strongly into RAM."""
        # The packet handler runs on the network thread. Queue all piano input
        # and audio initialization onto the main thread that owns those states.
        self.game.put(self.gameplay._start_piano_session)
        # Preload piano buffers on the main thread too: load_buffer reaches OpenAL
        # (context.genBuffer + set_data), which is unsafe on the network thread.
        def _preload_piano_buffers():
            notes = ["C4", "Db4", "D4", "Eb4", "E4", "F4", "Gb4", "G4", "Ab4", "A4", "Bb4", "B4", "C5", "Db5", "D5", "Eb5", "E5", "F5"]
            for note in notes:
                snd = f"piano/Piano.mf.{note}.ogg"
                snd_path = os.path.join(consts.SOUNDPREPEND, snd)
                try:
                    rel_snd = os.path.relpath(snd_path)
                except ValueError:
                    rel_snd = os.path.normpath(snd_path)
                try:
                    buf = self.game.audio_mngr.load_buffer(snd)
                    if buf:
                        self.game.audio_mngr._preloaded_buffers[rel_snd] = buf
                except Exception:
                    pass
        self.game.put(_preload_piano_buffers)

    def drum_start(self, data):
        """Enter drum mode and preload the requested kit on the main thread."""
        kit = data.get("kit") if isinstance(data, dict) else None
        self.game.put(lambda: self.gameplay._start_drum_session(kit=kit))

    def play_drum_hit(self, data):
        """Queue a validated remote one-shot for main-thread audio playback."""
        self.game.audio_mngr.drums.enqueue_remote_hit(data)

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
        from .logger import log, log_exception
        if not isinstance(data, dict):
            log("[ENTITY] Ignored malformed move packet")
            return
        name = data.get("name")
        if not name:
            log("[ENTITY] Ignored move packet without a name")
            return
        entity = self.gameplay.map.entities.get(name)
        if not entity and name == self.gameplay.player.name:
            entity = self.gameplay.player
        if entity:
            if getattr(entity, "is_vehicle", False):
                move_data = dict(data)
                self.game.put(
                    lambda move_data=move_data: self._apply_vehicle_move(move_data)
                )
                return
            try:
                entity.move(
                    data.get("x"), data.get("y"), data.get("z"),
                    bool(data.get("play_sound", False)), data.get("mode", "walk")
                )
                entity.face(data.get("angle", 0), entity.vfacing, entity.bfacing, force=True)
            except Exception as e:
                log_exception(e, f"move name={name!r} data={data!r}")
        else:
            log(f"[ENTITY] Ignored move for unknown entity {name!r}")

    def _apply_vehicle_move(self, data):
        entity = self.gameplay.map.entities.get(data.get("name"))
        if not entity or not getattr(entity, "is_vehicle", False):
            return
        entity.move(
            data.get("x"), data.get("y"), data.get("z"),
            False, "vehicle"
        )
        entity.apply_state(
            data.get("vehicle_speed", 0.0),
            data.get("engine_on", False),
            data.get("rider", ""),
            data.get("vehicle_facing", data.get("angle", 0.0)),
        )

    def _apply_vehicle_state(self, data):
        entity = self.gameplay.map.entities.get(data.get("name"))
        if not entity or not getattr(entity, "is_vehicle", False):
            return
        entity.apply_state(
            data.get("vehicle_speed", 0.0),
            data.get("engine_on", False),
            data.get("rider", ""),
            data.get("vehicle_facing", 0.0),
            bool(data.get("_initial_spawn", False)),
        )

    def vehicle_state(self, data):
        if not isinstance(data, dict):
            return
        state_data = dict(data)
        self.game.put(
            lambda state_data=state_data: self._apply_vehicle_state(state_data)
        )

    def motorcycle_state(self, data):
        self.vehicle_state(data)

    def vehicle_session(self, data):
        if not isinstance(data, dict):
            return
        session_data = dict(data)
        self.game.put(
            lambda session_data=session_data: self.gameplay.set_vehicle_session(session_data)
        )

    def motorcycle_session(self, data):
        if not isinstance(data, dict):
            return
        session_data = dict(data)
        session_data.setdefault("vehicle_type", "motorcycle")
        self.vehicle_session(session_data)

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

        # Pop previous builder menu substate if open so builder menus swap cleanly instead of stacking
        event_name = data.get("event", "")
        is_builder_menu = event_name.startswith("builder_") or event_name.startswith("edit_") or event_name == "element_action_select"
        if is_builder_menu and self.gameplay and getattr(self.gameplay, "states", None):
            top_state = self.gameplay.states[-1]
            top_event = getattr(top_state, "menu_event", "")
            if top_event.startswith("builder_") or top_event.startswith("edit_") or top_event == "element_action_select":
                self.gameplay.pop_last_substate()

        m = menu.Menu(self.game, data["title"], autoclose=False, parrent=self.gameplay)
        m.menu_event = data.get("event", "")
        m.menu_type = data.get("menu_type", "normal")
        options = []
        for idx, i in enumerate(data["options"]):
            options.append(
                (i["title"], functools.partial(on_select, i["value"], i["close"], idx), i.get("preview_sound"))
            )
        has_server_back = False
        if data.get("options"):
            for opt in data["options"]:
                opt_title = str(opt.get("title", "")).lower()
                opt_val = opt.get("value")
                if opt_title == "back" or opt_title.startswith("back "):
                    has_server_back = True
                    break
                if isinstance(opt_val, dict):
                    act = str(opt_val.get("action", ""))
                    prop = str(opt_val.get("property", ""))
                    if act in ("back", "builder_back", "bj_prompt_exit", "exit_blackjack", "back_main", "weapon_back") or prop == "back" or act.endswith("_back"):
                        has_server_back = True
                        break
                elif str(opt_val) in ("back", "builder_back", "bj_prompt_exit", "exit_blackjack", "back_main", "weapon_back") or str(opt_val).endswith("_back"):
                    has_server_back = True
                    break

        if not has_server_back:
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

        # Focus option 0 by default for active match/lobby turn menus so Enter works immediately
        if m.menu_type in ("match_play", "match_control") and m.pos == -1 and len(m.items) > 0:
            m.pos = 0

        m.sound_browse_mode = bool(data.get("sound_browse_mode", False))
        m.block_space = data.get("event", "").startswith("builder_")
        # Store menu context so Ctrl+C / Ctrl+V shortcuts know which event and
        # selected value to act on (used by the builder copy/paste clipboard).
        m.menu_event = data.get("event", "")
        m.menu_values = [i["value"] for i in data["options"]]
        menus.set_default_sounds(m)
        if m.menu_type in ("match_play", "match_control"):
            self.gameplay.in_minigame_match = True
            # If current top substate is already a match menu, replace it to prevent substate stacking
            if self.gameplay.substates and getattr(self.gameplay.substates[-1], "menu_type", "normal") in ("match_play", "match_control"):
                self.gameplay.replace_last_substate(m)
            else:
                self.gameplay.add_substate(m)
        else:
            self.gameplay.add_substate(m)

    def close_input(self, data):
        if getattr(self, "gameplay", None):
            self.gameplay.pop_last_substate()
            remaining_match = any(
                getattr(sub, "menu_type", "normal") in ("match_play", "match_control")
                for sub in self.gameplay.substates
            )
            if not remaining_match:
                self.gameplay.in_minigame_match = False

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
            if getattr(self.gameplay, "drum_mode", False):
                self.game.put(functools.partial(
                    self.gameplay._end_drum_session,
                    notify_server=False,
                ))
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

        data_obj = data.get("data")
        if not isinstance(data_obj, dict):
            data_obj = {}
        stage = data_obj.get("stage", "")
        input_type = data_obj.get("type", "")
        msg_length = data_obj.get("msg_length", 5000 if input_type == 'zone' or stage == 'text' else 200)
        min_val = data_obj.get("min_val", None)
        max_val = data_obj.get("max_val", None)
        
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
        self.tickets.view_tickets(
            data.get("tickets", []),
            reviewer=data.get("reviewer", data.get("moderator", False)),
        )

    def view_closed_tickets(self, data):
        if not data:
            return
        self.tickets.view_tickets(
            data.get("tickets", []),
            reviewer=data.get("reviewer", data.get("moderator", False)),
        )

    def feedback_home(self, data):
        self.tickets.show_home(reviewer=bool((data or {}).get("reviewer", False)))

    def feedback_list(self, data):
        if not data:
            return
        self.tickets.show_list(
            data.get("tickets", []),
            reviewer=bool(data.get("reviewer", False)),
            scope=data.get("scope", "own"),
            can_permanently_delete=bool(data.get("can_permanently_delete", False)),
        )

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
        msg = f"{data['message']} Press Enter to return."
        m = menu.Menu(self.game, "Login Failed")
        m.add_item(msg, lambda: menus.main_menu(self.game))
        m.pos = 0
        self.game.replace(m)
        speak(msg, False)


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
                player_sources = self.gameplay.megaphone.get_megaphone_player_sources(sender_id)
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
            music_src = getattr(entity, 'music_source', None)
            if music_src is not None:
                import time

                if not hasattr(entity, 'music_compression') or not entity.music_compression:
                    from .voice_chat import MusicCompression
                    entity.music_compression = MusicCompression(self.game)

                # Stamp the last receive time so entity.loop() can avoid pushing
                # silent keep-alive buffers that would interleave with real audio.
                entity._music_last_recv = time.time()
                try:
                    entity.music_compression.recieve(opus_data, music_src, None, entity_channel_id, self.gameplay)
                except Exception as e:
                    pass

    def has_radio(self, data):
        if not hasattr(self.gameplay, 'voice_channels') or not isinstance(self.gameplay.voice_channels, dict):
            return
        if data["channel"] not in self.gameplay.voice_channels.keys(): return
        self.gameplay.voice_channels[data["channel"]].has_radio = data["enable"]
    
    def has_radio_self(self, data):
        self.gameplay.player.has_radio = data["enable"]
    
    def megaphone_settings_response(self, data):
        """Handle server response for megaphone settings permission"""
        if data.get("allowed", False):
            # Player is builder/technician - open menu
            from . import megaphone_settings
            self.gameplay.push_substate(megaphone_settings.megaphone_settings(self.game, self.gameplay))
        else:
            # Player is not builder/technician - deny access
            speak("You must be a builder or sound technician to access megaphone settings.")
    
    def open_megaphone_settings(self, data):
        """Open megaphone settings menu (triggered from builder menu)"""
        from . import megaphone_settings
        self.gameplay.add_substate(megaphone_settings.megaphone_settings(self.game, self.gameplay))

    def megaphone_lock_state(self, data):
        """Handle megaphone lock state broadcasts from server"""
        self.gameplay.megaphone.lock_owner = data.get("owner")

    def request_scandir(self, data):
        """Scan client's local asset directory and return file/folder items to server"""
        speak("Requesting directory scan: " + str(data.get("path", "")), False)
        rel_path = data.get("path", "")
        category = data.get("category", "")
        
        import os
        base_dir = os.path.abspath(consts.SOUNDPREPEND.rstrip('/\\'))
        
        # Calculate full absolute path safely
        target_dir = os.path.abspath(os.path.join(base_dir, rel_path))
        
        # Security validation: Ensure target_dir is strictly inside base_dir
        if not target_dir.lower().startswith(base_dir.lower()):
            self.client.send(
                consts.CHANNEL_MISC,
                "scandir_response",
                {"success": False, "error": "Access Denied", "category": category, "path": rel_path}
            )
            return
            
        items = []
        try:
            if os.path.exists(target_dir):
                for entry in os.scandir(target_dir):
                    if entry.name.startswith("."):
                        continue
                    items.append({
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "is_file": entry.is_file()
                    })
            success = True
            error = ""
        except Exception as e:
            success = False
            error = str(e)
            
        response_data = {
            "success": success,
            "error": error,
            "items": items,
            "category": category,
            "path": rel_path
        }
        # Forward any extra keys (like perkName)
        for k, v in data.items():
            if k not in response_data:
                response_data[k] = v
                
        self.client.send(
            consts.CHANNEL_MISC,
            "scandir_response",
            response_data
        )

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
        from .logger import log, log_exception
        if not self.gameplay.spectator_mode:
            return
        if not isinstance(data, dict) or not isinstance(data.get("players"), list):
            log("[ENTITY] Ignored malformed spectator_update packet")
            return

        # Pong matches include team names + game mode so the spectator client can
        # announce which team is on which side when parked at a sideline angle.
        # Other game types don't send these, so default to plain labels.
        self.gameplay.pong_team1 = data.get("team1_name", "Team 1")
        self.gameplay.pong_team2 = data.get("team2_name", "Team 2")

        for p_data in data["players"]:
            if not isinstance(p_data, dict) or not p_data.get("name"):
                log("[ENTITY] Ignored malformed player entry in spectator_update")
                continue
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
                log(f"[ENTITY] Spectator snapshot arrived before spawn for {name!r}")
                continue
            try:
                # Snapshots are high-frequency state syncs, not gameplay movement.
                # Do not enter Entity.move(): it reallocates/touches EFX, SoundGroup
                # and voice sources, which previously caused native audio crashes.
                entity.sync_network_position(
                    p_data.get("x"), p_data.get("y"), p_data.get("z")
                )
                if "hfacing" in p_data:
                    entity.face(p_data["hfacing"], p_data.get("vfacing", 0), 0)
                if "hp" in p_data:
                    entity.hp = p_data["hp"]
            except Exception as e:
                log_exception(e, f"spectator_update name={name!r} data={p_data!r}")

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

    def shield_hit(self, data):
        x = data.get("x")
        y = data.get("y")
        z = data.get("z")
        if hasattr(self.gameplay, 'shield_mngr'):
            self.gameplay.shield_mngr.play_impact_sound(x, y, z, is_local=(x is None))

    def shield_break(self, data):
        x = data.get("x")
        y = data.get("y")
        z = data.get("z")
        if hasattr(self.gameplay, 'shield_mngr'):
            self.gameplay.shield_mngr.play_break_sound(x, y, z, is_local=(x is None))

    def equip_shield(self, data):
        if hasattr(self.gameplay, 'shield_mngr'):
            self.gameplay.shield_mngr.equip_shield(data)

    def unequip_shield(self, data):
        if hasattr(self.gameplay, 'shield_mngr'):
            self.gameplay.shield_mngr.unequip_shield()
