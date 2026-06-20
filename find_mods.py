import sys

# Pre-import standard stuff
initial_modules = set(sys.modules.keys())

# Import yt_dlp
import yt_dlp

# Let's also import some common submodules of yt_dlp to trigger their imports
try:
    import yt_dlp.extractor
except Exception:
    pass

try:
    import yt_dlp.downloader
except Exception:
    pass

try:
    import yt_dlp.postprocessor
except Exception:
    pass

all_loaded = set(sys.modules.keys())
new_loaded = all_loaded - initial_modules

std_libs = set()
for name in sorted(new_loaded):
    # Get the top-level module name
    top_name = name.split('.')[0]
    # Check if it is a standard library by checking its module file path
    mod = sys.modules.get(name)
    if mod and hasattr(mod, '__file__') and mod.__file__:
        if "site-packages" not in mod.__file__ and "final-hour-client" not in mod.__file__:
            std_libs.add(top_name)

print("Standard libraries:")
for lib in sorted(std_libs):
    print(lib)
