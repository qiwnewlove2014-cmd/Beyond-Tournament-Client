"""The spoken-subtitles pages of `MusicBotMenus`.

The switch and status page, the language order and the sync offset. Nothing
here leaves the machine that plays the song: the pages only read and write the
bot's own reader settings.

Moved out of `libs/music_bot/menus.py` on 2026-10-26 and mixed into
`MusicBotMenus`, so every caller still says `bot._menus().<method>()` and the
one-line shims in `controller.py` did not move. The state stays on
`MapMusicBot`: `self.` is this handler and `self._bot.` is the Music Bot. A page
that speaks through the module-level `speak` calls `self._say`, because
`libs.music_bot/menus.speak` is the patch point the menu tests use.

Pins: client/tests/test_music_bot_subtitles.py and test_music_bot_menu_help.py.
Rules: .agents/skills/music_bot_integration/.
"""

from ... import options
from .. import subtitles


class SubtitleMenus:
    """The spoken-subtitles pages."""

    def _open_subtitle_menu(self):
        """Everything about spoken subtitles, behind one line of the menu."""
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        def go_language():
            gp.pop_last_substate()
            self._open_subtitle_language_menu()

        def go_timing():
            gp.pop_last_substate()
            self._open_subtitle_timing_menu()

        def read_status():
            self._say(self._bot.subtitle_reader.status() +
                  " Subtitles come from the video on YouTube: the uploader's "
                  "own track when there is one, otherwise the automatic "
                  "captions. Songs often have none, because the words of a "
                  "song are rarely a caption track.")

        m = menu_mod.Menu(self._bot.game, "Subtitles", parrent=gp)
        m.add_items([
            (self._bot.subtitle_label, self._bot.toggle_subtitles, None,
             "Turns the spoken subtitles on or off: the song's own words, "
             "read out as the song reaches them."),
            (self._bot.subtitle_language_label, go_language, None,
             "Which language YouTube is asked for a caption track in. The "
             "first language with a track is the one spoken."),
            (self._bot.subtitle_timing_label, go_timing, None,
             "Moves the spoken line earlier or later against the song, if "
             "the screen reader falls behind or runs ahead."),
            (lambda: self._bot.subtitle_reader.status(), read_status, None,
             "How far the reader has got, and where the words come from: "
             "the uploader's own caption track, or YouTube's automatic one."),
            ("Back", go_back, None,
             "Returns to the Music Bot menu."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_subtitle_language_menu(self):
        """The order YouTube is asked for a language in."""
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_subtitle_menu()

        def make_label(key, label):
            def label_fn():
                current = getattr(self._bot, "subtitle_language", None)
                return f"{label} (current)" if current == key else label
            return label_fn

        def make_pick(key, label):
            def pick():
                self._bot.subtitle_language = subtitles.normalize_preference(key)
                options.set("music_bot_subtitle_language",
                            self._bot.subtitle_language)
                self._say(f"Subtitle language: {label}.")
                # A track already playing is read again in the new order (the
                # track itself is cached, so only a language change costs a
                # fetch).
                self._bot.subtitle_reader.clear()
                self._bot._begin_subtitles(self._bot._caption_page())
                m.speak_current_item()
            return pick

        m = menu_mod.Menu(self._bot.game, "Subtitle Language", parrent=gp)
        items = [(make_label(key, label), make_pick(key, label), None,
                  "The order YouTube is asked for a caption track in. The "
                  "first language with a track is the one read out; songs "
                  "often have none at all.")
                 for key, label, _languages in subtitles.LANGUAGE_PREFERENCES]
        items.append(("Back", go_back, None,
                      "Returns to the Subtitles menu."))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_subtitle_timing_menu(self):
        """Shift the captions against the audio, half a second a press.

                    Reading a caption takes the reader its own time and the rate is the player's setting,
                    so only the player can say where the line they hear starts.
                    """
        from ... import menu as menu_mod, menus
        gp = self._bot._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._open_subtitle_menu()

        def speak_timing():
            self._say(subtitles.offset_label(self._bot.subtitle_offset))

        def shift(delta):
            def move():
                self._bot.subtitle_offset = subtitles.normalize_offset(
                    self._bot.subtitle_offset + delta)
                options.set("music_bot_subtitle_offset", self._bot.subtitle_offset)
                speak_timing()
                m.speak_current_item()
            return move

        def reset():
            self._bot.subtitle_offset = 0
            options.set("music_bot_subtitle_offset", 0)
            speak_timing()
            m.speak_current_item()

        step = subtitles.OFFSET_STEP_MS / 1000.0
        m = menu_mod.Menu(self._bot.game, "Subtitle Timing", parrent=gp)
        m.add_items([
            (self._bot.subtitle_timing_label, speak_timing, None,
             "How far the spoken line sits from the song, and which way it "
             "has been pushed."),
            (f"Speak {step:.1f} seconds earlier",
             shift(subtitles.OFFSET_STEP_MS), None,
             "Speaks each line half a second earlier, for a screen reader "
             "that falls behind the song."),
            (f"Speak {step:.1f} seconds later",
             shift(-subtitles.OFFSET_STEP_MS), None,
             "Speaks each line half a second later, for a screen reader that "
             "runs ahead of the song."),
            ("Reset timing", reset, None,
             "Puts the timing back to exactly where the video's own captions "
             "are timed."),
            ("Back", go_back, None,
             "Returns to the Subtitles menu."),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)
