# Truck (MIT) Arcade Car Physics — Sound Asset Credits

All truck sound assets in this folder come from the **"Truck (MIT) Arcade Car
Physics"** sound pack (MIT-licensed arcade car physics audio), re-encoded to the
game's audio standard:

**OGG Vorbis · mono · 48 000 Hz** (matching the rest of the game's vehicles).

## ⚠️ Important: multi-source engine layout

The truck is a long vehicle: its engine is rendered from **4 spatial sources**
(front axle + rear axle, 5.0 tiles apart, 1.6 tiles wide) so the rumble follows
the driven path around corners — the front leads and the rear trails. The
`sourceLength` / `sourceWidth` / `trailerLagMs` audio fields in
`server/resources/vehicles.json` drive this layout on the client
(`libs/objects/vehicle.py`). A `sourceLength` of 0 keeps the classic
single-point engine (motorcycle).

## Source

- **Pack:** "Truck (MIT) Arcade Car Physics" game audio — see
  `LICENSE-MIT.md` (MIT License, © 2018 Saarg).
- **Horn / brake samples:** user-supplied `Vehicle9_horn.wav` and
  `Vehicle9_brake.wav`, re-encoded to OGG (horn = short blast used for both tap
  and hold-loop honking; brake = looping brake squeal whose pitch follows the
  wheel speed).

## File → source clip mapping

| Output file | Source | What it is |
| --- | --- | --- |
| `engine.ogg` | pack `engine.ogg` | Looping engine loop (idle → revved by pitch) |
| `start.ogg` | pack `start.ogg` | Engine crank/ignition at the cab |
| `stop.ogg` | pack `stop.ogg` | Engine shut-off at the cab |
| `horn.ogg` | user `Vehicle9_horn.wav` | Truck horn (tap = short, hold = looping) |
| `brake.ogg` | user `Vehicle9_brake.wav` | Brake squeal loop (pitch rides wheel speed) |
| `crash.ogg` | pack `crash.ogg` | Impact on blocked terrain / out-of-bounds |
| `wind.ogg` | pack `wind.ogg` | Speed wind (rider only, dry direct + reverb tail) |
| `water_resistance.ogg` | pack `water_resistance.ogg` | Driving through water |
| `mud_resistance.ogg` | pack `mud_resistance.ogg` | Driving through mud |
| `water_land.ogg` | pack `water_land.ogg` | Entering water |

## How the sounds were prepared

1. Kept the pack's engine/start/stop/crash/wind/terrain clips as-is.
2. Replaced the stock horn with the user-supplied `Vehicle9_horn.wav` and added
   `Vehicle9_brake.wav` for the brake pedal.
3. Re-encoded every clip to mono / 48 000 Hz / libvorbis so the game's
   `load_buffer` (pyogg) can stream them as spatial sources.

## Using this folder

- Drop `truck_mit_arcade_car_physics/` into `client/data/vehicles/` (already
  done here).
- The truck definition lives in `server/resources/vehicles.json` under
  `truck` (audio `soundProfile` points at this folder name).
- The engine/horn/brake rendering is implemented in
  `client/libs/objects/vehicle.py`; server-side state (`horn_on`, `brake_on`,
  `revving`) in `server/libs/objects/vehicle.ts`.
