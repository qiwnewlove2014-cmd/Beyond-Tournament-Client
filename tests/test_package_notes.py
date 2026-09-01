"""Public-document contract for the legacy validator; only temporary files are used."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


CLIENT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bt_finalize_package", CLIENT / "tools/finalize_client_package.py")
finalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finalizer)


class PackageNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bt-package-notes-test-")
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name) / "package"
        self.project = Path(self.temp.name) / "client"
        self.package.mkdir()
        self.project.mkdir()
        for name in finalizer.PLAYER_DOCUMENTS:
            (self.package / name).write_text("Public player document", encoding="utf-8")

    def verify(self):
        # Other runtime validation is outside this text-packaging regression.
        require = finalizer._require_file
        def notes_only(label, path, failures, minimum_size=1):
            if label in ("Player guide", "Player patch notes"):
                require(label, path, failures, minimum_size)
        with mock.patch.object(finalizer, "_require_file", side_effect=notes_only), \
                mock.patch.object(finalizer, "_require_glob"), \
                mock.patch.object(finalizer, "_require_tree"), \
                mock.patch.object(finalizer, "_compare_tree"), \
                contextlib.redirect_stdout(io.StringIO()):
            finalizer.verify_package(self.package, self.project, yt_dlp_source=self.project)

    def test_documents_without_technical_changelog_are_accepted(self):
        self.verify()

    def test_each_language_is_required_without_changelog_fallback(self):
        for name in finalizer.PLAYER_DOCUMENTS:
            with self.subTest(name=name):
                path = self.package / name
                saved = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(finalizer.PackageValidationError, "Player (?:guide|patch notes)"):
                    self.verify()
                path.write_bytes(saved)

    def test_empty_and_invalid_text_are_rejected(self):
        for name in finalizer.PLAYER_DOCUMENTS:
            path = self.package / name
            saved = path.read_bytes()
            for data in (b"", b" \n\t", b"\xff"):
                with self.subTest(name=name, data=data):
                    path.write_bytes(data)
                    with self.assertRaises(finalizer.PackageValidationError):
                        self.verify()
                    self.assertEqual(path.read_bytes(), data)
            path.write_bytes(saved)

    def test_technical_game_changelog_is_rejected_without_deleting_it(self):
        unwanted = self.package / "CHANGELOG.TXT"
        unwanted.write_text("Technical notes", encoding="utf-8")
        with self.assertRaisesRegex(finalizer.PackageValidationError, "technical game changelog"):
            self.verify()
        self.assertEqual(unwanted.read_text(encoding="utf-8"), "Technical notes")


if __name__ == "__main__":
    unittest.main()
