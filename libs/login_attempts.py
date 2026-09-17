"""Which ports a login tries, and what it says when nothing ever answers.

A first attempt can fail without anything being wrong at either end: a
first-hop router that drops the opening datagrams, a handshake that lands while
the server is still loading its maps, a stale NAT mapping on the way out. From
the player's chair every one of those is the same silence, and the old
"Connection error [timeout]" told them -- and us -- nothing about which one it
was. Retrying is what turns "it did not work" into "it worked on the second
try", and the failure that survives the retries is finally worth reporting.

Three rules keep the retry from making things worse than the silence:

- Only an attempt that got **no session at all** is retried. A refusal arrives
  as words on the screen ("wrong password", "this IP is banned", "client
  outdated") and is never retried, because trying again cannot change it.
- A login the server *answered* and then left quiet belongs to
  ``networking.Client.loop``, which owns that window and does not retry it: a
  server that is working is being worked on, and hammering it costs everybody.
- Every attempt gets its own fresh silence window (``login_timed_out``), so a
  genuinely slow server is still waited for for as long as it keeps talking.

The attempt list is one rule rather than two numbers: the endpoint, and the
port ``FALLBACK_OFFSET`` above it. The Server derives its second listener the
same way from its own primary port (``consts.FALLBACK_PORT``), so there is
nothing to embed and nothing to keep in sync -- and a login that only ever
tried one port would have no answer at all for a player whose way in is the
other one. A new socket on a *new local port* is half the point of retrying:
that is what repairs a stale NAT mapping, and on the second candidate the
remote port is different too, so a filter that drops one gets a fresh decision
to make about the other.

The offset is 2, not 1, because the port beside the game port is already this
project's own: the presence sound service listens on TCP 13001 (env
``PRESENCE_HTTP_PORT``, and the Windows installer opens it for Radmin). A
different protocol on one number does not conflict, but two stories sharing a
number is exactly how a future reader ends up asking why 13001 is sometimes
UDP -- so the game's second listener skips it.

The port a login last *got an answer on* is remembered and moved to the front
(``game`` reads and writes it through Options). It is only ever an ordering
hint: a remembered port that is not one of this endpoint's candidates is
ignored, so a stale setting -- or a hand-edited one -- cannot send anybody
anywhere.
"""

# How far above the configured port the fallback sits. The Server derives the
# same one from its own primary port, so the pair moves together; a login of
# one attempt is what this number being 0 would mean. See the module docstring
# for why it is not 1.
FALLBACK_OFFSET = 2

# Where the port that last answered is kept, as a plain key in Options. It
# holds a port, never a host, which is why it is not one of
# ``server_config.ENDPOINT_OPTION_KEYS``: a production build strips those, and
# a remembered port is exactly what a released build needs to keep -- an
# unreadable value is ignored below, and it can only ever reorder two ports
# this same endpoint already implies.
PREFERRED_OPTION_KEY = "login_port"


def fallback_port(port):
    """The port above this one, or None when there is no room for it."""
    port = int(port)
    candidate = port + FALLBACK_OFFSET
    return candidate if 1 <= candidate <= 65535 else None


def remembered_port(value):
    """The port a login last got an answer on, when it is a port at all."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def candidate_ports(port, preferred=None):
    """The ports one login walks through, in order.

    The endpoint first, then the port above it -- with the port that last
    answered moved to the front, so a player whose way in is the fallback does
    not pay a silence window on every login to rediscover it. An endpoint with
    no room above it (65535, 65534) is simply the one port it is.
    """
    port = int(port)
    above = fallback_port(port)
    if above is None or above == port:
        return (port,)
    if remembered_port(preferred) == above:
        return (above, port)
    return (port, above)


def retry_notice(attempt, total):
    """What the player hears as a retry starts, counted from one."""
    total = max(int(total), 1)
    attempt = min(max(int(attempt), 1), total)
    return f"Trying the connection again ({attempt} of {total})."


def silence_message(tries, ports):
    """The last word when no attempt was ever answered.

    It says what happened (nothing was answered at all), what that means (the
    connection rather than the account), and where the answer is (the staff
    connection log), because the player is the only person who can tell us the
    attempt happened. Both ports are named, because which of them was silent is
    the first thing the reader needs to know.
    """
    trial = "try" if int(tries) == 1 else "tries"
    listed = " and ".join(str(value) for value in dict.fromkeys(ports))
    where = f"port {listed}" if " and " not in listed else f"ports {listed}"
    return (
        f"No answer from the server after {tries} {trial} on {where}. "
        "Nothing was ever answered, so this is the connection rather than your account. "
        "Staff can read the connection log to see whether the attempt arrived."
    )
