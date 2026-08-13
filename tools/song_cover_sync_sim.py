"""Song + live-cover sync offset simulation.

Scenario: Player A opens a song broadcast to the megaphone/PA. Player B (the
drummer) hears it through the PA and plays along in time. Everyone hears B's
drums through the PA too.

The drummer can only play AFTER hearing the song, so B's hits are shifted by
one network leg (N = RTT + jitter floor + propagation). Then the drum audio
travels another network leg back to every listener. The song owner A hears
their own song through the ZERO-latency local sidechain feed, so at A the
drums are 2 network legs behind the song; at every other listener the song
also took one network leg, so the drums are only 1 leg behind.

This quantifies the offset and shows the delay-compensation fix (delay the
local monitor by one leg / two legs) and the jitter-floor lever.

Run:  python tools/song_cover_sync_sim.py
"""

JITTER_MS = 40.0   # adaptive jitter floor (pre-buffer + margin)
PROP_MS = 15.0     # distance/343 at a typical listening spot


def one_leg(rtt_ms):
    return rtt_ms + JITTER_MS + PROP_MS


def row(rtt_ms, label):
    n = one_leg(rtt_ms)
    offset_owner = 2 * n - PROP_MS          # A's local song (instant) vs drums at 2N
    offset_listener = 2 * n - n             # C hears song at N, drums at 2N
    fix_half = 2 * n - (n + PROP_MS) - PROP_MS   # A delays local feed by N
    fix_full = 2 * n - (2 * n - PROP_MS) - PROP_MS  # A delays by 2N (owner sync = 0)
    print(
        f"  RTT {label:<10} | one leg {n:6.1f}ms | "
        f"owner offset {offset_owner:6.1f}ms | other listeners {offset_listener:6.1f}ms | "
        f"fix-half {fix_half:6.1f}ms | fix-full {fix_full:6.1f}ms"
    )
    return offset_owner, offset_listener


print("=" * 78)
print("SONG + LIVE COVER SYNC OFFSET (true beat at T=0, drummer plays exactly in time)")
print("=" * 78)
print(f"  jitter floor {JITTER_MS:.0f}ms/leg, propagation {PROP_MS:.0f}ms (cancels at the same listening spot)")
print()
print("  Legend:")
print("    one leg        = RTT + 40ms jitter + propagation (one network hop of audio)")
print("    owner offset   = at the SONG OWNER's ears (their song is local/instant, drums are 2 legs)")
print("    other listeners= everyone else (their song took 1 leg too, drums are 2 legs)")
print("    fix-half       = owner delays their local song feed by ONE leg")
print("    fix-full       = owner delays their local song feed by TWO legs (perfect owner sync)")
print()
print("  RTT (client<->server)  scenario")
for rtt in (3, 10, 20, 50, 100):
    row(rtt, "LAN" if rtt <= 10 else "regional" if rtt <= 30 else "internet")

print("\n  --- WITH THE FIX (owner's local song monitor delayed by 2 legs, RTT-adaptive) ---")
for rtt in (3, 10, 20, 50, 100):
    n = one_leg(rtt)
    owner_after = 0.0                      # compensation cancels the owner's 2-leg offset exactly
    others_after = 2 * n - n               # non-owner listeners still hear 1 leg (drummer's own shift)
    print(
        f"  RTT {rtt:<10} | owner offset after fix {owner_after:6.1f}ms (was {2 * n - PROP_MS:6.1f}ms) | "
        f"others {others_after:6.1f}ms (unchanged, 1 leg is the floor)"
    )
print()

import sys

print("=" * 78)
print("TAKEAWAYS")
print("=" * 78)
print("""
1. The drummer physically cannot play before they HEAR the song, so their hits
   are one network leg late no matter what. Their drum audio then travels a
   second leg back to you. That double-count is the 'plays in time but sounds
   late' complaint.

2. At the song owner (you), the offset is the WORST: ~2x the one-leg delay,
   because your own song plays through the zero-latency local sidechain while
   the remote performance round-trips twice. Everyone else hears 1 leg.

3. THE FIX (implemented): the owner's LOCAL song monitor is delayed by
   2RTT + 40ms (piano/drums are MIDI note events - the song reaches the player
   one leg = RTT+40ms, the note returns one round trip = RTT - so the owner
   hears the remote instrument exactly 2RTT+40ms late; measured by an automatic
   ping sampler, capped at 240ms) through a small FIFO in the shared local-feed
   path. The owner's offset drops to ~0; the song start shifting ~40-120ms is
   imperceptible. Audio-stream instruments (guitar) arrive 40ms later and stay
   within one jitter floor. Instruments are covered by the same mechanism:
     - REMOTE instruments (drums/guitar/piano) arrive via the same network
       path -> they already line up with the delayed song, no per-instrument
       code.
     - The OWNER's own instruments stay instant (FIFO only tags 'music'): the
       player anchors to the delayed song themselves, so their own hits sound
       exactly when played and land on the beat they heard.
     - Future producers just add their tag to _COMP_PRODUCERS.

4. Remaining floor: non-owner listeners still hear the remote player 1 leg
   late (the drummer's own hearing shift is physics + network - nobody can
   play before they hear the song). On a LAN that is ~45-60ms.
""")
sys.exit(0)
