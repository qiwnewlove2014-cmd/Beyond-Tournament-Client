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


_cycle_states = {}


def get_next_cycle_item(dir_path):
    """Returns the next sequential item for attack/hit sounds in a 2 -> 1 -> 3 cycle,
    otherwise falls back to random_item."""
    norm_path = os.path.normpath(dir_path).replace("\\", "/")
    if not os.path.isdir(norm_path):
        return dir_path
    
    try:
        files = os.listdir(norm_path)
    except Exception as e:
        print(f"Error listing dir {norm_path}: {e}")
        return dir_path

    # Check if there are attack or hit files
    attack_files = sorted([f for f in files if f.lower().startswith("attack") and f.lower().endswith(".ogg")])
    hit_files = sorted([f for f in files if f.lower().startswith("hit") and f.lower().endswith(".ogg")])
    
    if attack_files:
        ordered = []
        for digit in ['2', '1', '3']:
            for f in attack_files:
                if digit in f:
                    ordered.append(f)
                    break
        for f in attack_files:
            if f not in ordered:
                ordered.append(f)
        
        if ordered:
            idx = _cycle_states.get(norm_path, 0)
            chosen_file = ordered[idx % len(ordered)]
            _cycle_states[norm_path] = (idx + 1) % len(ordered)
            return f"{norm_path}/{chosen_file}"

    if hit_files:
        ordered = []
        for digit in ['2', '1', '3']:
            for f in hit_files:
                if digit in f:
                    ordered.append(f)
                    break
        for f in hit_files:
            if f not in ordered:
                ordered.append(f)
        
        if ordered:
            idx = _cycle_states.get(norm_path, 0)
            chosen_file = ordered[idx % len(ordered)]
            _cycle_states[norm_path] = (idx + 1) % len(ordered)
            return f"{norm_path}/{chosen_file}"

    return random_item(dir_path)



_bags = {}


def bag_item(dir_path, key=None, prefix=""):
    """Pick a file out of a folder without repeating one until the folder is used up.

    ``random_item`` picks freshly every time, so six cloth samples can come out
    cloth2, cloth2, cloth5, cloth2 and read as a stutter; ``get_next_cycle_item``
    walks a fixed order, which fails the other way -- a walk hears the same six
    in the same sequence forever. A shuffle bag is neither: every file is dealt
    once before any of them comes round again, and the order is different each
    time round.

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
