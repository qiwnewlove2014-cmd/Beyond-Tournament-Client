import os
import shutil
import random


def random_item(dir):
    """return a random item (a folder or a ffile) given a folder path.
    if the path given doesnt lead to a folder, returns the given path"""
    try: return f"{dir}/{random.choice(os.listdir(dir))}" if os.path.isdir(dir) else dir
    except IndexError as e: 
        print(e)
        return ""


_bags = {}


def bag_item(dir_path, key=None, prefix=""):
    """Pick a file out of a folder without repeating one until the folder is used up.

    ``random_item`` picks freshly every time, so six cloth samples can come out
    cloth2, cloth2, cloth5, cloth2 and read as a stutter; a fixed order (which is
    what ``next_sound_item`` used to walk for attack pools) fails the other way
    -- a walk hears the same six in the same sequence forever. A shuffle bag is
    neither: every file is dealt once before any of them comes round again, and
    the order is different each time round.

    ``key`` is who is asking, and it is why the bags are per asker: armor worn
    by three players on one map would otherwise be dealt from a single deck, so
    one wearer's steps decide what another wearer hears next. ``prefix`` limits
    the bag to the files that are that kind of sound -- the armor folder holds
    its steps, its impacts and its equip sound together, and only the cloth
    ones belong under a footstep.

    The first file of a fresh bag is kept away from the one that just played,
    so the seam between two rounds is not a repeat either. Returns "" when the
    folder holds nothing to play, which callers read as "nothing to play".
    """
    if not dir_path or not os.path.isdir(dir_path):
        return ""
    try:
        candidates = sorted(
            name for name in os.listdir(dir_path)
            if name.lower().endswith(".ogg")
            and (not prefix or name.lower().startswith(prefix.lower()))
        )
    except OSError:
        return ""
    if not candidates:
        return ""

    bag_key = (os.path.normpath(dir_path).replace("\\", "/"), key, prefix.lower())
    state = _bags.get(bag_key)
    if state is None or not state["remaining"]:
        dealt = candidates[:]
        random.shuffle(dealt)
        last = state["last"] if state else None
        # The seam: a fair bag that opens with the sound that just played still
        # reads as a repeat, so that one is moved behind the others.
        if last is not None and len(dealt) > 1 and dealt[-1] == last:
            dealt[0], dealt[-1] = dealt[-1], dealt[0]
        state = {"remaining": dealt, "last": last}

    chosen = state["remaining"].pop()
    state["last"] = chosen
    _bags[bag_key] = state
    return f"{dir_path}/{chosen}"


def next_sound_item(dir_path):
    """Pick the file to play out of a folder path -- the one door every folder sound uses.

    A weapon's `fire/` and `impact/`, an entity's `attack/`, a foley folder: a
    server sends the folder and this decides which sample comes back. Two shapes
    of folder, two answers.

    * **attack / hit pools** come out of a shuffle bag (``bag_item``): every
      sample once before any of them comes round again, a fresh random order
      each round, and the sample that just played kept away from the start of
      the next round so the seam is not a repeat either. This used to walk a
      fixed ``2 -> 1 -> 3`` order, which is the other way a pool stops sounding
      like one: the samples are all heard, but always in the same sequence, so a
      match of swings reads as a single loop (the owner's report, 2026-09-28:
      *"the melee sounds still play attack1.ogg, attack2.ogg, attack3.ogg --
      do not repeat and do not loop, make it random"*).
    * **the prefix is what makes those pools**, not the folder: `fire/` holds a
      swing set *and* a block set in the same place (`Mjolnir`, `Goblin_Dagger`
      ship `attack1-3.ogg` beside `block1-3.ogg`), so only the `attack` files
      belong under a swing and only the `hit` files under an impact. A folder
      with neither prefix is not a pool and falls through to ``random_item`` --
      the plain fresh pick every other folder sound has always had, which may
      repeat itself. A folder that turns out to need "never twice in a row"
      belongs here, not at a call site. Both halves are pinned by
      `client/tests/test_attack_sound_pool.py`.
    """
    if not dir_path or not os.path.isdir(dir_path):
        return dir_path
    try:
        names = [name.lower() for name in os.listdir(dir_path)]
    except OSError:
        return dir_path
    for prefix in ("attack", "hit"):
        if any(name.startswith(prefix) and name.endswith(".ogg") for name in names):
            return bag_item(dir_path, prefix=prefix) or random_item(dir_path)
    return random_item(dir_path)


def copy_folder(src, dst):
    """
    Copy all files and folders from src to dst, overwriting existing files and folders in dst
    """
    if not src.endswith("/") and not src.endswith("\\"):
        src = src + "/"
    if not dst.endswith("/") and not dst.endswith("\\"):
        dst = dst + "/"

    for src_dir, dirs, files in os.walk(src):
        dst_dir = src_dir.replace(src, dst, 1)
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir)
        for file_ in files:
            src_file = os.path.join(src_dir, file_)
            dst_file = os.path.join(dst_dir, file_)
            if os.path.exists(dst_file):
                # in case of the src and dst are the same file
                if os.path.samefile(src_file, dst_file):
                    continue
                os.remove(dst_file)
            shutil.copy(src_file, dst_dir)
