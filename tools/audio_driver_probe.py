"""Measure EFX survival across `device.reset()` and `device.reopen()`.

This is the one question the device-free tests cannot answer, and the one a
player's "reverb quietly stopped after an hour" report turns on. The project
has measured `device.reset()` (the sound-system picker) before; `reopen()` --
which the frame watchdog calls after a device disconnect or rename -- has not
been measured at all.

Nothing is played: no buffer is attached, so the probe is silent. It only asks
OpenAL for a slot, attaches an effect, sends a source into it, and then reads
the slot's own gain back (a real `alGetAuxiliaryEffectSlotf`, not a Python
shadow) after each device call. An exception at any step is the measurement.

Usage (from `client/`):

    python tools/audio_driver_probe.py
    python tools/audio_driver_probe.py --no-reopen

Exit codes: 0 = measurements completed (whether or not EFX survived),
2 = no audio device could be opened, 3 = the probe itself failed.
"""

import argparse
import sys
import traceback

import cyal
import cyal.exceptions
import cyal.efx


def _open():
    device = cyal.Device(name=cyal.util.get_default_all_device_specifier())
    context = cyal.Context(
        device,
        make_current=True,
        mono_sources=64,
        stereo_sources=64,
        max_auxiliary_sends=64,
    )
    efx = cyal.efx.EfxExtension(context)
    return device, context, efx


def _probe_state(efx, slot, source, label, results):
    """Read the slot back for real, then re-send and read it again."""
    try:
        gain = slot.gain
        results.append((f"{label}: slot.gain", True, f"{gain:.2f}"))
    except Exception as error:
        results.append((f"{label}: slot.gain", False, type(error).__name__))
        return
    try:
        efx.send(source, 0, slot)
        results.append((f"{label}: send", True, ""))
    except Exception as error:
        results.append((f"{label}: send", False, type(error).__name__))
    try:
        again = slot.gain
        results.append((f"{label}: slot.gain after send", True, f"{again:.2f}"))
    except Exception as error:
        results.append((f"{label}: slot.gain after send", False,
                        type(error).__name__))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-reopen", action="store_true",
                        help="skip the reopen measurement")
    args = parser.parse_args(argv)

    try:
        device, context, efx = _open()
    except Exception as error:
        print(f"NO DEVICE: {type(error).__name__}: {error}")
        return 2

    results = []
    try:
        try:
            granted = efx.max_auxiliary_sends
            results.append(("context: max_auxiliary_sends", True, str(granted)))
        except Exception as error:
            results.append(("context: max_auxiliary_sends", False,
                            type(error).__name__))

        slot = efx.gen_auxiliary_effect_slot()
        effect = efx.gen_effect(type="EAXREVERB")
        effect.set("decay_time", 2.0)
        effect.set("diffusion", 0.9)
        slot.effect = effect
        slot.gain = 0.9
        source = context.gen_source(position=(1.0, 2.0, 3.0))
        results.append(("objects: slot+effect+source", True, ""))

        _probe_state(efx, slot, source, "baseline", results)

        try:
            device.reset()
            results.append(("device.reset()", True, ""))
        except Exception as error:
            results.append(("device.reset()", False, type(error).__name__))
        _probe_state(efx, slot, source, "after reset", results)

        if not args.no_reopen:
            try:
                name = device.name
            except Exception:
                name = None
            try:
                device.reopen(name=name)
                results.append(("device.reopen()", True, name or "<default>"))
            except Exception as error:
                results.append(("device.reopen()", False, type(error).__name__))
            _probe_state(efx, slot, source, "after reopen", results)
    except Exception:
        traceback.print_exc()
        print("\nPROBE FAILED before it could measure the device calls.")
        return 3
    finally:
        try:
            device.close()
        except Exception:
            pass

    width = max(len(name) for name, _ok, _detail in results) + 2
    for name, ok, detail in results:
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {name:<{width}} {detail}")

    survived = all(ok for name, ok, _detail in results
                   if "after " in name)
    print("\nEFX after device calls:", "survived" if survived else "NOT intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
