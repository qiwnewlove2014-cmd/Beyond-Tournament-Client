# Cinema Room Renderer — a library other games can adopt (v4, extracted)

> เป้าหมายของเอกสารนี้: ให้โปรเจกต์อื่นหยิบ "ห้องลำโพง" ไปใช้ได้ โดยไม่ต้องยกเกมนี้ไปด้วย
> v1 ถามว่า "สกัดได้ไหม" — v2 วัดว่าได้ครึ่งหนึ่ง — **v3 ลองย้ายแกนออกมานอก
> `libs/` — v4 (2026-09-15) สรุปเป็นข้อตกลงสุดท้าย: เกมไม่เปลี่ยนโครงสร้างเลย**
> โค้ดโรงหนังอยู่ `client/libs/audio/cinema/` ครบเหมือนเดิม และ **สำเนาที่
> เผยแพร่ได้** อยู่ที่ `cinema-room/` (ข้าง ๆ `client/`) มี pyproject/README/
> CHANGELOG/ตัวอย่าง/เทสต์ของตัวเอง — เกมห้าม import สำเนานั้น และทั้งสองฝั่ง
> เทียบกันแบบไบต์ต่อไบต์โดยเทสต์ของเกม

---

## 1. Mission

Make the room renderer usable **outside this game**: another project hands over a
list of speakers and a stereo PCM stream and gets one mono feed per speaker,
aware of where each speaker stands, how loud it runs and how far it is trimmed.

The portable unit is deliberately *not* "the cinema feature". It is:

> stereo PCM frame (s16le) in -> `resolve_room()` fixes the speaker layout ->
> `CinemaRenderer.render()` -> one mono feed per speaker out.

Everything else in the package — OpenAL transport, jukebox integration, live
instruments, voice/megaphone routing, music-bot peer feeds, map XML, menus,
permissions — is this game's adapter layer and stays where it is.

### What an adopting game writes

```python
from cinema_room import CinemaSpeakerSpec, resolve_room, CinemaRenderer

# Its own speaker list, in its own coordinates -- no map XML, no game object.
specs = [CinemaSpeakerSpec(slot, (x, y, z), level=level, delay_ms=trim)
         for slot, (x, y, z), level, trim in my_engine.speakers_in(room)]

placement = resolve_room(specs, anchor, report=why)   # None -> play plain stereo
renderer = CinemaRenderer(anchor, placement.profile_name, specs=placement.specs)

for left, right in my_engine.stereo_frames():          # any PCM source
    for slot, mono in renderer.render(left, right):    # 20 ms, s16le
        my_engine.play_at(slot_positions[slot], mono)
```

No OpenAL is required for those three names, which is exactly what the audit in
§3 measures.

## 2. Measured boundary (do not re-derive; re-run the probe instead)

Surveyed 2026-09-15 against the working tree:

| half | where | files | lines | what it needs |
|---|---|---|---|---|
| **portable core** | `libs/audio/cinema/` **and** `cinema-room/cinema_room/` | layout, profiles, placement, router, channel, listener | **1,838** | `math`, `array`, `collections`, each other |
| portable extras | `cinema-room/cinema_room/` only | `rooms.py` (document), `port.py` (protocol), `player.py` (transport) | 571 | `json`, `typing`, `collections` |
| the plugin | `cinema-room/cinema_room/openal.py` | 1 file | 126 | `cyal` (OpenAL) |
| game transport | `libs/audio/cinema/` | `bank.py`, `speech.py` | 2,175 | `cyal`, the game's logger |
| game adapters | `libs/audio/cinema/` | `plugin.py`, `live.py`, `peer.py`, `__init__.py` | 1,895 | this game's jukebox / instruments / voice / music bot |

Per-module import audit (AST, from the probe):

| module | lines | imports |
|---|---|---|
| channel.py | 178 | `array`, `collections` |
| layout.py | 366 | `math` |
| listener.py | 374 | `math` |
| placement.py | 536 | `math` |
| profiles.py | 175 | `math` |
| router.py | 209 | `math` |

Two facts make the extraction cheap rather than heroic:

- **The package is already plugin-shaped.** No file in it imports a game module.
  The game is passed in as constructor arguments and used duck-typed.
- **Only two files touch OpenAL at all**: `bank.py` (source/buffer pools, EFX
  sends, per-source gain and position) and `speech.py` (its own sources and
  sends). Everything else is arithmetic on bytes.

Tests that travel with the core today (offline, fake-based, no game and no
device): `test_cinema_placement.py`, `test_cinema_speaker.py`,
`test_cinema_speaker_delay.py`, `test_cinema_wiring.py` (the bit-identical
parity contract), plus the new `test_cinema_portability.py`.

Verification commands:

```
python tools/cinema_room_portability_check.py            # audit + render, exit 1 if the core drifts
python tools/cinema_room_portability_check.py --emit     # the portable room document
python tools/cinema_room_portability_check.py --json r.json
cd client && python -m unittest discover -s tests -p "test_cinema_portability.py"
```

## 3. The two boundaries, pinned

**Engine boundary.** `bank.py` and `speech.py` are the only files allowed to
import `cyal`. A core module that grows an engine import fails
`test_cinema_portability.py`, because that is the day the library stops being
portable. The probe renders one frame with `cyal` replaced by a tripwire (the
import succeeds, any attribute access raises), so "runs at all" means "never
reached for OpenAL" — and it also runs on a machine with no OpenAL installed.

**Game boundary.** Only `bank.py`, `live.py` and `peer.py` import the game's
logger (`libs/deferred_log`). A library logs through what its caller injects; a
default no-op logger is the right default.

## 4. Portable room document (`cinema-room/1`)

Another game does not have `cinemaSpeaker` map elements, so the room travels as
a document. `--emit` prints the demo room in exactly this shape, and the probe
reads it back with `--json`:

```json
{
  "format": "cinema-room/1",
  "room": "demo-theatre",
  "anchor": [0.0, 0.0, 0.0],
  "profile": "auto",
  "speakers": [
    {"slot": "front_l", "x": -3.0, "y": 10.0, "z": 0.0, "level": 100},
    {"slot": "side_l",  "x": -8.0, "y":  4.0, "z": 0.0, "level": 80, "delay_ms": 12}
  ]
}
```

Rules of the format:

- `slot` is one of the seven room slots or `auto` (position decides). A wrong
  label is **overruled by the position** and reported in the resolver's
  warnings, so a document may carry bad labels without producing a bad room.
- `level` is a percentage when above 4, a multiplier otherwise (the game's own
  map attributes do the same) — one reader, `coerce_spec`, so a document and a
  map element can never disagree.
- `delay_ms` 0–100: a deliberate Haas-style decorrelation trim, not latency to
  hide. It *is* latency for timing purposes: `CinemaRenderer.extra_latency_s`
  reports it so a caller syncing live notes to a song can subtract it.
- **A room with no stereo front pair is refused**, and the refusal reason comes
  back through `resolve_room(..., report=[...])`. A room is never invented from
  a lone speaker.
- A room read off a map/document is exactly the speakers it has: build the
  renderer with `CinemaLayout(anchor, specs, use_ring=False)` (what
  `RoomPlan(fill=False)` does). Only a profile that was *asked for* may pad
  itself out from the geometric ring.

## 5. Host ports (what an adopting engine provides)

Measured from the core's own use, not guessed:

1. **PCM in** — stereo s16le frames, any frame length (the room latches the
   frame size it is handed and reads its trims in those frames).
2. **Listener pose** — position and facing, per frame; nothing reaches into a
   camera object.
3. **Speaker positions** — from the room document, or the engine's own map.
4. **Occlusion (optional)** — a callback giving a wall tier per speaker
   position; absent, every speaker is unoccluded.
5. **A logger (optional)** — default no-op.
6. **A clock and a scheduler** — needed only by the transport half (trims and
   fades are scheduled), never by the core.

For an engine that is not OpenAL, the engine half needs a **backend port** as
well, and that port now exists in the library (`cinema_room/port.py`):
fourteen calls — source and buffer creation, mono s16le upload, queue/unqueue,
how many buffers are finished, play/pause/stop state, gain, position, a
one-time spatial configuration, and delete. `cinema_room/openal.py` implements
it through `cyal`, and `cinema_room/player.py` drives it (including the
per-speaker trims). A game with no 3D sources or EFX sends implements the same
calls and loses only those features.

What is still OpenAL-shaped and not yet on the port: this game's own
`libs/audio/cinema/bank.py`, which calls `cyal` directly
(`context.gen_source()`, `cyal.BufferFormat.MONO16`, `cyal.SourceState.*`) and
owns the EFX sends, the occlusion and category gains, the recovery ladders and
the fade/retire bookkeeping. Moving it onto `RoomBackend` is Phase 3's
remainder; it changes no behaviour and is what would let *this* game swap
transports too.

## 6. Phased plan (each phase ends at a green gate)

**Phase 0 — baseline (done, keep re-runnable).** Full client suite green;
`tools/cinema_room_portability_check.py` exits 0. Do not edit anything yet.

**Phase 1 — boundary audit (done for the core, 2026-09-15).** §2 and §3 are the
result: the portable core is measured, not assumed, and the engine and game
boundaries are pinned by tests. Remaining work in this phase is the *engine*
half: list every `cyal` call in `bank.py`/`speech.py` and label each one as
"needed by every engine" (create source, upload, queue, state, gain) or
"OpenAL only" (EFX sends, 3D position, distance model).

**Phase 2 — read the room from a document (DONE 2026-09-15).** The
`cinema-room/1` format is `cinema_room/rooms.py` with one implementation
(`parse_room` / `read_room` / `write_room` / `room_document`), the probe uses
it, and `renderer_for()` answers the `fill` question in one call. Gate met: the
probe, `test_cinema_room_library.py` and the game suite are green.

**Phase 3 — backend port (in the copy DONE, game half open).**
`cinema-room/cinema_room/port.py` defines `RoomBackend`, `openal.py` implements
it with `cyal` and `player.py` drives it, trims included, all verified over an
in-memory fake with no device (`cinema-room/tests/test_library.py`). What
remains is optional and stays the owner's call: moving this game's `bank.py`
onto the same port, with zero behaviour change. Gate (when attempted): parity
tests unchanged and green; a fake backend drives `test_cinema_wiring`'s room
tests.

**Phase 4 — package identity (DONE 2026-09-15, publishing open).**
`cinema-room/` — next to `client/` and `server/` — is the publishable package:
`pyproject.toml` (name `cinema-room`, `0.1.0`, `requires-python >=3.11`, **no
required dependencies**, `openal` extra for `cyal`), `README.md`,
`CHANGELOG.md`, the `cinema_room` package itself, two runnable examples (one
with no engine at all), its own tests, and `tools/portability_check.py`. The
game imports none of it: `client/libs/audio/cinema/` stays the whole feature, as
it was, and the copy is kept in step by a test.

`PublishedCopyTests` in `tests/test_cinema_portability.py` compares the six
portable modules byte for byte (and checks the metadata parses, the engine is an
extra, and the library's own modules are present). The fix when they differ is a
`cp` in whichever direction the change was made; the copy's own probe prints the
exact command. Still the owner's call: licence, repository location, and whether
to publish at all.

**Phase 5 — parity proof.** The bit-identical front-only path
(`router.PARITY_PLAN`) and the plain-pair pins in `test_jukebox.py` pass
unchanged, before and after. These are the contract that the shipped jukebox
never moves.

**Phase 6 — publish (owner decision only).** Not part of the green path here.

## 7. Rules and invariants

- The game's audible behaviour must not change; the zero-copy front-only path
  stays bit-identical (`test_cinema_wiring`).
- Never fork or modify `cyal`; the backend port wraps it.
- No server-side change is needed or allowed for this work.
- A core module may only import the standard library and its siblings — the
  audit in `test_cinema_portability.py` is the enforcement, not this sentence.
- Portability rules from `AGENTS.md` apply to any new tooling: name every text
  encoding explicitly; never spawn a bare command name or a shell string;
  tests keep their own private temp names.
- Do not commit or push from this task; leave the tree for the owner.
- Keep the root `AGENTS.md` cinema section authoritative: if a subsystem moves,
  update the file paths it names in the same change.

## 8. Open decisions for the owner

1. Library name (`cinema-room`? `roomrender`?) and whether PyPI is in scope.
2. License (MIT matches cyal).
3. Should the *transport* (bank) ship as an optional extra, or only the pure
   core, with each game writing its own playback?
4. Repository location and support policy.

## 9. Findings so far

**2026-09-15 — final arrangement: the game is untouched, the copy lives outside.**

- An earlier attempt moved the six core modules out to `client/cinema_room/`
  with shims at the old paths. It worked (1483 tests green) and was reverted the
  same day: two homes for one feature inside the client is exactly the confusion
  this document exists to remove, so the extraction is a **copy beside the tree**
  instead of a move inside it.
- `client/libs/audio/cinema/` — back to its original self-contained shape: 12
  files, 4,070 lines of game code plus the 1,838-line portable core, all as they
  were. No game file, no test and no server file needs to know the library
  exists.
- `cinema-room/` at the tree root — the installable side, 17 files:
  `pyproject.toml`, `README.md`, `CHANGELOG.md`, `cinema_room/` (the six core
  modules plus `rooms.py`, `port.py`, `player.py`, `openal.py`, `__init__.py`),
  `examples/render_a_room.py` (renders a room with no engine and prints every
  speaker's feed) and `examples/play_a_room.py` (the OpenAL path),
  `tests/test_library.py` (21 tests, device-free) and `tools/portability_check.py`.
- The pin: `tests/test_cinema_portability.py::PublishedCopyTests` compares the
  six core modules byte for byte (CRLF normalised), checks the metadata parses,
  the engine is an extra rather than a dependency, and the library's own modules
  are present. Verified by mutation: appending one comment to the copy fails it.
- `RoomPlayer` implements a per-speaker `delay_ms` the way the game's bank does
  in spirit: a whole-frame hold plus a sample-exact cut, rendered once per
  distinct trim, and a speaker is not fed until the history it cuts into exists
  (`cinema-room/tests/test_library.py` reads the fed bytes back and checks the
  480/480 split of a 30 ms trim at 20 ms frames).
- Client suite green; server untouched (`tsc --noEmit` clean, the four cinema
  tools pass, including the one that reads the client's profile list).

**2026-09-15 — the core was already portable (measured, before the move).**

- `python tools/cinema_room_portability_check.py` exits 0: 1,838 lines across
  six modules, imports `math`/`array`/`collections` plus siblings only.
- The same probe resolves a six-speaker room (`profile theatre`, no warnings)
  and renders one 20 ms stereo frame with OpenAL replaced by a tripwire:
  `front_l` 100% of the left channel, `side_l`/`side_r` 20% each, `rear_l` 5%,
  `rear_r` 12%, `front_r` silent — the Mid/Side spread working, reached without
  a device.
- `tests/test_cinema_portability.py` (11 tests) pins: core imports, engine and
  logger boundaries, the tripwired render, the room document reader, and that
  the resolver repairs a document whose wall labels are swapped (position wins,
  warning raised).
- Two traps found while writing the probe, both worth carrying into Phase 3:
  - `resolve_room(speakers, anchor, *, report=...)` — `report` is keyword-only,
    and the resolved room's profile lives on `RoomPlacement.profile_name`
    (`RoomPlan.profile_name` is the alias callers usually want).
  - A room read off a map must be rendered with `use_ring=False`; passing bare
    specs makes `CinemaLayout` invent ring speakers, so a six-speaker room
    plays seven. The probe follows the game's `RoomPlan.fill` rule for that
    reason, and a library must document it or every adopter will hit it.
- The copy is guarded, not trusted: `PublishableCopyTests` compares every
  module byte for byte, so a change made in one place and not the other fails
  the game's suite instead of shipping a published copy that disagrees with the
  game it came from.
- Two copies of the six core modules is a deliberate cost, paid for by keeping
  the game's structure untouched: the copy can drift, so a test compares them
  and the copy's probe names the modules out of step and prints the `cp` that
  fixes it. A change to a portable module belongs in both.
- Not portable yet, in priority order: (1) the game's own `bank.py` still calls
  `cyal` directly instead of going through `RoomBackend` (§5) — a behaviour-
  preserving refactor, not a feature; (2) the map XML reader for this game's
  `cinemaSpeaker` elements: `client/tools/cinema_room_check.py` reads maps but
  prints text rather than a `cinema-room/1` document, so a room cannot yet be
  exported from a map in one step; (3) licence and repository location; (4) the
  per-cabinet mode/profile policy, which is a game decision rather than a
  library concern.

---

*Drafted 2026-09-14; v2 measured the core with the portability probe; v3
(2026-09-15) moved the core inside the client and back again; v4 (2026-09-15)
ships it as `cinema-room/` beside the tree, with the game's own
`client/libs/audio/cinema/` exactly as it was and a test pinning the two copies
together.*
