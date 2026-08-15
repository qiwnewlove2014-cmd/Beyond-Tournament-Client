# WesternHorse — Sound Asset Credits

All horse sound assets in this folder come from the **"A Western Drama"** game
sound pack (western-theme game audio — clean field recordings of horses,
carriages and gunfights), re-encoded to the game's audio standard:

**OGG Vorbis · mono · 44 100 Hz · loudness-normalized to ~-20 LUFS**
(`loudnorm=I=-20:TP=-2:LRA=9`), exactly like the existing HorseBeast assets.

## ⚠️ Important: walk/run footstep split

Footsteps are single-beat clips (0.50 s each) so one clip plays per stride
without overlapping — same rule as HorseBeast.

- `step/walk/` — played while cruising (movement_time > 230 ms)
- `step/run/`  — played while galloping / sprint burst (movement_time ≤ 230 ms)

The walk/run selection is implemented in the entity's `move()` override
(see `server/libs/objects/horse_beast.ts` for the reference implementation).

---

## Source

- **Pack:** "A Western Drama" game audio (folder `A Western Drama/` next to the
  repos — original game files, not the game's final mix).
- **License:** user-supplied local pack; confirm distribution terms before
  shipping the compiled game publicly.

---

## File → source clip mapping

| Output file | Source clip | What it is |
| --- | --- | --- |
| `summon.ogg` | travels/`AWD_SFX_Horses_Leg spur+Neigh_01.ogg` | Loud arrival neigh (spur jingle + neigh) |
| `amb/noise1.ogg` | travels/`AWD_SFX_Horses_Leg spur+Neigh_02.ogg` (0.40–2.45 s) | Idle whinny (spur click cut + 40 ms fade-in) |
| `amb/noise2.ogg` | travels/`AWD_SFX_Horses_Leg spur+Neigh_03.ogg` (0.40–2.45 s) | Idle whinny (spur click cut + 40 ms fade-in) |
| `amb/noise3.ogg` | travels/`AWD_SFX_Horses_Leg spur+Neigh_01.ogg` (0.25–1.72 s) | Idle whinny variant (spur click cut + 40 ms fade-in) |
| `attack/attack1.ogg` | shootings/`AWD_SFX_LoopShooting_horsecomplain.ogg` (2.80–3.95 s) | Angry/agitated horse cry |
| `attack/attack2.ogg` | shootings/`AWD_SFX_LoopShooting_horsecomplain.ogg` (13.00–13.72 s) | Angry/agitated horse cry |
| `attack/attack3.ogg` | shootings/`AWD_SFX_LoopShooting_horsecomplain.ogg` (14.00–14.58 s) | Angry/agitated horse cry |
| `death/death1.ogg` | shootings/`AWD_SFX_LoopShooting_horsecomplain.ogg` (0.2–2.6 s) | Complaining/struggling cry |
| `death/death2.ogg` | shootings/`AWD_SFX_LoopShooting_horsecomplain.ogg` (3.0–5.4 s) | Complaining/struggling cry |
| `death/death3.ogg` | shootings/`AWD_SFX_LoopShooting_horsecomplain.ogg` (6.0–8.4 s) | Complaining/struggling cry |
| `hit_player/hithoof1.ogg` | HorseBeast `hit_player/hithoof1.ogg` (in-game asset) | Hoof-strike impact |
| `hit_player/hithoof2.ogg` | HorseBeast `hit_player/hithoof2.ogg` (in-game asset) | Hoof-strike impact |
| `hit_player/hithoof3.ogg` | HorseBeast `hit_player/hithoof3.ogg` (in-game asset) | Hoof-strike impact |
| `step/walk/walk1.ogg` | travels/`AWD_SFX_Horses_D_Trots.ogg` (4.50–5.00 s) | Single trot step |
| `step/walk/walk2.ogg` | travels/`AWD_SFX_Horses_D_Trots.ogg` (4.95–5.45 s) | Single trot step |
| `step/walk/walk3.ogg` | travels/`AWD_SFX_Horses_D_Trots.ogg` (5.35–5.85 s) | Single trot step |
| `step/run/run1.ogg` | travels/`AWD_SFX_Horses_D_Galop.ogg` (2.50–3.00 s) | Single gallop stride |
| `step/run/run2.ogg` | travels/`AWD_SFX_Horses_D_Galop.ogg` (4.30–4.80 s) | Single gallop stride |
| `step/run/run3.ogg` | travels/`AWD_SFX_Horses_D_Galop.ogg` (5.50–6.00 s) | Single gallop stride |

---

## How the sounds were prepared

1. Located the horse-related clips in the "A Western Drama" pack by filename
   (`*horse*`, `*neigh*`, `*gallop*`, `*trot*`, `*spur*`) and verified each
   with ffprobe.
2. Picked the clearest clip per category (neigh = arrival/ambient, enemy horse
   = attack, horse-complain = death, trot/gallop loops = footsteps).
3. For footsteps, analyzed the envelope of the trot/gallop loops to find the
   steady hoofbeat section, then cut 0.50 s windows (one stride each).
4. Re-encoded to mono / 44 100 Hz / libvorbis with
   `loudnorm=I=-20:TP=-2:LRA=9` (same standard as HorseBeast).

## Using this folder

- Drop `WesternHorse/` into `client/data/entities/` (already done here).
- The entity class on the server loads `amb/`, `attack/`, `death/`,
  `hit_player/` and `step/` folders + `summon.ogg` automatically (same loader
  as HorseBeast) — see `server/libs/objects/horse_beast.ts` and
  `zomby.ts` for how to register the new horse type.
- Rename the folder to match the new entity's sound profile name if you want
  it distinct from `WesternHorse`.
