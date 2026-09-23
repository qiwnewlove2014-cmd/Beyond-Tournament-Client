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

An attempt is an *address* as well as a port, because a name is the one part of
it that can fail on its own: a filtering or family resolver, an ad-blocking DNS
or a router that blocks the name answers every other name on the machine and not
this one, and a name cannot be retried into working. The release build embeds
the address it resolved for the endpoint in the pack
(``server_config.get_fallback_addresses``), and a login that cannot look the name
up walks to it instead -- a door with no DNS in front of it. The name is still
first, and still the only thing that survives the server moving, so a build
whose backup has gone stale loses the wait of one candidate and nothing else.

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


def candidate_addresses(host, port, addresses=(), preferred=None):
    """Every ``(host, port)`` one login walks through, in order.

    The endpoint's own name first -- the address it answers with is its own
    business, and the name is the one thing that keeps working when the server
    moves -- and then each backup address, because a backup is only ever for the
    login where the name was no use at all. Each address's ports are the same
    rule as the endpoint's own (``candidate_ports``), so the walk has one shape.

    A backup that *is* the endpoint, or that repeats one already listed, is not
    walked twice: two doors that are the same door are one door. Whether a backup
    turns out to be the address the name resolves to is not known here -- that
    answer belongs to the transport (``networking.Client.resolved_host``) -- so a
    login skips that one when it reaches it (``candidate_available``).

    With no addresses this is exactly ``candidate_ports`` for the endpoint, so a
    build that carries no backup walks precisely the doors it always did.
    """
    pairs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    repeated = {str(host).lower()}
    for index, name in enumerate((host, *addresses)):
        if not isinstance(name, str) or not name:
            continue
        if index and name.lower() in repeated:
            continue
        repeated.add(name.lower())
        for candidate_port in candidate_ports(port, preferred):
            pair = (name, candidate_port)
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
    return tuple(pairs)


def is_backup(candidate, host):
    """Whether a candidate is a backup address rather than the endpoint's name.

    It is the fact a report carries (``attempt_report``) and the fact a sentence
    needs (``resolution_message``): "the backup address was tried as well" is
    only true when a walk really left the name behind.
    """
    return str(candidate[0]).lower() != str(host).lower()


def candidate_available(candidate, unresolvable=(), dialled=()):
    """Whether one candidate is still worth dialling, given what is known now.

    Two facts retire a candidate. A *name* that could not be looked up: asking
    the same name answers the same thing, and a login that is about to fall back
    to an address must not spend another silence window learning it again. And a
    *door* this login has already dialled: a backup that turns out to be what
    the endpoint's name resolved to is the same door, and dialling it again buys
    nothing but the wait.

    A door is a *(host, port)* pair, and that is the whole difference between
    retiring one and retiring the other: an address dialled on the configured
    port says nothing about the port above it. A filter that drops one port and
    passes the other is the reason the second port is walked at all, and it is
    no less true of an address a login fell back to than of the name it fell
    back from -- so the walk keeps both ports of a backup address, and a backup
    that *is* the name's own address is still recognised door by door.
    """
    name = str(candidate[0]).lower()
    if name in {str(unresolvable_name).lower() for unresolvable_name in unresolvable}:
        return False
    return (name, candidate[1]) not in {
        (str(host).lower(), port) for host, port in dialled
    }


def diallable_count(candidates, unresolvable=(), dialled=()):
    """How many of *candidates* a login will still dial.

    This is the number a retry notice counts to: a candidate that is certain
    not to open (see ``candidate_available``) is not one of the attempts, and telling
    a player it is would make the one number they can report wrong.
    """
    return sum(
        1
        for candidate in candidates
        if candidate_available(candidate, unresolvable, dialled)
    )


def doors_before(candidates, index, unresolvable=()):
    """How many doors a login has been through before *candidates[index]*.

    A door is a candidate this machine could really knock on, and the ports of a
    name with no answer are not one: the name was walked past without a socket,
    so nothing was sent and no player waited on it. Counting those entries is
    what let the retry notice tell a player "2 of 2" with a single door left to
    dial -- and what a player reads out loud to staff is worth more than the
    list position it was derived from.
    """
    retired = {str(name).lower() for name in unresolvable}
    return sum(
        1
        for candidate in candidates[:index]
        if str(candidate[0]).lower() not in retired
    )


def retry_position(candidates, index, unresolvable=(), dialled=()):
    """The ``(attempt, total)`` a retry notice reads for *candidates[index]*.

    ``attempt`` is which door this is, counted over doors rather than over list
    entries, and ``total`` is how many doors this login will have been through
    if they all stay silent: the ones behind it, plus the ones still worth
    dialling. A candidate that would not open counts -- it was tried -- while
    the ports of a name with no answer do not, because nothing was ever sent to
    them (``doors_before``).

    The two numbers are what a player reports, so they have to describe the same
    walk the log's ``tried=`` list does: a login that says "2 of 2" and then
    dials one door is a sentence no reader can check.
    """
    attempted = doors_before(candidates, index, unresolvable)
    ahead = diallable_count(candidates[index:], unresolvable, dialled)
    return attempted + 1, attempted + ahead


def retry_notice(attempt, total):
    """What the player hears as a retry starts, counted from one."""
    total = max(int(total), 1)
    attempt = min(max(int(attempt), 1), total)
    return f"Trying the connection again ({attempt} of {total})."


def silence_message(tries, ports, backup_tried=False):
    """The last word when no attempt was ever answered.

    It says what happened (nothing was answered at all), what that means (the
    connection rather than the account), and where the answer is (the staff
    connection log), because the player is the only person who can tell us the
    attempt happened. Both ports are named, because which of them was silent is
    the first thing the reader needs to know. A walk that also tried a backup
    address says so, or the reader would go looking for a cause on the ports
    alone when the name was the door that did not open.
    """
    trial = "try" if int(tries) == 1 else "tries"
    listed = " and ".join(str(value) for value in dict.fromkeys(ports))
    where = f"port {listed}" if " and " not in listed else f"ports {listed}"
    message = (
        f"No answer from the server after {tries} {trial} on {where}. "
        "Nothing was ever answered, so this is the connection rather than your account. "
    )
    if backup_tried:
        message += "The server's backup address was tried as well. "
    return message + "Staff can read the connection log to see whether the attempt arrived."


def resolution_message(backup_tried=False, public_resolver_tried=False):
    """The last word when the server's *name* could not be looked up at all.

    A name with no answer is not a connection that was refused, and saying which
    one it was is the whole point: this is the failure where the player's machine
    never sent a packet, so nothing on the Server can show it, the connection log
    has nothing to read, and no amount of retrying changes it. The sentence names
    the likely owner of the problem and the cheapest thing to try, and it names
    the endpoint never -- a released build says nothing about where the official
    server is.

    ``public_resolver_tried`` says a second, independent way of answering the
    name was already used before this sentence was reached (a public resolver
    over HTTPS, asked by address, so the question itself needed no DNS). When it
    was, "this computer's DNS" is no longer the whole story -- the advice is the
    same, but the reader should know both ways out failed rather than one -- and
    the wording changes with it instead of claiming an attempt nobody made.
    """
    looked_up_by = (
        "this computer or by a public resolver over the internet"
        if public_resolver_tried
        else "this computer"
    )
    message = f"The server's name could not be looked up by {looked_up_by}."
    if backup_tried:
        return (
            message
            + " The backup address was tried instead, and it did not answer either,"
            + " so this is the connection rather than your account: try a mobile"
            + " hotspot."
        )
    whose = (
        "this computer's connection rather than your account"
        if public_resolver_tried
        else "this computer's DNS rather than the server"
    )
    return (
        message
        + f" Nothing was ever sent, so this is {whose}: try a mobile hotspot, or set"
        + " this computer's DNS to 8.8.8.8."
    )


def attempt_report(
    host,
    tried=(),
    resolved=None,
    answered=None,
    lookup_failed=False,
    lookup=None,
    backup_tried=False,
    error=None,
):
    """One line for the client log, for a login that opened or did not.

    The Server can only ever see the attempts that *arrived*, and the failure
    that has cost this project the most is the one where nothing arrived at all:
    the player is told to ask staff to read the connection log, and there is
    nothing in it to read. This end is the only witness, so the line carries what
    the machine did -- the name that was asked for, what it made of it, how the
    address was found (``lookup=literal`` for an address dialled as given,
    ``local`` for this machine's resolver, ``public`` for a public one asked over
    HTTPS, ``none`` when no way of answering the name worked), every door that
    was dialled and how it ended -- in a shape a person can read and a tool can
    still parse. One line per login, never one per candidate.
    """
    parts = [f"host={host or 'unknown'}"]
    if resolved:
        parts.append(f"resolved={resolved}")
    elif lookup_failed:
        parts.append("resolved=none")
    if lookup:
        parts.append(f"lookup={lookup}")
    if answered:
        parts.append(f"answered={answered[0]}:{answered[1]}")
        parts.append("outcome=answered")
    elif lookup_failed:
        parts.append("outcome=name-not-looked-up")
    else:
        parts.append("outcome=no-answer")
    if tried:
        parts.append("tried=" + ",".join(f"{name}:{port}" for name, port in tried))
    if backup_tried:
        parts.append("backup=yes")
    if error is not None:
        parts.append("error=" + _one_line(error))
    return "[LOGIN] " + " ".join(parts)


def _one_line(text, limit=200):
    """An error's words without the line breaks that would split a log line."""
    return " ".join(str(text).split())[:limit]
