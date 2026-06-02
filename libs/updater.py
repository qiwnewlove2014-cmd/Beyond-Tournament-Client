import os
import shutil
import zipfile
import subprocess
from pygame import key
import requests
import pySmartDL as dl
from . import state, menu, version
from .speech import speak

nothing = lambda: None


class Updater(state.State):
    def __init__(self, game, check=True):
        super().__init__(game)
        self.check = check
        try:
            self.downloader = dl.SmartDL(
                "https://final-hour.net/fh.zip",
                threads=2,
                progress_bar=False,
                timeout=10,
            )
        except Exception as e:
            self.game.exit()
        self.last_progress = 0
        self.paused = False

    def enter(self):
        # Local import to break circular dependency
        from . import menus
        super().enter()
        # Bypass all update logic and go to the main menu
        menus.main_menu(self.game)

    def exit(self):
        super().exit()
        if self.downloader and not self.downloader.isFinished():
            self.downloader.stop()

    def update(self, events):
        super().update(events)
        
    def get_eta(self):
        return (
            self.downloader.get_eta(human=True)
            if self.downloader.get_eta()
            else "unknown"
        )