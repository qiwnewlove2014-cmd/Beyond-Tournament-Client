import pygame
from . import state, audio_manager
from .speech import speak


class Key_config_screen(state.State):
    def __init__(
        self,
        game,
        func,
        options_menu=None,
        display_name=None,
        key_validator=None,
        cancel_keys=(),
    ):
        """get a function string and set the first pressed key as the key for that function, updating the players key config and pop."""
        super().__init__(game)
        self.func = func
        self.func_call= options_menu
        self.display_name = display_name or func
        self.key_validator = key_validator
        self.cancel_keys = frozenset(cancel_keys)
        self.done = False

    def enter(self):
        self.game.direct_soundgroup.play("ui/keyconfig/start.ogg")
        speak(f"Please press the key you want for {self.display_name}.")

    def update(self, events):
        if self.done:
            return True
        for event in events:
            if event.type == pygame.KEYDOWN:
                if event.key in self.cancel_keys:
                    self.done = True
                    self.game.direct_soundgroup.play("ui/keyconfig/end.ogg")
                    speak("Key binding canceled.", False)
                    self.game.call_after(300, self.func_call)
                    break

                error = (
                    self.key_validator(event.key)
                    if self.key_validator is not None
                    else None
                )
                if error:
                    self.game.direct_soundgroup.play("ui/error.ogg")
                    speak(error)
                    continue

                self.done = True
                speak(pygame.key.name(event.key))
                self.game.keyconfig.set(event.key, self.func)
                
                self.game.direct_soundgroup.play("ui/keyconfig/end.ogg")
                speak("Done.", False)
                
                self.game.call_after(500, self.func_call)
                break
        return True

    def exit(self):
        pass
