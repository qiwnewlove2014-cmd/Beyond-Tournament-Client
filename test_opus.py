import os
import sys

# Test without adding dll directory first
try:
    from pyogg import OpusDecoder
    print("OpusDecoder loaded successfully without add_dll_directory.")
except Exception as e:
    print("Failed without add_dll_directory:", e)

# Test with add_dll_directory
try:
    if hasattr(os, 'add_dll_directory'):
        dll_dir = os.path.abspath('dlls_windows')
        print(f"Adding DLL directory: {dll_dir}")
        os.add_dll_directory(dll_dir)
    from pyogg import OpusDecoder
    print("OpusDecoder loaded successfully after add_dll_directory.")
except Exception as e:
    print("Failed after add_dll_directory:", e)
