# Run footstep samples: what is missing, and why

A player who sprints on a surface with **no recorded run samples** hears the
walking sound at a running cadence. That is not a regression and not a server
problem — it is a gap in the audio assets, and the code has always filled it
silently.

## The rule the game applies

`client/libs/objects/entity.py` (in `Entity.move`):

```python
if mode == "run" and not os.path.exists(
    f"{consts.SOUNDPREPEND}/steps/{tile}/run"
):
    mode = "walk"
```

So a surface has a run variant **if and only if the folder
`data/steps/<tile>/run/` exists** and holds at least one `.ogg`. Nothing else
is needed: `AudioManager.load_buffer` -> `path_utils.random_item` picks a
random file out of that folder on every step, and `get_next_cycle_item` is
only involved for attack/hit sets. Dropping the files in is the whole change —
no code edit, no test edit, no map edit.

## Missing today (24 of 33 surfaces)

Save under `client/data/steps/<tile>/run/`. The walk set next to it says how
many samples that surface usually carries; matching that count is the
convention, not a requirement.

- [ ] bare — 1 walk sample
- [ ] bridge
- [ ] broken_glass
- [ ] cave
- [ ] ceramic
- [ ] clay
- [ ] cloth
- [ ] debris
- [ ] deck
- [ ] deep_sand
- [ ] deep_snow
- [ ] glass
- [ ] **grass — 5 walk samples** (Mor Lam Field, main, main2, Apartment_complex)
- [ ] hardwood — 5 walk samples
- [ ] ice
- [ ] ladder
- [ ] metal_pipe
- [ ] mud
- [ ] snow — 4 walk samples
- [ ] stone — 6 walk samples
- [ ] tree
- [ ] water
- [ ] **wet_ground — 5 walk samples** (main, main2)
- [ ] wooden_floor — 5 walk samples

Bold entries are the ones seen in this project's own maps, so they are the
ones a player is most likely to notice.

## Already recorded (9) — do not redo

`carpet`, `concrete`, `deep_water`, `dirt`, `gravel`, `metal`, `sand`,
`underwater`, `wood`. All nine came from the project's first commit
(`d3d12a7`); the other twenty-four were never recorded.

File names inside a `run/` folder are free: `carpet/run/` uses
`run-01.ogg`…`run-05.ogg`, `gravel/run/` uses `1.ogg`…`6.ogg`, and
`deep_water/run/` carries eleven. Any `.ogg` name works.

## Deliberately not done

Playing the tile's **walk** sample pitched up (a rate-raised stand-in) was
considered and **rejected by the owner on 2026-09-19**: this game's whole point
is that a surface sounds like itself, and a pitched walk is a fake run. The
fallback stays as it is until real samples exist. Do not change the footstep
sound silently — if a run variant is ever synthesised, it needs the owner's
call and the four player-facing documents, because it changes what players
hear.

## Verifying after adding files

Start the client (assets are read at load time), stand on that surface, hold
run, and the step sound becomes the recorded run set. `data/steps/<tile>/run`
existing is the entire condition, so an empty folder or a non-ogg file inside
it changes nothing.
