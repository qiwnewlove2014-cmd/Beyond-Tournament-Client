"""The one helper both menu layers use: `stand_on`.

It puts a rebuilt page's cursor back on the line that was just pressed. Kept
apart so `menus.py` and `menu_pages/settings.py` can both import it without a
cycle; `menus.py` re-exports it for the tests and tools that named it there.
"""


def stand_on(menu, prefix):
    """Put a rebuilt menu's cursor back on the line whose text starts `prefix`.

    A page that changes shape around a press -- the Auto-Tune dials exist only
    while the switch above them is on -- cannot be updated in place: it is
    popped and built again. The cursor would land on the first line of a page
    the player did not open from the top, so it is put back on the switch they
    just pressed instead, and the line is read with its new label.
    """
    if menu is None:
        return
    for index, item in enumerate(menu.items):
        text = item[0]() if callable(item[0]) else item[0]
        if str(text).startswith(prefix):
            menu.pos = index
            break
    menu.speak_current_item()
