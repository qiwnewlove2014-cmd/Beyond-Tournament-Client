from . import state, options
from .speech import speak
import pygame


class megaphone_settings(state.State):
    def __init__(self, game, parent=None):
        super().__init__(game, parrent=parent)
        # Volume + Mic Gain sliders
        self.sliders = [
            {
                "label": "Speaker Vol",
                "value": options.get("megaphone_volume", 100),
                "min": 0,
                "max": 100,
                "step": 1
            },
            {
                "label": "Mic Gain",
                "value": options.get("megaphone_mic_volume", 100),
                "min": 0,
                "max": 200,
                "step": 5
            },
        ]
        # Fixed EQ values (not adjustable)
        self.fixed_bass = 2.5
        self.fixed_mid = 1.5
        self.fixed_high = 0.5
        self.current_index = 0
    
    def update(self, events):
        super().update(events)
        for event in events:
            if event.type == pygame.KEYDOWN:
                key = event.key
                
                # TAB: Switch slider
                if key == pygame.K_TAB:
                    if event.mod & pygame.KMOD_SHIFT:
                        if self.current_index > 0:
                            self.current_index -= 1
                        else:
                            self.current_index = len(self.sliders) - 1
                    else:
                        if self.current_index < len(self.sliders) - 1:
                            self.current_index += 1
                        else:
                            self.current_index = 0
                    self.announce_current()
                
                # ENTER/ESC: Save and close
                elif key == pygame.K_RETURN or key == pygame.K_ESCAPE:
                    self.save_and_close()
                
                # UP: Increase value
                elif key == pygame.K_UP:
                    self.adjust_value(1)
                
                # DOWN: Decrease value
                elif key == pygame.K_DOWN:
                    self.adjust_value(-1)
                
                # PAGE UP: Increase by 10x step
                elif key == pygame.K_PAGEUP:
                    self.adjust_value(10)
                
                # PAGE DOWN: Decrease by 10x step
                elif key == pygame.K_PAGEDOWN:
                    self.adjust_value(-10)
                
                # HOME: Set to max
                elif key == pygame.K_HOME:
                    slider = self.sliders[self.current_index]
                    slider["value"] = slider["max"]
                    self.apply_settings()
                    speak(f"{slider['value']:.1f}")
                
                # END: Set to min
                elif key == pygame.K_END:
                    slider = self.sliders[self.current_index]
                    slider["value"] = slider["min"]
                    self.apply_settings()
                    speak(f"{slider['value']:.1f}")
        
        return True
    
    def adjust_value(self, multiplier):
        slider = self.sliders[self.current_index]
        step = slider["step"] * multiplier
        new_value = slider["value"] + step
        
        # Clamp to min/max
        new_value = max(slider["min"], min(slider["max"], new_value))
        slider["value"] = round(new_value, 2)  # 2 decimal places for pitch
        
        self.apply_settings()
        speak(f"{slider['value']:.2f}")
    
    def announce_current(self):
        slider = self.sliders[self.current_index]
        speak(f"{slider['label']}. Slider: {slider['value']:.2f}")
    
    def apply_settings(self):
        """Apply settings to megaphone in real-time"""
        # Speaker Volume (Index 0)
        volume = self.sliders[0]["value"]
        
        # Mic Gain (Index 1)
        mic_gain = self.sliders[1]["value"]
        options.set("megaphone_mic_volume", mic_gain, autosave=False) # Update memory, don't write to disk yet
        
        # Use fixed EQ values
        bass = self.fixed_bass
        mid = self.fixed_mid
        high = self.fixed_high
        
        # Apply to parent (gameplay) if it has megaphone update method
        if self.parrent and hasattr(self.parrent, 'update_megaphone_settings'):
            self.parrent.update_megaphone_settings(volume, bass, mid, high)
    
    def save_and_close(self):
        """Save settings to options and close menu"""
        # Save both values to disk
        options.set("megaphone_volume", self.sliders[0]["value"])
        options.set("megaphone_mic_volume", self.sliders[1]["value"]) # Autosave is True by default
        
        if self.parrent:
            self.parrent.pop_last_substate()
        else:
            self.game.pop()
    
    def enter(self):
        super().enter()
        speak("Megaphone settings. Use TAB to switch sliders, arrows to adjust.")
        if len(self.sliders) > 0:
            self.announce_current()
    
    def exit(self):
        super().exit()
        speak("Megaphone settings closed")
