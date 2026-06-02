from . import options, consts

try:
    from accessible_output2 import outputs
    speaker = outputs.auto.Auto()
except Exception as e:
    print(f"Warning: Failed to initialize accessible_output2: {e}")
    class DummySpeaker:
        def output(self, text, interrupt=True):
            print(f"[Speech] {text}")
    speaker = DummySpeaker()


history = (
    []
)  # should only be used for viewing on the screen, might contain diffrant things than what's spoken.


def speak(text, interupt=True, store_in_history=True, id=None, silent=False):
    if options.get("mute_speech_on_focus_loss", False) and not pygame.key.get_focused(): silent = True
    if id is not None:
        for item in history:
            if item[1] == id:
                history.remove(item)
    if store_in_history:
        history.append((text, id))
    if not silent:
        speaker.output(text, interupt)
