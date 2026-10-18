"""The controls a cabinet menu opens: two sliders and the scrub line.

Each one previews locally (so the performer hears the change at once) and commits to the
server on close; each one returns to the cabinet's menu when it closes, which is why the
menu import inside them is deferred rather than circular."""

import time

import pygame

from .. import state
from ..speech import speak

from .view import (_current_state, _get_eq_values, _get_jukebox_volume,
                   _position_label, _scrub_menu_label, _scrub_spot)


def _reopen_cabinet_menu(game, parent):
    """Return to the cabinet's menu after a control closes.

    Deferred on purpose: menu.py opens these controls, so importing it at the top
    would close the ring. At call time it is already loaded.
    """
    from .menu import open_jukebox_menu
    open_jukebox_menu(game, parent)


class _JukeboxEqSlider(state.State):
    """Accessible, server-synchronized Bass/Mid/Treble slider state."""

    SEND_INTERVAL = 0.075
    BANDS = (("bass", "Bass"), ("mid", "Mid"), ("treble", "Treble"))

    def __init__(self, game, parent, jukebox_id):
        super().__init__(game, parrent=parent)
        self.jukebox_id = jukebox_id
        self.values = _get_eq_values(parent, jukebox_id)
        self.current_index = 0
        self._last_preview_at = 0.0
        self._closed = False

    def enter(self):
        super().enter()
        speak(
            "Jukebox Sound Profile EQ. Tab switches Bass, Mid, and Treble. "
            "Up and Down adjust. Page Up and Page Down adjust by 10. "
            "Home resets the current band to 50. Enter saves."
        )
        self._announce_current()

    def exit(self):
        super().exit()
        speak("Jukebox Sound Profile EQ closed.")

    def update(self, events):
        super().update(events)
        for event in events:
            if event.type != pygame.KEYDOWN:
                continue
            key = event.key
            if key == pygame.K_TAB:
                direction = -1 if event.mod & pygame.KMOD_SHIFT else 1
                self.current_index = (self.current_index + direction) % len(self.BANDS)
                self._announce_current()
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
                self._close_and_commit()
                break
            elif key == pygame.K_UP:
                self._adjust(1)
            elif key == pygame.K_DOWN:
                self._adjust(-1)
            elif key == pygame.K_PAGEUP:
                self._adjust(10)
            elif key == pygame.K_PAGEDOWN:
                self._adjust(-10)
            elif key == pygame.K_HOME:
                self._set_current(50)
        return True

    def _announce_current(self):
        band, label = self.BANDS[self.current_index]
        speak(f"{label}. Slider: {self.values[band]}%")

    def _adjust(self, amount):
        band, _ = self.BANDS[self.current_index]
        self._set_current(self.values[band] + amount)

    def _set_current(self, value):
        band, _ = self.BANDS[self.current_index]
        value = max(0, min(100, int(value)))
        if value != self.values[band]:
            self.values[band] = value
            self._apply(preview=True)
        speak(f"{value}%")

    def _apply(self, preview):
        values = dict(self.values)
        boxes = _current_state(self.parrent).setdefault("jukeboxes", {})
        box = boxes.setdefault(self.jukebox_id, {"id": self.jukebox_id})
        box["eq_profile"] = "custom"
        box["eq_values"] = values
        player = getattr(self.parrent, "jukebox_player", None)
        if player is not None:
            player.set_eq_profile(self.jukebox_id, "custom", values)

        now = time.monotonic()
        if preview and now - self._last_preview_at < self.SEND_INTERVAL:
            return
        network = getattr(self.game, "network", None)
        if network is not None:
            from .. import consts
            network.send(consts.CHANNEL_MISC, "jukebox_set_eq", {
                "id": self.jukebox_id,
                "eq_values": values,
                "preview": bool(preview),
                "commit": not preview,
            })
            if preview:
                self._last_preview_at = now

    def _close_and_commit(self):
        if self._closed:
            return
        self._closed = True
        self._apply(preview=False)
        self.parrent.pop_last_substate()
        _reopen_cabinet_menu(self.game, self.parrent)


def _open_eq_menu(game, gp, jukebox_id):
    """Open the accessible, real-time Jukebox Equalizer sliders."""
    gp.add_substate(_JukeboxEqSlider(game, gp, jukebox_id))


class _JukeboxScrubControl:
    # The cabinet's Scrub line: Left and Right move its needle. The line IS the control - Enter
    # says what it does and the arrow keys work on it with nothing to open first. The first arrow
    # freezes the cabinet for the room (reusing the pause every listener already understands) and
    # every press inside SCRUB_HOLD_S is free, instant and silent, because nothing is sounding
    # while the needle moves. A held arrow skims (this client never calls pygame.key.set_repeat).
    # Whether the room hears the song again is the *Server's* answer, never this machine's guess.

    STEP_S = 10.0
    STEP_SHIFT_S = 30.0
    STEP_CTRL_S = 60.0
    #: A held arrow waits this long first, so a tap is one clean step, and then
    #: skims at GLIDE_S_PER_S from where it began to be held.
    GLIDE_AFTER_S = 0.35
    GLIDE_S_PER_S = 40.0
    # : How long the cabinet stays frozen after the last arrow. Long enough that a hand moving
    # : between arrows never pays for the room going quiet again (every press inside it is
    # : free), short enough that walking away from a cabinet is a two-second pause rather than
    # : a silent map.
    SCRUB_HOLD_S = 2.0
    # : The hold keeps its own clock: a small tick pumps the held-arrow skim and plays the song
    # : on once the hand has stopped. It is armed only while a scrub is open, and the Server's
    # : own idle timer stays the backstop for a client that never gets to close anything at all.
    TICK_INTERVAL_S = 0.04
    # : How often a skimming needle is read out, and how often the Server is told where it is.
    # : The Server's copy only says what an abandoned scrub would continue from (the closing
    # : packet carries the answer), so it can be far coarser than what the scrubber hears.
    ANNOUNCE_INTERVAL_S = 0.5
    SEND_INTERVAL_S = 0.15
    #: The song's own last two seconds stay behind the needle: landing there
    #: would end the song on resume before anybody heard it (the same margin the
    #: Server keeps).
    END_MARGIN_S = 2.0

    def __init__(self, game, gp, jukebox_id):
        self.game = game
        self.gp = gp
        self.jukebox_id = jukebox_id
        self.duration = 0.0
        self.needle = 0.0
        self._open = False
        self._tick_id = None
        self._held_direction = 0
        self._held_step = self.STEP_S
        self._held_since = 0.0
        self._glide_from = 0.0
        self._last_activity = 0.0
        self._last_announce = 0.0
        self._last_send = 0.0

    # ─── the line ───

    def label(self):
        # What the line reads, its needle included. While a scrub is open the needle is this
        # control's own: the Server is told about a move only now and then, so the cached state
        # would still be reading the number the freeze started at. Between scrubs it is asked
        # fresh, because then it is the playing song's own position.
        if self._open:
            return _scrub_menu_label(self.gp, self.jukebox_id,
                                     spot=(self.needle, self.duration))
        return _scrub_menu_label(self.gp, self.jukebox_id)

    def __call__(self):
        """Enter on the line: what it does, and where the needle is now."""
        spot = _scrub_spot(self.gp, self.jukebox_id)
        if spot is None:
            speak("There is nothing to scrub at this cabinet.")
            return
        needle, duration = spot
        speak(
            f"Scrub playback. The song is at {_position_label(needle)} of "
            f"{_position_label(duration)}. Left and Right move ten seconds, "
            "with Shift thirty, with Control a minute. Hold an arrow to skim. "
            "The cabinet plays on by itself once you stop."
        )

    def arrow(self, direction, mod):
        # One Left or Right on the highlighted line. menu.Menu hands an arrow to the line it is on
        # when that line's own action offers this, which is what makes the scrub a keystroke
        # rather than a menu to enter and leave.
        if not self._open and not self._begin():
            return
        self._press(direction, mod)

    def _begin(self):
        """Freeze the cabinet and start the hold. False when this cabinet has
        nothing to scrub at all."""
        spot = _scrub_spot(self.gp, self.jukebox_id)
        if spot is None:
            speak("There is nothing to scrub at this cabinet.")
            return False
        self.needle, self.duration = spot
        self.needle = self._clamp(self.needle)
        self._open = True
        now = time.monotonic()
        self._last_activity = now
        self._last_announce = 0.0
        self._last_send = 0.0
        self._send("jukebox_seek_start", {})
        self._arm()
        return True

    # ─── needle ───

    def _clamp(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        return max(0.0, min(value, max(0.0, self.duration - self.END_MARGIN_S)))

    def _step_for(self, mod):
        if mod & pygame.KMOD_CTRL:
            return self.STEP_CTRL_S
        if mod & pygame.KMOD_SHIFT:
            return self.STEP_SHIFT_S
        return self.STEP_S

    def _readout(self):
        if self.needle <= 0.05:
            return f"Start of the song, {_position_label(self.duration)} long"
        return (f"{_position_label(self.needle)} of "
                f"{_position_label(self.duration)}")

    def _move(self, delta, force_announce=False):
        now = time.monotonic()
        before = self.needle
        self.needle = self._clamp(self.needle + delta)
        changed = self.needle != before
        if changed and now - self._last_send >= self.SEND_INTERVAL_S:
            self._last_send = now
            self._send("jukebox_seek", {"position": round(self.needle, 2)})
        # A press that changes nothing still answers -- at the very start or the
        # very end of the song the key would otherwise read as broken.
        if (force_announce or not changed
                or now - self._last_announce >= self.ANNOUNCE_INTERVAL_S):
            self._last_announce = now
            speak(self._readout())

    # ─── keys ───

    def _press(self, direction, mod):
        step = self._step_for(mod)
        now = time.monotonic()
        self._held_direction = direction
        self._held_step = step
        self._held_since = now
        self._glide_from = 0.0
        self._last_activity = now
        self._move(direction * step, force_announce=True)

    # ─── the hold ───

    def _tick(self):
        """One beat of the hold: skim if an arrow is still down, then either
        keep the hold open or play the song on."""
        self._tick_id = None
        if not self._open:
            return
        now = time.monotonic()
        if self._held_direction:
            self._glide(now)
            if self._held_direction:
                # A held arrow is a hand still on the key, however long the
                # skim runs: it keeps the hold open by itself.
                self._last_activity = now
        if now - self._last_activity >= self.SCRUB_HOLD_S:
            self._end()
            return
        self._arm()

    def _arm(self):
        if self._tick_id is None:
            self._tick_id = self.game.call_after(
                int(self.TICK_INTERVAL_S * 1000), self._tick)

    def _end(self):
        """Let the song play on from where the needle stopped -- once, whoever
        closes the scrub (the hold running out, a map change, a later menu)."""
        if not self._open:
            return
        self._open = False
        self._held_direction = 0
        tick, self._tick_id = self._tick_id, None
        if tick is not None:
            try:
                self.game.cancel_before(tick)
            except Exception:
                pass
        self._send("jukebox_seek_end", {"position": round(self.needle, 2)})

    def _glide(self, now):
        # Keep moving while the arrow is still down. Read straight off the keyboard rather than
        # from key-repeat events: this client never calls pygame.key.set_repeat, so a held arrow
        # produces exactly one KEYDOWN and would otherwise skim nothing at all.
        if not self._held_direction:
            return
        key = pygame.K_LEFT if self._held_direction < 0 else pygame.K_RIGHT
        if not pygame.key.get_pressed()[key]:
            self._held_direction = 0
            self._glide_from = 0.0
            return
        if now - self._held_since < self.GLIDE_AFTER_S:
            return
        if self._glide_from <= 0.0:
            # First beat past the delay: this only starts the clock, so how far
            # the arrow has been held is measured between beats, never from the
            # press (which would apply the whole hold in one jump).
            self._glide_from = now
            return
        rate = self.GLIDE_S_PER_S * (self._held_step / self.STEP_S)
        self._move(self._held_direction * rate * max(0.0, now - self._glide_from))
        self._glide_from = now

    # ─── the scrub's own packets ───

    def _send(self, event, payload):
        from .. import consts
        network = getattr(self.game, "network", None)
        if network is None:
            return
        data = {"id": self.jukebox_id}
        data.update(payload)
        network.send(consts.CHANNEL_MISC, event, data)


class _JukeboxVolumeSlider(state.State):
    """Accessible cabinet-volume slider with immediate local preview."""

    SEND_INTERVAL = 0.075

    def __init__(self, game, parent, jukebox_id):
        super().__init__(game, parrent=parent)
        self.jukebox_id = jukebox_id
        self.value = _get_jukebox_volume(parent, jukebox_id)
        self._last_preview_at = 0.0
        self._closed = False

    def enter(self):
        super().enter()
        speak(
            "Jukebox Volume. Up and Down adjust. Page Up and Page Down "
            "adjust by 10. Home sets 100. End sets 0. Enter saves."
        )
        speak(f"Volume. Slider: {self.value}%")

    def exit(self):
        super().exit()
        speak("Jukebox Volume closed.")

    def update(self, events):
        super().update(events)
        for event in events:
            if event.type != pygame.KEYDOWN:
                continue
            key = event.key
            if key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
                self._close_and_commit()
                break
            if key == pygame.K_UP:
                self._set_value(self.value + 1)
            elif key == pygame.K_DOWN:
                self._set_value(self.value - 1)
            elif key == pygame.K_PAGEUP:
                self._set_value(self.value + 10)
            elif key == pygame.K_PAGEDOWN:
                self._set_value(self.value - 10)
            elif key == pygame.K_HOME:
                self._set_value(100)
            elif key == pygame.K_END:
                self._set_value(0)
        return True

    def _set_value(self, value):
        value = max(0, min(100, int(value)))
        if value != self.value:
            self.value = value
            self._apply(preview=True)
        speak(f"{value}%")

    def _apply(self, preview):
        boxes = _current_state(self.parrent).setdefault("jukeboxes", {})
        box = boxes.setdefault(self.jukebox_id, {"id": self.jukebox_id})
        box["volume"] = self.value
        player = getattr(self.parrent, "jukebox_player", None)
        if player is not None:
            player.set_cabinet_volume(self.jukebox_id, self.value)

        now = time.monotonic()
        if preview and now - self._last_preview_at < self.SEND_INTERVAL:
            return
        network = getattr(self.game, "network", None)
        if network is not None:
            from .. import consts
            network.send(consts.CHANNEL_MISC, "jukebox_set_volume", {
                "id": self.jukebox_id,
                "volume": self.value,
                "preview": bool(preview),
                "commit": not preview,
            })
            if preview:
                self._last_preview_at = now

    def _close_and_commit(self):
        if self._closed:
            return
        self._closed = True
        self._apply(preview=False)
        self.parrent.pop_last_substate()
        _reopen_cabinet_menu(self.game, self.parrent)


def _open_volume_menu(game, gp, jukebox_id):
    gp.add_substate(_JukeboxVolumeSlider(game, gp, jukebox_id))
