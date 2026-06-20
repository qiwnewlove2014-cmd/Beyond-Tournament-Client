import sys
import modulefinder

finder = modulefinder.ModuleFinder()
# Run finder on the yt_dlp package entry point
import yt_dlp
finder.run_script(yt_dlp.__file__)

std_modules = []
for name, mod in finder.modules.items():
    if mod.__file__ is not None:
        # Check if the file is in the Python standard library directory
        if "site-packages" not in mod.__file__ and "final-hour-client" not in mod.__file__:
            std_modules.append(name)

print("Standard library modules imported by yt_dlp:")
for name in sorted(std_modules):
    # Only show top-level standard library modules to keep it simple
    if "." not in name:
        print(name)
