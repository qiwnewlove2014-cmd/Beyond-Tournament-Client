import threading
import time
import os
import pygame
import struct

class SecureInt:
    """
    A protected integer that prevents memory modification via Cheat Engine.
    It stores the real value alongside a XOR'd shadow value.
    If the real value is modified in memory, it will not match the shadow value,
    triggering an immediate game exit.
    """
    def __init__(self, initial_value=0):
        # Generate a unique key for this instance based on memory address and real time
        self._key = int(time.perf_counter() * 1000000) ^ id(self)
        self._value = int(initial_value)
        self._shadow = self._value ^ self._key

    def get(self):
        # Verification
        if self._value != (self._shadow ^ self._key):
            # Tampering detected!
            print("CRITICAL: Memory tampering detected on SecureInt! Exiting...")
            os._exit(1)
        return self._value

    def set(self, new_value):
        # Verification before setting
        if self._value != (self._shadow ^ self._key):
            print("CRITICAL: Memory tampering detected on SecureInt! Exiting...")
            os._exit(1)
        
        self._value = int(new_value)
        self._shadow = self._value ^ self._key

class SecureFloat:
    """
    Similar to SecureInt but for floats.
    Since bitwise operations don't work directly on floats, we use struct to pack/unpack to 64-bit int.
    """
    def __init__(self, initial_value=0.0):
        self._key = int(time.perf_counter() * 1000000) ^ id(self)
        self._value = float(initial_value)
        self._shadow = self._float_to_int(self._value) ^ self._key

    def _float_to_int(self, f):
        return struct.unpack('<Q', struct.pack('<d', f))[0]

    def get(self):
        current_int = self._float_to_int(self._value)
        if current_int != (self._shadow ^ self._key):
            print("CRITICAL: Memory tampering detected on SecureFloat! Exiting...")
            os._exit(1)
        return self._value

    def set(self, new_value):
        current_int = self._float_to_int(self._value)
        if current_int != (self._shadow ^ self._key):
            print("CRITICAL: Memory tampering detected on SecureFloat! Exiting...")
            os._exit(1)
            
        self._value = float(new_value)
        self._shadow = self._float_to_int(self._value) ^ self._key

import psutil
import ctypes

def detect_cheat_engine():
    """Returns True if Cheat Engine process or window is found."""
    # 1. Process Name Detection
    forbidden_processes = [
        "cheatengine-x86_64.exe", 
        "cheatengine-i386.exe", 
        "cheatengine-x86_64-seh.exe"
    ]
    
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() in forbidden_processes:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    # 2. Window Title Detection (Catches renamed executables)
    EnumWindows = ctypes.windll.user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    GetWindowText = ctypes.windll.user32.GetWindowTextW
    GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible

    cheat_engine_found = False

    def foreach_window(hwnd, lParam):
        nonlocal cheat_engine_found
        if IsWindowVisible(hwnd):
            length = GetWindowTextLength(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                GetWindowText(hwnd, buff, length + 1)
                if "Cheat Engine" in buff.value:
                    cheat_engine_found = True
                    return False
        return True
    
    EnumWindows(EnumWindowsProc(foreach_window), 0)
    return cheat_engine_found

def _speedhack_watchdog():
    """
    Background thread to detect Speedhack and Cheat Engine presence globally.
    """
    print("Anti-Cheat: Speedhack & Process watchdog initialized.")
    # Give the game some time to stabilize
    time.sleep(2.0)
    
    last_real_time = time.time()
    last_game_time = pygame.time.get_ticks() / 1000.0
    
    while True:
        time.sleep(1.0)
        
        # 1. Global Process Check
        if detect_cheat_engine():
            print("CRITICAL: Cheat Engine process detected! Exiting...")
            os._exit(1)
        
        # 2. Speedhack Check
        current_real_time = time.time()
        current_game_time = pygame.time.get_ticks() / 1000.0
        
        real_delta = current_real_time - last_real_time
        game_delta = current_game_time - last_game_time
        
        if game_delta > (real_delta * 1.5):
            print(f"CRITICAL: Speedhack detected! (Real: {real_delta:.2f}s | Game: {game_delta:.2f}s). Exiting...")
            os._exit(1)
            
        last_real_time = current_real_time
        last_game_time = current_game_time

def start_speedhack_watchdog():
    """
    Starts the anti-cheat speedhack watchdog in a daemon thread.
    Must be called after pygame.init().
    """
    t = threading.Thread(target=_speedhack_watchdog, daemon=True)
    t.start()
