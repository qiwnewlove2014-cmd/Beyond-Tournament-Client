"""Which rendering the sound card grants, and how to ask for another.

The game has always asked the driver for one thing -- ``hrtf.use(model)``, a
binaural HRTF set -- and never looked at what the card answered.  This module is
the other half of that choice: a listener may ask for a speaker system instead,
and is told what the card actually gave.

Everything here is OpenAL Soft through cyal: no ctypes, no second DLL.  What
the shipped build was measured to do (see the cinema/RoomSim skills for how
this project proves such things):

* one output mode per context -- ``ALC_STEREO_BASIC_SOFT`` 0x19AE,
  ``ALC_STEREO_UHJ_SOFT`` 0x19AF, ``ALC_STEREO_HRTF_SOFT`` 0x19B2,
  ``ALC_QUAD_SOFT`` 0x1503, ``ALC_SURROUND_5_1_SOFT`` 0x1504,
  ``ALC_SURROUND_6_1_SOFT`` 0x1505, ``ALC_SURROUND_7_1_SOFT`` 0x1506.
* ``device.reset(**kwargs)`` is ``alcResetDeviceSOFT``, and it does **not** tear
  the audio graph down: measured at 17-29 ms with the context object, its
  sources and their buffers still valid and ``alGetError`` clear.  A switch
  therefore never needs a stream rebuilt or a song re-seeked.
* **asking is not granting.**  On a stereo endpoint, requesting 7.1 answers
  success -- no ALC error at all -- and grants ``ALC_STEREO_BASIC_SOFT`` *with
  HRTF switched off*.  That silent downgrade would leave a listener in plain
  stereo with the HRTF set they had chosen gone, so every apply reads the
  granted mode back and restores the previous rendering when it differs.
* whether the card recommends HRTF at all is the *driver's* decision, and it can
  be asked rather than forced: ``ALC_DONT_CARE_SOFT`` (2) lets it choose, and
  ``get_attrs().hrtf_status_soft`` is its own word about what it decided
  (4 = "headphones detected"), which no client-side guess can match.
* the surround modes a *device* grants are the machine's speaker configuration,
  not the engine's limit: the same machine whose endpoint refused 7.1 rendered
  7.1 and third-order ambisonics through an ``ALC_SOFT_loopback`` device.

The keys here are what a saved option holds; the labels are what a listener
reads.  ``default`` is deliberately first and is the option's default: it is the
behaviour the game shipped with (force the saved HRTF model) and it must stay
byte-for-byte unchanged for anybody who never opens this picker.
"""

# ALC_SOFT_output_mode values.  Fixed by the extension, and verified against the
# shipped build by ``test_sound_system`` through the driver's own
# ``alcGetEnumValue`` -- the one place a magic number here could ever drift.
MODE_MONO = 0x1500
MODE_STEREO = 0x1501
MODE_QUAD = 0x1503
MODE_SURROUND_5_1 = 0x1504
MODE_SURROUND_6_1 = 0x1505
MODE_SURROUND_7_1 = 0x1506
MODE_STEREO_BASIC = 0x19AE
MODE_STEREO_UHJ = 0x19AF
MODE_STEREO_HRTF = 0x19B2

# ALC_DONT_CARE_SOFT: "pick for me" (the same value ALC_HRTF_DENIED_SOFT uses,
# in a different enum domain -- it belongs in the HRTF attribute here).
DONT_CARE = 2

MODE_LABELS = {
    MODE_MONO: "Mono",
    MODE_STEREO: "Stereo",
    MODE_STEREO_BASIC: "Stereo",
    MODE_STEREO_UHJ: "Stereo UHJ (super stereo)",
    MODE_STEREO_HRTF: "Headphone HRTF",
    MODE_QUAD: "Quadraphonic (4 speakers)",
    MODE_SURROUND_5_1: "5.1 Surround",
    MODE_SURROUND_6_1: "6.1 Surround",
    MODE_SURROUND_7_1: "7.1 Surround",
}

# The driver's own word about HRTF (ALC_HRTF_STATUS_SOFT).  Reported as the card
# said it rather than translated into a claim of our own.
STATUS_LABELS = {
    0: "HRTF is off",
    1: "HRTF is on",
    2: "it refused HRTF",
    3: "HRTF is required",
    4: "headphones detected",
    5: "this format cannot carry HRTF",
}

# key -> (label, what to ask the device for).  ``None`` means "nothing but the
# saved HRTF model", which is the shipped behaviour; ``DONT_CARE`` means "let
# the card decide".  Order is the picker's order.
SYSTEMS = {
    "default": ("Use the HRTF model chosen above", None),
    "auto": ("Automatic - let the sound card choose", DONT_CARE),
    "stereo": ("Stereo, with no headphone processing", MODE_STEREO),
    "uhj": ("Stereo UHJ (super stereo)", MODE_STEREO_UHJ),
    "quad": ("Quadraphonic, 4 speakers", MODE_QUAD),
    "surround51": ("5.1 Surround", MODE_SURROUND_5_1),
    "surround61": ("6.1 Surround", MODE_SURROUND_6_1),
    "surround71": ("7.1 Surround", MODE_SURROUND_7_1),
}

# What counts as granted for each request.  ALC_STEREO_SOFT is answered as
# ALC_STEREO_BASIC_SOFT (measured), so a strict equality check would report a
# refusal for a mode the card did in fact give.
GRANTED_AS = {
    "stereo": (MODE_STEREO, MODE_STEREO_BASIC),
    "uhj": (MODE_STEREO_UHJ,),
    "quad": (MODE_QUAD,),
    "surround51": (MODE_SURROUND_5_1,),
    "surround61": (MODE_SURROUND_6_1,),
    "surround71": (MODE_SURROUND_7_1,),
}

DEFAULT_SYSTEM = "default"
DEFAULT_HRTF_MODEL = "oalsoft_hrtf_48000"
OPTION_KEY = "sound_system"
REFUSAL_OPTION_KEY = "sound_system_refused"


class State():
    """What the card is doing right now, and what it said about HRTF."""

    __slots__ = ("mode", "hrtf", "status", "model")

    def __init__(self, mode=None, hrtf=False, status=None, model=None):
        self.mode = mode
        self.hrtf = hrtf
        self.status = status
        self.model = model

    def label(self):
        """How this rendering is said out loud."""
        if self.mode is None:
            # A driver without ALC_SOFT_output_mode: HRTF is the only axis we
            # can see, so name that and nothing we cannot see.
            text = "Headphone HRTF" if self.hrtf else "Stereo"
        else:
            text = MODE_LABELS.get(self.mode, f"sound mode {self.mode:#x}")
        if self.hrtf and self.model and self.model not in text:
            text += f" ({self.model})"
        return text

    def describe(self):
        """The rendering plus the card's own word about its HRTF decision."""
        text = self.label()
        word = STATUS_LABELS.get(self.status)
        if word:
            text += f" - {word}"
        return text


class Attempt():
    """One switch: whether the card granted it, and how to say what happened.

    ``label`` is what the card is rendering when the call returns (so a refused
    request reads as what the listener still has), and ``gave`` is what the card
    answered with for a request it would not grant -- the picker says that back
    ("7.1 Surround - this machine gives Stereo") instead of offering the same
    choice again next time.
    """

    __slots__ = ("key", "granted", "label", "detail", "gave")

    def __init__(self, key, granted, label, detail, gave=None):
        self.key = key
        self.granted = bool(granted)
        self.label = label
        self.detail = detail
        self.gave = gave

    def __repr__(self):
        return f"Attempt({self.key!r}, granted={self.granted}, {self.detail!r})"


class OutputSystem():
    """The device's rendering, read and asked for through one object.

    ``context`` and ``hrtf`` are cyal's (``cyal.Context`` and
    ``cyal.hrtf.HrtfExtension``); a test passes fakes of the same shape.
    """

    def __init__(self, context, hrtf):
        self.context = context
        self.hrtf = hrtf

    @property
    def device(self):
        return self.context.device

    # -- reading ------------------------------------------------------------

    def supported(self):
        """Does this build/driver let a context choose its output mode?"""
        try:
            return bool(self.device.is_extension_present(b"ALC_SOFT_output_mode"))
        except Exception:
            return False

    def state(self):
        """The granted rendering.  Never raises: an unreadable attribute is None."""
        attrs = self.context.get_attrs()
        return State(
            mode=_attr(attrs, "output_mode_soft"),
            hrtf=bool(_attr(attrs, "hrtf_soft")),
            status=_attr(attrs, "hrtf_status_soft"),
            model=self.hrtf.current_model,
        )

    def describe(self):
        return self.state().describe()

    def model(self):
        try:
            return self.hrtf.current_model
        except Exception:
            return None

    # -- asking -------------------------------------------------------------

    def start(self, key, model=DEFAULT_HRTF_MODEL):
        """Apply the saved choice -- what startup and a device change do.

        ``default`` is exactly what the game did before this module existed:
        force the saved HRTF model and change nothing else.
        """
        if key == DEFAULT_SYSTEM or key not in SYSTEMS:
            self.hrtf.use(model)
            return Attempt(DEFAULT_SYSTEM, True, self.state().label(),
                           f"using HRTF model {model}")
        if not self.supported():
            # The saved choice cannot be honoured by this build: keep the sound
            # the game always had and say so, rather than going quiet about it.
            self.hrtf.use(model)
            return Attempt(key, False, self.state().label(),
                           "this sound card cannot be asked for another output "
                           f"mode, so HRTF model {model} stays")
        return self.apply(key, model=model, previous=DEFAULT_SYSTEM)

    def apply(self, key, model=DEFAULT_HRTF_MODEL, previous=DEFAULT_SYSTEM):
        """Ask the card for ``key`` and believe only what it answers back.

        A request the card cannot honour restores ``previous`` -- it must never
        leave a listener in a downgraded rendering that they did not choose.
        """
        if key == DEFAULT_SYSTEM:
            self.hrtf.use(model)
            return Attempt(key, True, self.state().label(),
                           f"using HRTF model {model}")
        if key not in SYSTEMS:
            return Attempt(key, False, self.state().label(),
                           f"unknown sound system {key!r}")
        if not self.supported():
            return Attempt(key, False, self.state().label(),
                           "this sound card cannot be asked for another output mode")

        label, request = SYSTEMS[key]
        try:
            self._ask(request)
        except Exception as error:
            self._restore(previous, model)
            return Attempt(key, False, self.state().label(),
                           f"could not ask the sound card for {label}: {error}")

        state = self.state()
        granted = state.mode is not None and (
            key == "auto" or state.mode in GRANTED_AS[key]
        )
        if granted:
            detail = (f"the sound card chose {state.label()}" if key == "auto"
                      else f"using {label}")
            return Attempt(key, True, state.label(), detail)

        gave = state.label()
        self._restore(previous, model)
        return Attempt(
            key, False, self.state().label(),
            f"{label} is not available on this sound card: it gave {gave} "
            "instead, so the sound stays as it was",
            gave=gave,
        )

    # -- internals ----------------------------------------------------------

    def _ask(self, request):
        if request is DONT_CARE:
            self.device.reset(hrtf_soft=DONT_CARE)
        else:
            # HRTF and a speaker system are one mode, not two: asking for the
            # system has to give HRTF up in the same call, or the driver keeps
            # the binaural rendering and the request is a no-op.
            self.device.reset(hrtf_soft=0, output_mode_soft=request)

    def _restore(self, previous, model):
        """Put the machine back where it was after a request it would not grant."""
        try:
            if previous in (None, DEFAULT_SYSTEM) or previous not in SYSTEMS:
                self.hrtf.use(model)
            else:
                self._ask(SYSTEMS[previous][1])
        except Exception:
            # Restoring is best-effort; the caller reports the refusal either
            # way, and a device that cannot be re-asked is reported by the
            # state the menu reads next.
            pass


def _attr(attrs, name):
    try:
        value = getattr(attrs, name)
    except AttributeError:
        return None
    return None if value is None else int(value)


def menu_entries(system, refusals=None):
    """The picker's lines: ``(key, label)`` in the order a listener reads them.

    A system this machine has already refused carries what it gave instead, so
    the picker teaches rather than offering a choice that cannot be honoured.
    Without a usable device only ``default`` is offered -- there is nothing
    else that could be granted.
    """
    refusals = refusals or {}
    if system is None or not system.supported():
        return [(DEFAULT_SYSTEM, SYSTEMS[DEFAULT_SYSTEM][0])]
    entries = []
    for key, (label, _) in SYSTEMS.items():
        gave = refusals.get(key)
        if gave:
            label = f"{label} - this machine gives {gave}"
        entries.append((key, label))
    return entries
