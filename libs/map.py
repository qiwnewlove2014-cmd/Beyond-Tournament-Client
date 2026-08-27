# map parser.
from .speech import speak
from . import data_parser
from .audio_diagnostics import probe as audio_probe


# Static labels only: map IDs, paths and arbitrary packet type strings must
# never become diagnostic labels. Related cheap geometry shares one bucket.
_SPAWN_TIMINGS = {
    "reverb": "map.spawn.reverb",
    "ambience": "map.spawn.ambience",
    "music": "map.spawn.music",
    "soundSource": "map.spawn.source",
    "pannable": "map.spawn.source",
    "megaphoneSpeaker": "map.spawn.speaker",
    "instrument": "map.spawn.instrument",
    "platform": "map.spawn.geometry",
    "door": "map.spawn.geometry",
    "zone": "map.spawn.geometry",
}


class Map_parser:
    def __init__(self, game, world_map):
        self.game = game
        self.mapobj = world_map
        self.map_data = ""

    def load(self, data: dict, destroy_entities=True):
        try:
            audio_probe.call("map.destroy", self.mapobj.destroy, destroy_entities)
        except Exception as e:
            print(e)
        self.map_data = data
        self.mapobj.minx = data["minx"]
        self.mapobj.miny = data["miny"]
        self.mapobj.minz = data["minz"]
        self.mapobj.maxx = data["maxx"]
        self.mapobj.maxy = data["maxy"]
        self.mapobj.maxz = data["maxz"]
        for element in data["elements"]:
            key = element["type"]
            if hasattr(self.mapobj, f"spawn_{key}"):
                try:
                    # The existing dispatch remains unchanged; only the timing
                    # bucket is constrained to a fixed, low-cardinality set.
                    label = (_SPAWN_TIMINGS.get(key, "map.spawn.other")
                             if type(key) is str else "map.spawn.other")
                    with audio_probe.span(label):
                        getattr(self.mapobj, f"spawn_{key}")(**element["data"])
                except Exception  as e:
                    print(e)
                    speak(f"Map Error: {e}")
