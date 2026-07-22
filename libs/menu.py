import contextlib

import pygame as pg

from . import state
from . import globals as g
from . import options, speech, consts


class Menu(state.State):
    def __init__(
        self,
        game,
        title: str,
        wrapping=False,
        up_down=True,
        left_right=False,
        autoclose=False,
        parrent=None,
    ):
        super().__init__(game, parrent=parrent)
        self.direct_soundgroup = self.game.direct_soundgroup
        self.title = title
        self.autoclose = autoclose
        self.wrapping = wrapping
        self.up_down = up_down
        self.left_right = left_right
        self.items = []
        self.pos = -1

        # sound filepaths.
        self.click = ""
        self.close = ""
        self.edge = ""
        self.enter_sound = ""
        self.open = ""
        self.wrap = ""
        self.music = ""
        self.mus=None
        self.music_volume = None
        self.preview_volume = 100
        self.sound_browse_mode = False
        self.block_space = False
        # Menu context for builder copy/paste shortcuts (set by make_menu).
        self.menu_event = ""
        self.menu_values = []
        self.in_history_mode = False
        self.history_pos = 0

    def speak_history_item(self):
        history_items = getattr(self.game, "match_history", [])
        if not history_items:
            speech.speak("No match history recorded yet.", id="menu_history")
            return
        if self.history_pos < 0:
            self.history_pos = 0
        elif self.history_pos >= len(history_items):
            self.history_pos = len(history_items) - 1

        item_text = history_items[self.history_pos]
        speech.speak(f"{self.history_pos + 1} of {len(history_items)}: {item_text}", id="menu_history")

    def stop_preview_sound(self):
        if "menu_preview" in self.direct_soundgroup.labeled_sources:
            snd = self.direct_soundgroup.labeled_sources.pop("menu_preview")
            if snd and snd.source:
                try:
                    self.game.automate(
                        snd.source, "gain", 0.0, 300,
                        callback=snd.destroy, cancelable=False
                    )
                except Exception:
                    snd.source.stop()
                    snd.destroy()


    def toggle_preview(self):
        """If preview is playing, fade it out. If not playing, replay current item."""
        snd = self.direct_soundgroup.labeled_sources.get("menu_preview")
        if snd and snd.source and snd.source.state.name != "STOPPED":
            self.stop_preview_sound()
            speech.speak("Stopped", id="preview_toggle")
        else:
            if self.pos >= 0:
                self.speak_current_item()

    def return_first_match(self, text, current_index=0):
        """return the first index that has an item that matches the text"""
        # first, check if there are any items after current_index
        for i in self.items[current_index + 1 :]:
            item_name = (i[0]() if callable(i[0]) else i[0]).lower()
            if item_name.startswith(text.lower()):
                return self.items.index(i)
        # no matching item after current_index, lets check the items above it.
        for i in self.items[:current_index]:
            item_name = (i[0]() if callable(i[0]) else i[0]).lower()
            if item_name.startswith(text.lower()):
                return self.items.index(i)
        # no matching item at all, so just return current_index
        return current_index

    def enter(self):
        super().enter()
        speech.speak(self.title, id="menu_title")
        if self.open:
            self.direct_soundgroup.play(self.open, cat="ui")

    def add_item(self, title, action=None, preview_sound=None):
        if action is None:
            action = lambda: None
        self.items.append((title, action, preview_sound))

    def add_items(self, items: list[tuple]):
        """
        adds the items given in a list of items. each item is a tuple of (str|callable, callable)
        if [0] is a callable it will use its return value to speak its label. when enter is pressed, calls [1]
        """
        for i in items:
            self.items.append(i)

    def set_sounds(self, click="", close="", edge="", enter="", open="", wrap=""):
        self.click = click
        self.close = close
        self.edge = edge
        self.enter_sound = enter
        self.open = open
        self.wrap = wrap

    def set_music(self, music_path: str, gain=None):
        if gain is None:
            gain = options.get("menu_music_volume", 50)
        self.music = music_path

        # Check if the game already has this music playing!
        current_music = getattr(self.game, "_active_menu_music", None)
        if current_music and current_music.get("path") == music_path:
            self.mus = current_music["sound"]
            if self.mus and self.mus.source is not None:
                self.mus.source.gain = gain / 100
            self.mus.volume = gain
            self.music_volume = gain
            current_music["kept"] = True
            return

        if current_music:
            old_mus = current_music.get("sound")
            if old_mus:
                old_mus.destroy()
            self.game._active_menu_music = None

        mus = self.game.audio_mngr.create_soundgroup(direct=True)
        self.mus = mus.play(self.music, looping=True, cat="music")
        if self.mus is None:
            return
        if self.mus.source is not None: self.mus.source.gain = gain / 100
        self.mus.volume = gain
        self.music_volume = gain

        self.game._active_menu_music = {
            "path": music_path,
            "sound": self.mus,
            "soundgroup": mus,
            "kept": False
        }

    def speak_current_item(self):
        item = self.items[self.pos]
        text = item[0]
        if callable(text):
            text = text()
        speech.speak(text, id="menu_item")
        
        if len(item) > 2 and item[2]:
            self.direct_soundgroup.play(item[2], cat="ui", id="menu_preview", volume=self.preview_volume)

    def update(self, events):
        super().update(events)
        for event in events:
            if event.type == pg.KEYUP:
                target_gp = getattr(self, "parrent", None) or getattr(self.game, "gameplay", None)
                if target_gp and hasattr(target_gp, "voice_chat_stop"):
                    if event.key == self.game.keyconfig.get("voice_chat", pg.K_g):
                        target_gp.voice_chat_stop(getattr(event, "mod", 0))
                continue

            if event.type == pg.KEYDOWN:
                key = event.key

                is_minigame_match = getattr(self, "menu_type", "normal") in ("match_play", "match_control")

                # Allow chat, voice chat, and buffer reading keys whenever attached to parent Gameplay
                target_gp = getattr(self, "parrent", None) or getattr(self.game, "gameplay", None)
                if target_gp and hasattr(target_gp, "chat"):
                    if key == pg.K_QUOTE:
                        target_gp.chat(event.mod)
                        continue
                    elif key == pg.K_SLASH:
                        target_gp.map_chat(event.mod)
                        continue
                    elif key == pg.K_COMMA:
                        target_gp.buffer_move_l(event.mod)
                        continue
                    elif key == pg.K_PERIOD:
                        target_gp.buffer_move_r(event.mod)
                        continue
                    elif key == pg.K_LEFTBRACKET:
                        target_gp.buffer_cycle_l(event.mod)
                        continue
                    elif key == pg.K_RIGHTBRACKET:
                        target_gp.buffer_cycle_r(event.mod)
                        continue
                    elif key == self.game.keyconfig.get("voice_chat", pg.K_g):
                        target_gp.voice_chat_start(event.mod)
                        continue

                # Tab / Shift+Tab Dual-Pane Toggle (Match History vs Card Actions) - ONLY in minigame matches!
                if key == pg.K_TAB and is_minigame_match:
                    history_items = getattr(self.game, "match_history", [])
                    if not history_items:
                        speech.speak("No match history recorded yet.", True)
                    else:
                        self.in_history_mode = not self.in_history_mode
                        if self.in_history_mode:
                            self.history_pos = len(history_items) - 1
                            speech.speak("Match History. Press Up or Down to review. Press Tab for Card Actions.", True)
                            self.speak_history_item()
                        else:
                            speech.speak("Card Actions.", True)
                            self.speak_current_item()
                    continue

                if self.in_history_mode and is_minigame_match:
                    history_items = getattr(self.game, "match_history", [])
                    if key == pg.K_DOWN:
                        if self.history_pos > 0:
                            self.history_pos -= 1
                            self.speak_history_item()
                        else:
                            if self.edge != "":
                                self.direct_soundgroup.play(self.edge, cat="ui")
                    elif key == pg.K_UP:
                        if self.history_pos < len(history_items) - 1:
                            self.history_pos += 1
                            self.speak_history_item()
                        else:
                            if self.edge != "":
                                self.direct_soundgroup.play(self.edge, cat="ui")
                    elif key in (pg.K_RETURN, pg.K_SPACE):
                        self.speak_history_item()
                    elif key == pg.K_HOME:
                        self.history_pos = len(history_items) - 1
                        self.speak_history_item()
                    elif key == pg.K_END:
                        self.history_pos = 0
                        self.speak_history_item()
                    elif key == pg.K_ESCAPE:
                        self.in_history_mode = False
                        speech.speak("Card Actions.", True)
                        self.speak_current_item()
                    continue

                if self.up_down and key == pg.K_DOWN:
                    self.move_down()

                elif self.up_down and key == pg.K_UP:
                    self.move_up()

                elif self.up_down and key == pg.K_END:
                    self.move_end()

                elif self.up_down and key == pg.K_HOME:
                    self.move_top()

                elif key == pg.K_RETURN:
                    if self.pos != -1:
                        self.select_current_item()
                elif key == pg.K_c and event.mod & pg.KMOD_CTRL:
                    # Builder copy: only meaningful in the element list menu
                    if self.menu_event == "edit_element_select" and 0 <= self.pos < len(self.menu_values):
                        value = self.menu_values[self.pos]
                        self.game.network.send(consts.CHANNEL_MENUS, "builder_copy_element", {"value": value})
                elif key == pg.K_v and event.mod & pg.KMOD_CTRL:
                    # Builder paste: only meaningful in the main builder menu
                    if self.menu_event == "builder_menu_select":
                        self.game.network.send(consts.CHANNEL_MENUS, "builder_paste_element", {"value": "paste"})
                elif key == pg.K_SPACE:
                    if self.sound_browse_mode:
                        self.toggle_preview()
                    elif not self.block_space and self.pos != -1:
                        self.select_current_item()

                elif key in (pg.K_BACKSPACE, pg.K_ESCAPE):
                    now = pg.time.get_ticks()
                    if now - getattr(self, "last_backspace_time", 0) < 250:
                        print(f"[DEBUG MENU] Mashing debounced: key={pg.key.name(key)}")
                        continue  # 🛡️ Debounce rapid mashing
                    self.last_backspace_time = now

                    menu_type = getattr(self, "menu_type", "normal")
                    print(f"[DEBUG MENU] Backspace/Esc pressed: title='{self.title}', menu_type='{menu_type}', key={pg.key.name(key)}")
                    if menu_type == "match_play":
                        if key == pg.K_BACKSPACE:
                            control_callback = None
                            for title, cb, _ in self.items:
                                if any(w in title for w in ("Control", "Leave", "Exit", "Surrender")):
                                    control_callback = cb
                                    break
                            if control_callback:
                                print(f"[DEBUG MENU] match_play: Executing control callback: {control_callback}")
                                control_callback()
                            else:
                                print(f"[DEBUG MENU] match_play: Sending prompt_exit_minigame packet")
                                self.game.network.send(consts.CHANNEL_MENUS, "mainmenu", {"value": {"action": "prompt_exit_minigame"}})
                        continue
                    elif menu_type == "match_control":
                        if self.items:
                            print(f"[DEBUG MENU] match_control: Executing Resume Game (item 0)")
                            self.items[0][1]()
                        continue
                    else:
                        # 🛡️ Normal server menu (lobby / setup menus): Consume Backspace and trigger last option or back callback!
                        if self.items:
                            print(f"[DEBUG MENU] Normal menu: Executing back option '{self.items[-1][0]}'")
                            self.items[-1][1]()
                        continue

                    # activate the last option, we'll assume it exits the menu or navigates back.
                    self.items[-1][1]()
                    # Do not pop client menu if this is a server-navigated menu (allowing server-driven back/confirm dialog)
                    is_server_menu = self.menu_event and (
                        self.menu_event.startswith("builder_") or 
                        self.menu_event in ("edit_element_select", "element_action_select", "mainmenu", "pong_arcade_menu")
                    )
                    if self.autoclose and not is_server_menu:
                        if self.parrent:
                            self.parrent.pop_last_substate()
                        else:
                            self.game.pop()

                elif key == pg.K_PAGEDOWN and self.music_volume is not None:
                    # Builder paste: only meaningful in the builder menu
                    if self.game.builder_clipboard:
                        from libs.builder.utils import handle_builder_paste
                        handle_builder_paste(self.game)
                elif key == pg.K_LEFT:
                    if self.preview_volume > 0:
                        self.preview_volume = max(0, self.preview_volume - 5)
                        speech.speak(f"{self.preview_volume}")
                        snd = self.direct_soundgroup.labeled_sources.get("menu_preview")
                        if snd and snd.source:
                            snd.source.gain = (self.preview_volume / 100) * (self.direct_soundgroup.parent.volume_categories.get("ui", [100])[0] / 100)
                elif key == pg.K_RIGHT:
                    if self.preview_volume < 100:
                        self.preview_volume = min(100, self.preview_volume + 5)
                        speech.speak(f"{self.preview_volume}")
                        snd = self.direct_soundgroup.labeled_sources.get("menu_preview")
                        if snd and snd.source:
                            snd.source.gain = (self.preview_volume / 100) * (self.direct_soundgroup.parent.volume_categories.get("ui", [100])[0] / 100)
                elif event.unicode and event.unicode.isprintable():
                    new_pos = self.return_first_match(event.unicode, self.pos)
                    if new_pos != self.pos:
                        self.pos = new_pos
                        if self.click:
                            self.direct_soundgroup.play(self.click, cat="ui")
                        self.speak_current_item()
            # Consume all events when in menu - don't pass them to gameplay
        return True

    def select_current_item(self):
        now = pg.time.get_ticks()
        if now - getattr(self, "last_select_time", 0) < 250:
            print(f"[DEBUG MENU] Item selection debounced (mashing protection)")
            return  # 🛡️ Debounce rapid mashing on single or multi options!
        self.last_select_time = now

        self.items[self.pos][1]()
        if self.enter_sound != "":
            self.direct_soundgroup.play(self.enter_sound, cat="ui")
        if self.autoclose:
            if self.parrent:
                self.parrent.pop_last_substate()
            else:
                self.game.pop()

    def move_up(self):
        if self.pos > 0:
            self.pos -= 1
            if self.click != "":
                self.direct_soundgroup.play(self.click, cat="ui")
        elif self.wrapping:
            self.pos = len(self.items) - 1
            if self.wrap != "":
                self.direct_soundgroup.play(self.wrap, cat="ui")
        else:
            self.pos = 0
            if self.edge != "":
                self.direct_soundgroup.play(self.edge, "ui")
        self.speak_current_item()

    def move_down(self):
        if self.pos < len(self.items) - 1:
            self.pos += 1
            if self.click != "":
                self.direct_soundgroup.play(self.click, cat="ui")
        elif self.wrapping:
            self.pos = 0
            if self.wrap != "":
                self.direct_soundgroup.play(self.wrap, cat="ui")
        else:
            self.pos = self.pos
            if self.edge != "":
                self.direct_soundgroup.play(self.edge, cat="ui")
        self.speak_current_item()

    def move_top(self):
        self.pos = 0
        self.speak_current_item()
        if self.click != "":
            self.direct_soundgroup.play(self.click, cat="ui")

    def move_end(self):
        self.pos = len(self.items) - 1
        self.speak_current_item()
        if self.click != "":
            self.direct_soundgroup.play(self.click, cat="ui")

    def set_music_volume(self, gain: float, changepref=True):
        if 0 <= gain <= 100:
            self.music_volume = gain
            if self.mus is not None:
                if self.mus.source is not None: self.mus.source.gain = gain / 100
                self.mus.volume = gain
        options.set("menu_music_volume", self.music_volume)
        speech.speak(f"music volume set to {self.music_volume}%", id="music_volume")

    def exit(self):
        self.stop_preview_sound()
        super().exit()
        options.save()
        if self.mus != None:
            active = getattr(self.game, "_active_menu_music", None)
            if active and active.get("sound") == self.mus:
                # Give the next menu state 100ms to claim this music
                self.game.call_after(100, lambda: self._cleanup_menu_music(self.mus, active["soundgroup"]))
            else:
                self.game.automate(
                    self.mus.source, "gain", 0.0, 500, 
                    callback=self.mus.destroy, cancelable=False
                )
        if self.close:
            self.direct_soundgroup.play(self.close, cat="ui")

    def _cleanup_menu_music(self, sound, soundgroup):
        active = getattr(self.game, "_active_menu_music", None)
        if active and active.get("sound") == sound:
            if active.get("kept"):
                active["kept"] = False
            else:
                self.game.automate(
                    sound.source, "gain", 0.0, 500, 
                    callback=sound.destroy, cancelable=False
                )
                self.game._active_menu_music = None

