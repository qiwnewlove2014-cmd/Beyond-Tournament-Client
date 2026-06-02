
import sys
import traceback
import os

# Redirect stderr to a file
log_file = "client_debug.log"

# Clean previous log
if os.path.exists(log_file):
    try:
        os.remove(log_file)
    except:
        pass

class Logger(object):
    def __init__(self):
        self.terminal = sys.stdout
        self.log = open(log_file, "a", encoding='utf-8')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

sys.stdout = Logger()
sys.stderr = sys.stdout

print("Starting client debug logging...")

try:
    import final_hour
    final_hour.main()
except Exception:
    print("\nCRITICAL ERROR CAUGHT:")
    traceback.print_exc()
    print(f"\nLog saved to {os.path.abspath(log_file)}")
    input("Press Enter to exit...")
