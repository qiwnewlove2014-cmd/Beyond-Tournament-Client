# Beyond Tournament — Project History

How an open-source audio game became a 5x larger project with a community
of its own. This document records the journey from **Final Hour** to
**Beyond Tournament** using numbers measured directly from both codebases.

--------------------------------------------------------------------------------

## 1. Origins: Final Hour

Beyond Tournament is built upon **Final Hour**, an open-source, audio-based
game inspired by the Zombies mode in the Call of Duty series, created by
**The Lower Elements** (Michael Connor Buchan, "TheFake VIP").

### About the original author

- **Born 2003** (22–23 years old as of September 2026), from Scotland.
- Visually impaired since birth (Leber congenital amaurosis, photophobia and
  nystagmus) — a lifelong screen-reader user and blind-gaming advocate.
- Studying a **Computing Science BSc at the University of Stirling**
  (undergraduate, as of 2026 — no postgraduate degree yet).
- Maintains **29 public repositories** under the `lower-elements` GitHub
  organisation, mostly *tools for blind users and audio-game developers*
  rather than finished games: audio bindings (*cyal*, *cySteamAudio*),
  screen-reader libraries (*accessible_output3*, *Zenblue*), a game engine
  (*lege*), MUD/MMO engines, TTS libraries, IRC clients and more.
- Open-source advocate (Linux, Matrix, the fediverse); self-describes as
  aspiring to be like **Foaly** (the gadget-master from the Artemis Fowl
  books) — a fitting summary of his habit of building foundations that
  others build on.

His audio stack — *cyal* (OpenAL bindings), HRTF models and the client/server
architecture — is exactly what this game's engine is built on.

Upstream repositories:

- Final Hour client: https://github.com/lower-elements/final-hour-client-public (GPL-3.0)
- Final Hour server: https://github.com/lower-elements/final-hour-server-public (AGPL-3.0)
- Final Hour docs: https://github.com/lower-elements/final-hour-docs
- cyal (OpenAL bindings): https://github.com/lower-elements/cyal (MIT)

Final Hour's public source release: **5 July 2025**.

--------------------------------------------------------------------------------

## 2. Timeline

| Date | Milestone |
|---|---|
| 2022–2025 | Final Hour developed privately by The Lower Elements |
| 2025-07-05 | Final Hour client + server released publicly on GitHub |
| 2026-06-02 | Beyond Tournament client + server repositories initialized (from the Final Hour codebase) |
| 2026-06 → 2026-09 | Major expansion: 476+ commits, dozens of new systems |
| 2026-09-08 | This document created |

--------------------------------------------------------------------------------

## 3. Codebase growth (measured from `libs/` on both projects)

| | Final Hour | Beyond Tournament | Growth |
|---|---|---|---|
| **Client (Python)** | 44 files / 6,706 lines | 99 files / **40,412 lines** | **x6.0 (+503%)** |
| **Server (TypeScript)** | 117 files / 11,281 lines | 247 files / **54,949 lines** | **x4.9 (+387%)** |
| **Total** | 161 files / 17,987 lines | 346 files / **95,361 lines** | **x5.3 (+430%)** |

Roughly **~77,000 lines are new code** written on top of the ~18,000 lines
carried over from Final Hour.

### How much of the original is still unchanged?

Measured file-by-file, the code that survived untouched is a small fraction:

| File | Final Hour | Beyond Tournament | Diff |
|---|---|---|---|
| clock.py | 17 lines | 17 lines | identical (0) |
| data_parser.py | 42 | 42 | identical (0) |
| os_tools.py | 12 | 12 | identical (0) |
| movement.py | 175 | 188 | ~17 lines changed |
| buffer.py | 313 | 347 | ~44 lines changed |
| audio_manager.py | 256 | 982 | rewritten/expanded (~896) |
| event_handeler.py | 377 | 2,597 | rewritten/expanded (~2,338) |
| gameplay.py | 749 | 2,416 | rewritten/expanded (~1,927) |
| world_map.py | 693 | 1,525 | rewritten/expanded (~978) |

> ~95% of the current codebase is new or heavily rewritten.

--------------------------------------------------------------------------------

## 4. New systems (not present in Final Hour)

### Client (42 new modules)
- **Music Bot** — full YouTube/Spotify playback, EQ (3-band), volume, crossfade, megaphone broadcast, direct/relay transports
- **Jukebox** — per-cabinet queues, repeat modes, EQ profiles, relay workers, 403-recovery ladder, prefetch/cache
- **Party Sync** — private "listen together" sessions, team-talk voice, party chat, mid-song joins
- **Instruments** — piano, guitar, drums (keyboard/MIDI input, jam-note sync, timeline-aware relaying)
- **Megaphone system** — clock-driven PA broadcasting, per-speaker EFX slots, wall occlusion
- **Game audio recorder** — game-only WASAPI process-loopback capture (Windows 10 2004+)
- **Anti-cheat**, VFS, watchdog, instance manager, crash reporting, safe vorbis, UI renderer, vehicle handler, shields, presence sounds, YouTube resolver

### Server (55 new modules/folders)
- **Builder** — full in-game map editor (walls, perks, sound sources, reverbs, jukeboxes...), validator, archive manager, lock system
- **Officer Mason AI** — Thai/English tokenizer, RAG, intent routing, leaderboard answers, staff auto-answer
- **Pong arcade** — cabinet menu flow, matchmaking, physics, sound manager
- **Instruments** — piano/guitar/drum/jukebox event handling, music timeline validation
- **Party sync**, vehicles, shields, league system, leaderboard, blackjack, helper AI + remote control, ticket system, matchmaking, security sentinel, packet validator, crash-log browser, presence sounds, translator, player docs & patch notes, backups

--------------------------------------------------------------------------------

## 5. What was kept from Final Hour

- The **audio foundation**: 3D positional audio via OpenAL Soft + HRTF models (`.mhr`), reverb zones, megaphone concept
- The **client/server architecture** (Python client + TypeScript server over ENet)
- Music tracks 1–11 (credited in [credits.txt](credits.txt))
- Small utility modules (clock, data_parser, os_tools) left untouched
- Licensing structure: client GPL-3.0, server AGPL-3.0

--------------------------------------------------------------------------------

## 6. Perspective

Estimated at industry-standard productivity (5,000–10,000 lines per
developer-year for complex systems code), the ~77,000 new lines represent
roughly **10–15 developer-years** of work — completed in about three months
of recorded history (476+ commits). The original project's "good bones"
(its audio stack) combined with this expansion is what makes Beyond
Tournament what it is today.

--------------------------------------------------------------------------------

## 7. Credits & licensing

- Full credits: [credits.txt](credits.txt)
- Client license: GNU General Public License v3.0 (see [LICENSE](../LICENSE))
- Server license: GNU Affero General Public License v3.0
- Original project: The Lower Elements — Final Hour
  (https://github.com/lower-elements/final-hour-client-public)
