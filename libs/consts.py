EVT_PAUSE = 100
EVT_DESTROY = 101
OS_LINUX = 201
OS_MAC = 202
OS_WINDOWS = 203
TITLE = "Beyond Tournament"
SOUNDPREPEND = "data/"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 13000
SERVER_SOUNDS_URL = "https://final-hour.net/sounds/"
# How long a login attempt tolerates *silence* from the server, in
# milliseconds. This is a watchdog, not a budget: the clock restarts on every
# packet the server sends and every request this client sends, so a slow
# handshake, database lookup or snapshot no longer aborts a login the server is
# still working on. The old fixed window from the moment the player pressed
# "log in" did exactly that -- the client returned to the main menu while the
# server finished authenticating, leaving a session on the map that nobody
# owned (see client/libs/networking.py, Client.login_timed_out).
TIMEOUT = 8000
SETTINGS_KEY = b"mquBJ6q6YIMKxvf8PB880i-bL-DPNv3GPs63FZuD1yQ="
CHANNEL_MISC = 0
CHANNEL_SOUND = 1
CHANNEL_CHAT = 1
CHANNEL_MOVEMENT = 2
CHANNEL_MAP = 4
CHANNEL_PING = 5
CHANNEL_MENUS = 6
CHANNEL_WEAPONS = 7
CHANNEL_SERVER_SOUNDS = 8
# Dedicated unreliable channel for live instrument notes (piano/drum/guitar).
# Keeps them off the reliable CHANNEL_MAP/CHANNEL_SOUND queues where one lost
# world-sound packet head-of-line-blocks every following note (100-500ms spikes).
CHANNEL_JAM = 9
CHANNEL_VOICECHAT = 20
CHANNEL_MUSICBOT = 22
CHANNEL_JUKEBOX_RELAY = 23
CHANNEL_MUSICBOT_TIMELINE = 24
CHANNEL_MEGAPHONE = 30
SOUNDSPREPEND="/data/"
# Bound on the lazy VFS temp cache (MB). Only played assets land on disk,
# and the oldest ones are evicted while this limit is exceeded.
VFS_CACHE_MB = 512

# Update this variable to force clients to update their game
CLIENT_VERSION = "BT-1.8.8"
