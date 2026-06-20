import sys
import os

print("Program started successfully.")
print("sys.path is:")
for p in sys.path:
    print(f"  {p}")

try:
    import yt_dlp
    print("yt-dlp imported successfully!")
    print(f"yt-dlp path: {yt_dlp.__file__}")
except Exception as e:
    print(f"Failed to import yt-dlp: {e}")
