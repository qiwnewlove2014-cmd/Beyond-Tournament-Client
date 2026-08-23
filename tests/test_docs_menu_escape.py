"""Regression coverage for Escape in bilingual /docs reader menus."""

import os
import sys
import unittest

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.menu import Menu


class _Game:
    direct_soundgroup = object()


class _Parent:
    def __init__(self):
        self.pop_count = 0

    def pop_last_substate(self):
        self.pop_count += 1


class DocsMenuEscapeTests(unittest.TestCase):
    def test_escape_uses_final_bilingual_back_not_backspace_document_row(self):
        selected = []
        parent = _Parent()
        docs_menu = Menu(_Game(), "คู่มือผู้เล่น — ภาษาไทย", parrent=parent)
        docs_menu.items = [
            ("Backspace — เปิดเมนูหลัก", lambda: selected.append("document-row"), None),
            ("บรรทัดคู่มือภาษาไทย", lambda: selected.append("document-row"), None),
            ("Back — กลับไปเลือกภาษา", lambda: selected.append("back"), None),
        ]
        docs_menu.menu_type = "normal"

        escape = pygame.event.Event(
            pygame.KEYDOWN,
            key=pygame.K_ESCAPE,
            mod=pygame.KMOD_NONE,
        )
        docs_menu.update((escape,))

        self.assertEqual(selected, ["back"])
        self.assertEqual(parent.pop_count, 0)


if __name__ == "__main__":
    unittest.main()
