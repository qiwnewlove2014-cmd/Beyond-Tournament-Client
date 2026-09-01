import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from libs import local_player_documents as documents
from libs.event_handeler import EventHandeler


class _Gameplay:
    def __init__(self):
        self.substates = [object()]
        self.replacements = []
        self.additions = []

    def replace_last_substate(self, screen):
        self.substates[-1] = screen
        self.replacements.append(screen)

    def add_substate(self, screen):
        self.substates.append(screen)
        self.additions.append(screen)

    def pop_last_substate(self):
        return self.substates.pop()


class LocalPlayerDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bt-local-docs-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = SimpleNamespace(direct_soundgroup=object())
        self.gameplay = _Gameplay()
        for name in (
            "docs.txt",
            "docs_th.txt",
            "player_patch_notes.txt",
            "player_patch_notes_th.txt",
        ):
            (self.root / name).write_text(f"First line of {name}\n\nLast line", encoding="utf-8")

    def open(self, event_name, language):
        with patch.object(documents, "document_root", return_value=self.root), patch.object(
            documents, "set_default_sounds"
        ):
            return documents.handle_server_language_selection(
                self.game, self.gameplay, event_name, language
            )

    def test_docs_and_patch_notes_open_from_fixed_local_files(self):
        cases = (
            ("docs_language_select", "en", "First line of docs.txt"),
            ("docs_language_select", "th", "First line of docs_th.txt"),
            ("patchnotes_language_select", "en", "First line of player_patch_notes.txt"),
            ("patchnotes_language_select", "th", "First line of player_patch_notes_th.txt"),
        )
        for event_name, language, first_line in cases:
            with self.subTest(event_name=event_name, language=language):
                self.assertTrue(self.open(event_name, language))
                reader = self.gameplay.replacements[-1]
                self.assertEqual(reader.items[0][0], first_line)
                self.assertEqual(reader.items[1][0], " ")
                self.assertTrue(reader.items[-1][0].startswith("Back"))
                self.assertEqual(reader.menu_event.endswith("_reader"), True)

    def test_missing_local_file_returns_false_for_server_fallback(self):
        (self.root / "docs.txt").unlink()
        self.assertFalse(self.open("docs_language_select", "en"))
        self.assertEqual(self.gameplay.replacements, [])

    def test_network_values_cannot_select_a_path(self):
        before = list(self.gameplay.replacements)
        self.assertFalse(self.open("docs_language_select", "../secret.txt"))
        self.assertFalse(self.open("unknown_event", "en"))
        self.assertEqual(self.gameplay.replacements, before)

    def test_back_replaces_reader_with_local_language_menu(self):
        with patch.object(documents, "document_root", return_value=self.root), patch.object(
            documents, "set_default_sounds"
        ):
            self.assertTrue(documents.open_reader(
                self.game, self.gameplay, "docs", "th", replace=True
            ))
            self.gameplay.replacements[-1].items[-1][1]()
        language_menu = self.gameplay.replacements[-1]
        self.assertIn("เลือกภาษา", language_menu.title)
        self.assertEqual([item[0] for item in language_menu.items[-1:]], ["Close"])

    def test_server_language_menu_uses_local_reader_before_sending_selection(self):
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(direct_soundgroup=object(), menu_memory={})
        handler.gameplay = _Gameplay()
        handler.client = SimpleNamespace(send=Mock())
        data = {
            "event": "docs_language_select",
            "title": "Choose language",
            "options": [{"title": "English", "value": "en", "close": True}],
        }
        with patch("libs.event_handeler.menus.set_default_sounds"), patch(
            "libs.event_handeler.local_player_documents.handle_server_language_selection",
            return_value=True,
        ) as local_open:
            handler.make_menu(data)
            handler.gameplay.additions[-1].items[0][1]()

        local_open.assert_called_once_with(
            handler.game, handler.gameplay, "docs_language_select", "en"
        )
        handler.client.send.assert_not_called()

    def test_missing_local_reader_keeps_existing_server_fallback(self):
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(direct_soundgroup=object(), menu_memory={})
        handler.gameplay = _Gameplay()
        handler.client = SimpleNamespace(send=Mock())
        data = {
            "event": "patchnotes_language_select",
            "title": "Choose language",
            "options": [{"title": "ภาษาไทย", "value": "th", "close": True}],
        }
        with patch("libs.event_handeler.menus.set_default_sounds"), patch(
            "libs.event_handeler.local_player_documents.handle_server_language_selection",
            return_value=False,
        ):
            handler.make_menu(data)
            handler.gameplay.additions[-1].items[0][1]()

        handler.client.send.assert_called_once()
        channel, event_name, payload = handler.client.send.call_args.args
        self.assertEqual((event_name, payload), ("patchnotes_language_select", {"value": "th"}))


if __name__ == "__main__":
    unittest.main()
