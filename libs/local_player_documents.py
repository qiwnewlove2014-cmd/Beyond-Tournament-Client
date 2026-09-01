"""Read the fixed public guides packaged beside the Client executable.

The Server still owns the /docs and /patchnotes commands and sends the
language picker.  A current Client handles that picker locally so the live
Server does not need a second copy of the public text files.  Only this fixed
allowlist is readable; packet data is never treated as a file path.
"""

from pathlib import Path
import sys

from .menu import Menu
from .menus import set_default_sounds
from .speech import speak


DOCUMENTS = {
    "docs": {
        "menu_title": "Player documentation — Choose language / คู่มือผู้เล่น — เลือกภาษา",
        "languages": {
            "en": {
                "filename": "docs.txt",
                "title": "Player documentation — English",
                "label": "English — Read the English guide",
            },
            "th": {
                "filename": "docs_th.txt",
                "title": "คู่มือผู้เล่น — ภาษาไทย",
                "label": "ภาษาไทย — อ่านคู่มือภาษาไทย",
            },
        },
    },
    "patchnotes": {
        "menu_title": "Player patch notes — Choose language / บันทึกอัปเดตเกม — เลือกภาษา",
        "languages": {
            "en": {
                "filename": "player_patch_notes.txt",
                "title": "Player patch notes — English",
                "label": "English — Read the patch notes",
            },
            "th": {
                "filename": "player_patch_notes_th.txt",
                "title": "บันทึกอัปเดตเกม — ภาษาไทย",
                "label": "ภาษาไทย — อ่านบันทึกอัปเดตเกม",
            },
        },
    },
}

SERVER_LANGUAGE_EVENTS = {
    "docs_language_select": "docs",
    "patchnotes_language_select": "patchnotes",
}


def document_root():
    """Return the source/package root without trusting the process CWD."""

    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def read_document(kind, language, root=None):
    """Return a fixed document title and lines, or raise a safe read error."""

    document = DOCUMENTS.get(kind)
    selected = document and document["languages"].get(language)
    if selected is None:
        raise ValueError("Unsupported player document selection")
    path = (Path(root) if root is not None else document_root()) / selected["filename"]
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError("Player document is empty")
    return selected["title"], text.splitlines()


def _show(gameplay, screen, replace):
    if replace and getattr(gameplay, "substates", None):
        gameplay.replace_last_substate(screen)
    else:
        gameplay.add_substate(screen)


def open_language_menu(game, gameplay, kind, replace=False):
    document = DOCUMENTS.get(kind)
    if document is None:
        return False

    language_menu = Menu(game, document["menu_title"], autoclose=False, parrent=gameplay)
    language_menu.menu_event = f"local_{kind}_language"

    def choose(language):
        if not open_reader(game, gameplay, kind, language, replace=True):
            speak(
                "This document is unavailable in the installed game files. "
                "ไม่พบเอกสารนี้ในไฟล์เกม กรุณาติดตั้งหรืออัปเดตเกมใหม่",
                True,
            )

    for language in ("en", "th"):
        selected = document["languages"][language]
        language_menu.add_item(selected["label"], lambda value=language: choose(value))
    language_menu.add_item("Close", gameplay.pop_last_substate)
    set_default_sounds(language_menu)
    _show(gameplay, language_menu, replace)
    return True


def open_reader(game, gameplay, kind, language, root=None, replace=True):
    """Open a local read-only document. Return False to allow Server fallback."""

    try:
        title, lines = read_document(kind, language, root=root)
    except (OSError, UnicodeError, ValueError):
        return False

    reader = Menu(game, title, autoclose=False, parrent=gameplay)
    reader.menu_event = f"local_{kind}_reader"
    for line in lines:
        reader.add_item(line or " ", lambda: None)
    reader.add_item(
        "Back — กลับไปเลือกภาษา" if language == "th" else "Back to language selection",
        lambda: open_language_menu(game, gameplay, kind, replace=True),
    )
    set_default_sounds(reader)
    _show(gameplay, reader, replace)
    return True


def handle_server_language_selection(game, gameplay, event_name, language):
    """Intercept the two fixed Server language menus when local files exist."""

    kind = SERVER_LANGUAGE_EVENTS.get(event_name)
    if kind is None or language not in ("en", "th"):
        return False
    return open_reader(game, gameplay, kind, language, replace=True)
