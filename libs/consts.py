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
#: Party Sync's live jam leg: the same Opus upload as CHANNEL_MUSICBOT, but
#: with no timeline and a receiver that starts on a 20 ms pre-buffer (two
#: 10 ms frames) instead of the song path's 240 ms. A session member's captured program goes
#: here while nothing is playing, so the party hears them play, not a quarter
#: second later. Only used once the Server advertises `music_live_v1`.
CHANNEL_MUSICBOT_LIVE = 25
#: Party Sync's cinema-room leg: the host's ONE mixed Opus stream, already
#: summed from every jammer. Only non-members on the cabinet's map receive it;
#: session members keep their direct private feed and must not hear the room.
CHANNEL_MUSICBOT_PARTY_ROOM = 26
CHANNEL_MEGAPHONE = 30
# PA upload with the frame's position in it: version, epoch and frame sequence
# in front of the Opus frame, so a listener can see that a frame is MISSING and
# conceal it (Opus PLC) instead of holding a reserve big enough to sit through
# every hole. Only used once the Server advertises `pa_timeline_v1`.
CHANNEL_MEGAPHONE_TIMELINE = 31
#: The Pan Delay's movement, three bytes per frame, beside the voice leg. It is
#: reserved out of the player voice-channel range (`get_available_voice_channel`
#: assigns 21-255) so a movement packet can never be mistaken for a speaker.
CHANNEL_VOICE_MOTION = 254
# The wire format on that channel (both ends read it from here): version, then
# the sender's voice_channel, then the stream epoch, then the frame sequence -
# 9 bytes uploaded by the sender, 10 once the Server has inserted the channel.
PA_TIMELINE_VERSION = 1
PA_TIMELINE_UPLOAD_BYTES = 9            # what the sender writes: version + epoch + frameSeq
PA_TIMELINE_HEADER_BYTES = 10           # what a listener reads: + the sender's voice_channel
SOUNDSPREPEND="/data/"
# Bound on the lazy VFS temp cache (MB). Only played assets land on disk,
# and the oldest ones are evicted while this limit is exceeded.
VFS_CACHE_MB = 512

# Update this variable to force clients to update their game
CLIENT_VERSION = "BT-1.9.5"
