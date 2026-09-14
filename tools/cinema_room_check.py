"""See what a cinema room will do, without starting the game.

Three ways to use it:

    python tools/cinema_room_check.py --demo
        Resolve a few example rooms (clean, swapped, contradictory) and print
        what each one becomes, including which speaker is ahead of you and
        which is behind you as you turn.

    python tools/cinema_room_check.py "../server/maps/main.map"
        Read a real map: for every jukebox on it, resolve the cinema speakers
        around it and print the room that would play, plus how loud each
        speaker reaches the cabinet and what stands between it and the room
        (a wall muffles a speaker, it never silences it).

    python tools/cinema_room_check.py "../server/maps/main.map" --make-room 7eLIOyBF
        Print the `<cinemaSpeaker>` lines to paste into that map to give one
        jukebox a complete room (front wall, walls, rear), computed from the
        cabinet's own position so the coordinates are correct by construction.

Nothing here writes to a map or touches the game: it is the same resolver the
client uses at playback time, so the output is what you would hear.
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from math import cos, radians, sin, sqrt, trunc

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from libs.audio.cinema import (ROOM_MAX_DISTANCE, ROOM_REFERENCE_DISTANCE,
                               CinemaLayout, ListenerPose, exclusive_speakers,
                               facing_report, resolve_room)
from libs.audio.cinema.router import CinemaRenderer

# Angles and radius for the room --make-room generates: a front wall, the two
# side walls and the rear, all on one circle so the room is symmetric.
ROOM_PLAN = (
    ("front_l", -30.0),
    ("front_c", 0.0),
    ("front_r", 30.0),
    ("side_l", -90.0),
    ("side_r", 90.0),
    ("rear_l", -150.0),
    ("rear_r", 150.0),
)
# The ring this tool places its own demo speakers on -- NOT the game's room
# radius (see layout.ROOM_RADIUS): a synthetic room is small on purpose so the
# printed coordinates stay readable.
DEMO_RING_RADIUS = 8.0


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def element_spec(element):
    """A map element as the placement resolver reads it."""
    attributes = dict(element.attrib)
    spec = {"id": attributes.get("id", "")}
    bounds = attributes.get("bounds")
    if bounds:
        values = [float(value) for value in bounds.split()]
        if len(values) == 6:
            spec.update(dict(zip(("minx", "maxx", "miny", "maxy", "minz", "maxz"),
                                 values)))
    for key in ("x", "y", "z", "channel", "room", "level", "delay", "aim_yaw",
                "inner_cone_angle", "outer_cone_angle", "outer_cone_gain"):
        if key in attributes:
            spec[key] = _number(attributes[key]) if key != "channel" else attributes[key]
    return spec


def read_map(path):
    """``(jukeboxes, speakers)`` from a .map file: (id, anchor) and raw specs.

    ``bounds`` in a map file is absolute (the map origin is only used inside
    ``<area>`` blocks), so the centre of a box is the element's position.
    """
    root = ET.parse(path).getroot()
    body = root.find("body")
    if body is None:
        raise SystemExit(f"{path} has no <body>")
    jukeboxes = []
    speakers = []
    for element in body:
        if element.tag == "jukebox":
            spec = element_spec(element)
            try:
                anchor = ((spec["minx"] + spec["maxx"]) / 2.0,
                          (spec["miny"] + spec["maxy"]) / 2.0,
                          (spec["minz"] + spec["maxz"]) / 2.0)
            except KeyError:
                continue
            jukeboxes.append((spec.get("id", "?"), anchor))
        elif element.tag == "cinemaSpeaker":
            speakers.append(element_spec(element))
    return jukeboxes, speakers


def read_walls(path):
    """Wall boxes from a .map file, for the "is it behind a wall" estimate.

    Walls are platforms whose type starts with ``wall`` (``wall``, ``wallwood``,
    ``wallglass``, ...), which is exactly how the client's own tiles are named
    and what its occlusion ray tests for. ``<wall>`` elements are accepted too,
    for hand-written maps.
    """
    root = ET.parse(path).getroot()
    walls = []
    for element in root.find("body") or ():
        if element.tag == "wall":
            pass
        elif element.tag == "platform" and str(element.get("type", "")).startswith("wall"):
            pass
        else:
            continue
        values = [_number(value) for value in (element.get("bounds") or "").split()]
        if len(values) == 6:
            walls.append(tuple(int(value) for value in values))
    return walls


def wall_tiles(walls, source, listener):
    """Wall tiles a ray crosses, as the client's own occlusion ray counts them.

    Mirrors ``Map.wall_occlusion_ratio``: the walk is tile by tile, one
    crossing is a light muffle, three or more is the full "through a wall"
    filter. A wall element's box is its tiles, ends included, exactly like
    ``BaseMapObj.in_bound`` treats them.
    """
    x1, y1, z1 = trunc(source[0]), trunc(source[1]), trunc(source[2])
    x2, y2, z2 = trunc(listener[0]), trunc(listener[1]), trunc(listener[2])
    distance = round(sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)) + 1
    hits = 0
    for _ in range(distance):
        if any(minx <= x1 <= maxx and miny <= y1 <= maxy and minz <= z1 <= maxz
               for minx, maxx, miny, maxy, minz, maxz in walls):
            hits += 1
            if hits >= 3:
                return hits          # full occlusion; no need to walk further
        if (x1, y1, z1) == (x2, y2, z2):
            break
        x1 += 1 if x1 < x2 else -1 if x1 > x2 else 0
        y1 += 1 if y1 < y2 else -1 if y1 > y2 else 0
        z1 += 1 if z1 < z2 else -1 if z1 > z2 else 0
    return hits


def audible_lines(room, anchor, walls):
    """Per speaker: how far it is from the room and what is in the way.

    A wall never silences a speaker -- the room applies a lowpass (light for
    one tile, heavy for three or more) measured from that speaker's own spot,
    and the room's own distance ramp: full within reference distance, silent
    at the room's max distance (a plain jukebox pair keeps the narrower 8/40
    it has always had). This is what a builder wants to know before walking
    the room.
    """
    reference = float(ROOM_REFERENCE_DISTANCE)
    span = max(0.0001, float(ROOM_MAX_DISTANCE) - reference)
    lines = ["  at the cabinet:"]
    for slot in room.slots:
        position = room.speakers[slot].position
        distance = sqrt(sum((float(anchor[i]) - position[i]) ** 2 for i in range(3)))
        if distance <= reference:
            loudness = "full"
        elif distance >= ROOM_MAX_DISTANCE:
            loudness = "silent"
        else:
            loudness = f"{1.0 - (distance - reference) / span:.0%} of full"
        tiles = wall_tiles(walls, position, anchor)
        if tiles == 0:
            wall = "clear path"
        elif tiles < 3:
            wall = f"{tiles} wall tile(s) - light muffle"
        else:
            wall = f"{tiles}+ wall tiles - muffled, still audible"
        lines.append(f"    {slot:<8} {distance:5.1f}m  {loudness:<12} {wall}")
    return "\n".join(lines)


def speaker_note(speaker):
    """The trims this speaker carries, or "" when it is at its defaults.

    A room that sounds different from the one next door should say why. The
    delay is the one setting whose effect is not visible in the map file's own
    units -- 25 ms looks like a number, and it is heard as a wider wall.
    """
    notes = []
    delay = float(getattr(speaker.spec, "delay_ms", 0.0) or 0.0)
    if delay > 0.0:
        notes.append(f"+{delay:.0f} ms")
    level = float(getattr(speaker.spec, "level", 1.0) or 1.0)
    if abs(level - 1.0) > 0.005:
        notes.append(f"level {level * 100:.0f}%")
    return f"  [{', '.join(notes)}]" if notes else ""


def _renderer_for(room, anchor):
    """The renderer the game would build for this room, feeds and all.

    A room read off the map is only the speakers the map has (the game builds
    it with ``use_ring=False``), so listing the feeds from the profile alone
    would invent a centre channel and a side pair nobody placed and report a
    room the player never hears. A requested profile is allowed to pad itself
    out, and then the ring is the only thing there is to play.
    """
    # ``fill`` only exists on the host's plan; a room resolved straight off
    # the map is its speakers, so a missing flag means exactly that.
    if getattr(room, "fill", False) or not room.specs:
        return CinemaRenderer(anchor, room.profile_name, specs=room.specs)
    return CinemaRenderer(anchor, room.profile_name,
                          CinemaLayout(anchor, room.specs, use_ring=False))


def describe(room, anchor=(0.0, 0.0, 0.0), heading=None):
    lines = [f"  profile : {room.profile_name}",
             f"  anchor  : {anchor}   room yaw: {room.yaw_deg:.0f}deg"]
    for slot in room.slots:
        speaker = room.speakers[slot]
        how = {"label": "as labelled", "geometry": "placed by position",
               "floor": "moved to a free slot"}.get(speaker.source, speaker.source)
        lines.append(f"  {slot:<8} at {tuple(round(v, 1) for v in speaker.position)}"
                     f"  ({how}){speaker_note(speaker)}")
    for warning in room.warnings:
        lines.append(f"  ! {warning}")
    renderer = _renderer_for(room, anchor)
    feeds = ", ".join(f"{slot} {gain_l:.2f}L/{gain_r:.2f}R"
                      for slot, gain_l, gain_r in renderer.plan())
    lines.append(f"  feeds   : {feeds}")
    if heading is not None:
        lines.extend(facing_lines(room, anchor, (heading,)))
    return "\n".join(lines)


def facing_lines(room, anchor, headings):
    """One line per heading: which speakers are ahead of the listener."""
    lines = []
    for heading in headings:
        pose = ListenerPose((heading, 0.0, 0.0), anchor)
        facing = ", ".join(
            f"{slot} {'ahead' if in_front else 'behind'}"
            for slot, _quadrant, in_front, _distance in facing_report(pose, room))
        lines.append(f"  facing {heading:>3.0f}deg: {facing}")
    return lines


def demo():
    from libs.audio.cinema import CinemaSpeakerSpec

    def speaker(channel, bearing, radius=DEMO_RING_RADIUS, **kwargs):
        angle = radians(bearing)
        return CinemaSpeakerSpec(channel,
                                 (sin(angle) * radius, cos(angle) * radius, 0.0),
                                 **kwargs)

    rooms = {
        "clean room (7 speakers, labelled correctly)": [
            speaker(channel, bearing) for channel, bearing in ROOM_PLAN],
        "front pair only (bug-for-bug the normal jukebox)": [
            speaker("front_l", -30), speaker("front_r", 30)],
        "left and right swapped by mistake": [
            speaker("front_r", -30), speaker("front_l", 30), speaker("front_c", 0)],
        "whole room rotated 90 degrees": [
            speaker(channel, bearing + 90.0) for channel, bearing in ROOM_PLAN],
        "one wall speaker with no partner": [
            speaker("front_l", -30), speaker("front_r", 30), speaker("side_l", -90)],
        "the labels contradict each other": [
            speaker("front_l", 60), speaker("front_r", 120), speaker("front_c", 90),
            speaker("side_r", 0), speaker("side_l", 180)],
    }
    for title, speakers in rooms.items():
        room = resolve_room(speakers, (0.0, 0.0, 0.0))
        print(f"\n{title}")
        if room is None:
            print("  -> no room: the plain two-source jukebox plays instead")
            continue
        print(describe(room))
        # Turning on the spot is the whole point of a room, so show it for the
        # one room with every speaker in it.
        if len(room.slots) == len(ROOM_PLAN):
            print("\n".join(facing_lines(room, (0.0, 0.0, 0.0), (0.0, 90.0, 180.0))))


def report_map(path):
    jukeboxes, speakers = read_map(path)
    if not jukeboxes:
        # The common trap: speakers placed, nothing to anchor them to. A room
        # is resolved around a cabinet, so this is the answer, not "no room".
        print(f"{path}: no jukebox on this map")
        if speakers:
            print(f"  {len(speakers)} cinema speaker(s) here have nothing to "
                  "anchor them --\n  add a Jukebox (Builder menu F8 -> Musical "
                  "Instrument -> Jukebox)")
        else:
            print("  add a Jukebox (Builder menu F8 -> Musical Instrument -> "
                  "Jukebox), then run --make-room <id>")
        return
    print(f"{path}: {len(jukeboxes)} jukebox(es), {len(speakers)} cinema speaker(s)")
    for jid, anchor in jukeboxes:
        print(f"\njukebox {jid} at {tuple(round(v, 1) for v in anchor)}")
        # A speaker belongs to the cabinet it stands closest to: two rooms on
        # one map must never play two songs through the same speaker.
        rivals = [other for other_id, other in jukeboxes if other_id != jid]
        mine = exclusive_speakers(speakers, anchor, rivals=rivals)
        if len(mine) != len(speakers):
            print(f"  ({len(speakers) - len(mine)} of {len(speakers)} speakers "
                  f"stand nearer another cabinet)")
        reasons = []
        room = resolve_room(mine, anchor, room=jid, report=reasons)
        if room is None:
            # Same reasons the in-game menu reports, from the same resolver:
            # which wall is missing is what makes map data fixable.
            print("  -> no room resolved: this cabinet plays the normal "
                  "two-source jukebox")
            for reason in reasons:
                print(f"     {reason}")
            continue
        print(describe(room, anchor, heading=0.0))
        walls = read_walls(path)
        if walls:
            print(audible_lines(room, anchor, walls))
        if not speakers:
            print("  (no cinema speakers on this map yet -- run with "
                  f"--make-room {jid} to get paste-ready lines)")


def make_room(path, jukebox_id, channel_list, radius):
    jukeboxes, _speakers = read_map(path)
    match = [item for item in jukeboxes if item[0] == jukebox_id]
    if not match:
        raise SystemExit(f"no jukebox with id {jukebox_id!r} on {path}")
    anchor = match[0][1]
    plan = [(channel, bearing) for channel, bearing in ROOM_PLAN
            if channel in channel_list]
    print(f"<!-- Cinema Speakers for jukebox {jukebox_id} "
          f"at {tuple(round(v, 1) for v in anchor)}: paste inside <body> -->")
    for index, (channel, bearing) in enumerate(plan):
        angle = radians(bearing)
        x = round(anchor[0] + sin(angle) * radius)
        y = round(anchor[1] + cos(angle) * radius)
        z = round(anchor[2])
        print(f'\t\t<cinemaSpeaker bounds="{x} {x + 1} {y} {y + 1} {z} {z + 1}" '
              f'channel="{channel}" id="cin{index:02d}"/>')
    print("\nThen: restart the server, enable \"Cinema speakers\" in the options "
          "menu,\nwalk to that jukebox, press Enter and queue a song.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("map", nargs="?", help="path to a .map file")
    parser.add_argument("--demo", action="store_true",
                        help="run the built-in example rooms")
    parser.add_argument("--make-room", metavar="JUKEBOX_ID",
                        help="print paste-ready speaker lines for one cabinet")
    parser.add_argument("--channels", default="front_l,front_c,front_r,side_l,side_r,rear_l,rear_r",
                        help="comma separated slots for --make-room")
    parser.add_argument("--radius", type=float, default=DEMO_RING_RADIUS,
                        help="room radius in blocks for --make-room")
    args = parser.parse_args()

    if args.demo or not args.map:
        demo()
        return
    if args.make_room:
        channels = {name.strip() for name in args.channels.split(",") if name.strip()}
        make_room(args.map, args.make_room, channels, args.radius)
        return
    report_map(args.map)


if __name__ == "__main__":
    main()
