"""In-process test: piano & drums routed through the PA must be shaped by
distance (hearing_range) and wall occlusion exactly like the voice/music
speakers - so instruments do not "converge to the middle" and walls dampen
them the same way they dampen speech.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CALLS = []  # (x, y, z, volume, reference_distance, max_distance)


class FakeAudioMngr:
    def play_unbound(self, path, x, y, z, volume=100, cat="miscelaneous",
                     reference_distance=15.0, max_distance=100.0, direct_filter=None,
                     **kw):
        CALLS.append({
            "x": x, "y": y, "z": z, "volume": volume,
            "reference_distance": reference_distance, "max_distance": max_distance,
        })
        snd = types.SimpleNamespace(source=types.SimpleNamespace())
        return snd


class FakeMegaphone:
    def __init__(self, speaker_data, occ_results):
        self.speaker_data = speaker_data
        self._occ = occ_results
        self.lowpass_filter = object()
        self.eq_slot = object()
        self.reverb_slot = object()

    def _check_speaker_occlusion(self, speaker_pos, player_pos):
        if callable(self._occ):
            return self._occ(speaker_pos, player_pos)
        return self._occ


class FakeCam:
    def __init__(self, x, y, z):
        self.focus_object = types.SimpleNamespace(x=x, y=y, z=z)


class FakeGP:
    def __init__(self, cam, mega):
        self.camera = cam
        self.megaphone = mega
        self.music_bot = types.SimpleNamespace(volume=50)


def make_speaker(x, y, z, hearing_range, base_volume=0.6):
    return {
        "position": (x, y, z),
        "base_volume": base_volume,
        "hearing_range": hearing_range,
    }


def occ_by_index(speaker_data, occ_map):
    def _check(speaker_pos, player_pos):
        for i, spk in enumerate(speaker_data):
            if spk["position"] == tuple(speaker_pos):
                return occ_map.get(i, 0.0)
        return 0.0
    return _check


def run_piano(gp):
    from libs import piano
    p = piano.PianoAudio.__new__(piano.PianoAudio)
    p.am = FakeAudioMngr()
    p.gameplay = gp
    p.active_piano_notes = {}
    p._get_pedal_filter = lambda peer, kind: None
    p._tag_sounds = lambda *a, **k: None
    p.apply_chorus_send = lambda *a, **k: None
    p.apply_effect_send = lambda *a, **k: None
    CALLS.clear()
    p.route_to_megaphone_speakers("local", "C4", 300)
    return list(CALLS)


def run_drums(gp):
    from libs import drums
    d = drums.DrumAudio.__new__(drums.DrumAudio)
    d.am = FakeAudioMngr()
    d.gameplay = gp
    d.pad_defs = lambda kit: {"snare": ("snare", "drums/Snare.ogg", 1.0, 1.0)}
    d._tag_sounds = lambda *a, **k: None
    d.apply_effect_send = lambda *a, **k: None
    CALLS.clear()
    d.route_to_megaphone_speakers("local", "snare", 300)
    return list(CALLS)


def main():
    # Player stands at map center (50, 50).
    # Speaker A: close & clear (50, 60) - full volume
    # Speaker B: far beyond hearing_range (-200, -200) - must be silent
    # Speaker C: close but fully walled (100, 50) - must be silent
    spk_data = [
        make_speaker(50, 60, 0, 80),
        make_speaker(-200, -200, 0, 80),
        make_speaker(100, 50, 0, 80),
    ]
    occ = {0: 0.0, 1: 0.0, 2: 1.0}
    gp = FakeGP(FakeCam(50, 50, 0), FakeMegaphone(spk_data, occ_by_index(spk_data, occ)))

    piano_calls = run_piano(gp)
    assert len(piano_calls) == 1, f"piano: expected 1 audible speaker, got {len(piano_calls)}"
    pc = piano_calls[0]
    assert pc["x"] == 50 and pc["y"] == 60, f"piano should hit close speaker, got {pc}"
    assert pc["reference_distance"] == 16.0, f"piano ref distance should be hr*0.2=16, got {pc['reference_distance']}"
    assert pc["max_distance"] == 80.0, f"piano max distance should be hr=80, got {pc['max_distance']}"
    assert pc["volume"] > 0, "close clear speaker must be audible"
    print("PASS piano: close clear speaker audible, far/blocked speakers skipped, range applied")

    drum_calls = run_drums(gp)
    assert len(drum_calls) == 1, f"drums: expected 1 audible speaker, got {len(drum_calls)}"
    dc = drum_calls[0]
    assert dc["x"] == 50 and dc["y"] == 60, f"drums should hit close speaker, got {dc}"
    assert dc["reference_distance"] == 16.0 and dc["max_distance"] == 80.0
    assert dc["volume"] > 0
    print("PASS drums: close clear speaker audible, far/blocked speakers skipped, range applied")

    # Partial occlusion (wall edge): volume should be dampened but not zero.
    occ = {0: 0.5, 1: 1.0, 2: 1.0}
    gp2 = FakeGP(FakeCam(50, 50, 0), FakeMegaphone(spk_data, occ_by_index(spk_data, occ)))
    calls = run_piano(gp2)
    assert len(calls) == 1, f"partial occlusion: expected 1 speaker, got {len(calls)}"
    occ_data = [make_speaker(50, 60, 0, 80)]
    gp3 = FakeGP(FakeCam(50, 50, 0), FakeMegaphone(occ_data, occ_by_index(occ_data, {0: 0.0})))
    full_calls = run_piano(gp3)
    full_vol = full_calls[0]["volume"]
    assert calls[0]["volume"] < full_vol, \
        f"walled speaker should be quieter: {calls[0]['volume']} vs clear {full_vol}"
    print("PASS partial occlusion dampens volume proportionally")

    print("ALL PASS: PA-routed instruments follow distance + wall occlusion like voice/music")


if __name__ == "__main__":
    main()
