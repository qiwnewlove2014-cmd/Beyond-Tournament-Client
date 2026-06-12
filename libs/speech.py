from . import options, consts

try:
    from accessible_output2 import outputs
    speaker = outputs.auto.Auto()
except Exception as e:
    print(f"Warning: Failed to initialize accessible_output2 Auto speaker: {e}")
    speaker = None

# Fallback mechanism if Auto fails (common when compiled due to win32com/SAPI5 issues)
if speaker is None:
    fallback_outputs = []
    try:
        from accessible_output2.outputs.jaws import Jaws
        fallback_outputs.append(Jaws)
    except Exception: pass
    try:
        from accessible_output2.outputs.nvda import NVDA
        fallback_outputs.append(NVDA)
    except Exception: pass
    try:
        from accessible_output2.outputs.window_eyes import WindowEyes
        fallback_outputs.append(WindowEyes)
    except Exception: pass

    for OutputClass in fallback_outputs:
        try:
            temp_speaker = OutputClass()
            if temp_speaker.is_active():
                class FallbackWrapper:
                    def __init__(self, spk):
                        self.spk = spk
                    def output(self, text, interrupt=False, **kwargs):
                        try:
                            self.spk.speak(text, interrupt=interrupt, **kwargs)
                        except Exception: pass
                        try:
                            self.spk.braille(text, **kwargs)
                        except Exception: pass
                speaker = FallbackWrapper(temp_speaker)
                print(f"Fallback: Successfully initialized {OutputClass.__name__}")
                break
        except Exception:
            pass

if speaker is None:
    print("Warning: All screen reader initializations failed. Falling back to DummySpeaker.")
    class DummySpeaker:
        def output(self, text, interrupt=True, **kwargs):
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
        speaker.output(text, interrupt=interupt)
