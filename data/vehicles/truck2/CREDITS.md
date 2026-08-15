# Livestock Truck (truck2) — Cabin Interior Audio Sound Assets

This folder holds the **"Livestock Truck"** sound set with **cabin interior
audio**: separate exterior and interior engine loops, so the driver hears the
stereo cabin while everyone outside hears the mono exterior engine at the
truck's position.

## ⚠️ Important: interior / exterior audio model

The livestock truck renders **two engine layers** (see
`server/resources/vehicles.json` audio fields + `client/libs/objects/vehicle.py`):

- **Exterior** (world, 3D): `truck_idle_ext.ogg` ↔ `truck_drive_ext.ogg`
  crossfaded by wheel speed — heard by everyone at full volume.
- **Interior** (stereo, direct): `truck_idle_int.ogg` ↔ `truck_drive_int.ogg`
  crossfaded by wheel speed — heard by the **local rider** at `interiorGain`,
  while the exterior engine is muffled to `interiorExtScale` (0.25) so the
  cabin dominates. Non-riders never hear the interior layer.
- Start / stop one-shots are also split: `start_int` / `stop_int` for the
  rider, `start_ext` / `stop_ext` for everyone else.
- `truck_spawn.ogg` / `truck_unspawn.ogg` announce placement/removal.
- `truck_command.ogg` is the in-cab command-menu UI blip (start/stop engine,
  get out) — deliberately not the main game menu sounds.

## Encoding

- Exterior loops + one-shots: **OGG Vorbis · mono · 48 000 Hz** (3D spatial).
- Interior loops + one-shots + command blip: **OGG Vorbis · stereo ·
  48 000 Hz** (cabin is a direct, non-spatial layer).

## File → purpose mapping

| Output file | What it is |
| --- | --- |
| `truck_idle_ext.ogg` | Exterior engine idle loop |
| `truck_drive_ext.ogg` | Exterior engine drive loop |
| `truck_idle_int.ogg` | Interior (cabin) idle loop — stereo |
| `truck_drive_int.ogg` | Interior (cabin) drive loop — stereo |
| `truck_start_ext.ogg` | Exterior engine crank/ignition |
| `truck_start_int.ogg` | Interior engine crank/ignition — stereo |
| `truck_stop_ext.ogg` | Exterior engine shut-off |
| `truck_stop_int.ogg` | Interior engine shut-off — stereo |
| `truck_command.ogg` | In-cab command menu UI blip — stereo |
| `truck_spawn.ogg` | Placement thunk when the truck spawns |
| `truck_unspawn.ogg` | Removal thunk when the truck despawns |
| `horn.ogg` | Horn (shared with truck1, tap/hold) |
| `brake.ogg` | Brake squeal loop (pitch rides wheel speed) |
| `crash.ogg` | Impact on blocked terrain / out-of-bounds |
| `wind.ogg` | Speed wind (rider only) |
| `water_resistance.ogg` / `mud_resistance.ogg` / `water_land.ogg` | Terrain |

## Using this folder

- Drop `truck2/` into `client/data/vehicles/` (already done here).
- The definition lives in `server/resources/vehicles.json` under `truck2`
  (audio `soundProfile` points at this folder name; `interiorAudio` enables
  the cabin layer).
- The in-cab command menu is opened by `client/libs/gameplay.py` when the
  local player rides a `truck2` (`vehicle_command` packets drive the
  server-side engine switch in `server/libs/objects/vehicle.ts`).
