# HorseBeast — Sound Asset Credits

All horse sound assets in this folder come from **Mixkit** (studio-quality,
modern recordings — no old tape hiss or background people), except the
`hit_player/` impact sounds which are reused from the in-game Minotaur asset.

All files re-encoded to the game's audio standard:
**OGG Vorbis · mono · 44 100 Hz · loudness-normalized to ~-20 LUFS**.

## ⚠️ Important: walk/run footstep split

Footsteps are split into two folders so each footstep is a **single beat**
(0.45–0.55 s) instead of one long clip — this prevents sounds overlapping
when the engine plays a clip on every stride.

- `step/walk/` — played while cruising (movement_time > 230 ms)
- `step/run/`  — played while galloping / sprint burst (movement_time ≤ 230 ms)

The walk/run selection is implemented in `server/libs/objects/horse_beast.ts`
(`move()` override). The base `BossEntity.move()` is called with
`play_sound=false` to silence its default `step/` playback, then this subclass
plays from `step/walk/` or `step/run/` based on the current movement_time.

---

## Source

### Mixkit — Free Horse Sound Effects
- **Library:** https://mixkit.co/free-sound-effects/horse/
- **License:** Mixkit License — free to use (including commercial projects),
  no attribution required.
  - https://mixkit.co/license/
- **Why this source:** clean studio recordings with accurate, descriptive
  labels ("Intense horse stallion neigh", "Hard big horse snort", etc.). No
  tape hiss, no background noise, no people talking.

> Mixkit License does not require attribution. A mention of "Horse sounds from
> Mixkit" in the game's audio credits is appreciated but optional.

---

## File → source clip mapping

| Output file | Mixkit clip (ID) | What it is |
| --- | --- | --- |
| `summon.ogg` | Intense horse stallion neigh (76) | Loud arrival neigh |
| `amb/noise1.ogg` | Horse stallion snore (75) | Idle snore |
| `amb/noise2.ogg` | Stallion horse neigh (1762) | Idle whinny |
| `amb/noise3.ogg` | Scared horse neighing (85) | Alert whinny |
| `attack/attack1.ogg` | Scared stallion horse (30) 0–2s | Angry charge cry |
| `attack/attack2.ogg` | Scared stallion horse (30) 2–4s | Angry charge cry |
| `attack/attack3.ogg` | Scared stallion horse (30) 4–6.2s | Angry charge cry |
| `death/death1.ogg` | Scared horse neighing (85) | Death whinny |
| `death/death2.ogg` | Intense horse stallion neigh (76) | Final neigh |
| `death/death3.ogg` | Scared stallion horse (30) 4–6s | Death cry |
| `hit_player/hithoof1.ogg` | Minotaur `hithorn1.ogg` (in-game asset) | Hoof-strike impact |
| `hit_player/hithoof2.ogg` | Minotaur `hithorn2.ogg` (in-game asset) | Hoof-strike impact |
| `hit_player/hithoof3.ogg` | Minotaur `hithorn3.ogg` (in-game asset) | Hoof-strike impact |
| `step/walk/walk1.ogg` | Horse walking fast on concrete (84) 0.05–0.55s | Single walk step |
| `step/walk/walk2.ogg` | Horse trot on pavement street (81) 0.25–0.70s | Single trot step |
| `step/walk/walk3.ogg` | Horse walking fast on concrete (84) 1.40–1.90s | Single walk step |
| `step/run/run1.ogg` | Horse fast gallop on concrete (83) 0.05–0.60s | Single gallop stride |
| `step/run/run2.ogg` | Horse fast gallop on concrete (83) 1.05–1.60s | Single gallop stride |
| `step/run/run3.ogg` | Horse fast gallop on concrete (83) 1.65–2.20s | Single gallop stride |

---

## How the sounds were prepared

1. Downloaded original 24-bit / 44.1 kHz WAV files directly from
   `assets.mixkit.co/active_storage/sfx/`.
2. Picked the clearest single-take clip for each category using Mixkit's
   descriptive labels (no guessing from generic file names).
3. For long clips (gallop/walk/trot), trimmed the cleanest ~2-second window.
4. Re-encoded to mono / 44 100 Hz / libvorbis with
   `loudnorm=I=-20:TP=-2:LRA=9` so every file is roughly the same perceived
   volume — the game's per-category volume config then does the final mix.

### Minotaur horn-strike sounds (for `hit_player/`)
- **Used in:** `hit_player/hithoof1.ogg`, `hithoof2.ogg`, `hithoof3.ogg`
- **Source:** copied from `client/data/entities/Minotaur/hit_player/hithorn1/2/3.ogg`
  (an existing in-game Beyond Tournament asset).
- **Why:** horse snort/snore sounds read as "breathing", not as a strike
  impact. The Minotaur horn-strike sounds are the game's established "melee
  hit" impact effect, so reusing them keeps hit feedback consistent with the
  rest of the game. Renamed `hithorn` → `hithoof` since horses strike with
  hooves, not horns.
- **Note:** `boss_entity.ts` loads `hit_player/` as a folder (random pick), so
  the filename does not matter to the loader — `hithoof*` is used purely for
  clarity.

Raw Mixkit WAVs are kept under `.freebuff/horse_raw/mk_*.wav` in case any clip
needs to be re-cut differently.
