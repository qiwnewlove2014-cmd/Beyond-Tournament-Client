import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from libs import menus, options, server_config, vfs
from tools import pack_data


class ServerConfigResolverTests(unittest.TestCase):
    def test_nuitka_marker_is_detected_as_production(self):
        with mock.patch.dict(server_config.__dict__, {"__compiled__": object()}):
            self.assertTrue(server_config.is_production_build())

    def test_dev_cli_overrides_config_and_saved_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "dev_config.json"
            config_path.write_text(
                json.dumps({"host": "config.example", "port": 14000}),
                encoding="utf-8",
            )
            saved = {"host": "saved.example", "port": 13000}

            endpoint = server_config.get_server_endpoint(
                argv=["--host", "cli.example", "--port=15000"],
                dev_config_path=config_path,
                settings_getter=saved.get,
            )

        self.assertEqual(endpoint, ("cli.example", 15000))

    def test_dev_config_overrides_saved_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "dev_config.json"
            config_path.write_text(
                json.dumps({"host": "config.example", "port": 14000}),
                encoding="utf-8",
            )
            saved = {"host": "saved.example", "port": 13000}
            endpoint = server_config.get_server_endpoint(
                argv=[],
                dev_config_path=config_path,
                settings_getter=saved.get,
            )

        self.assertEqual(endpoint, ("config.example", 14000))

    def test_local_alias_is_preserved_for_source_testing(self):
        saved = {"host": "saved.example", "port": 13001}
        endpoint = server_config.get_server_endpoint(
            argv=["local"],
            dev_config_path=Path("missing-dev-config.json"),
            settings_getter=saved.get,
        )
        self.assertEqual(endpoint, ("127.0.0.1", 13001))

    def test_production_uses_only_embedded_endpoint(self):
        settings_getter = mock.Mock(side_effect=AssertionError("settings were read"))
        with mock.patch.object(server_config, "is_production_build", return_value=True), mock.patch.object(
            vfs,
            "get_embedded_server_config",
            return_value={"host": "official.example", "port": 13000},
        ):
            endpoint = server_config.get_server_endpoint(
                argv=["--host", "attacker.example", "--port", "9999"],
                dev_config_path="dev_config.json",
                settings_getter=settings_getter,
            )

        self.assertEqual(endpoint, ("official.example", 13000))
        settings_getter.assert_not_called()

    def test_production_fails_closed_when_embedded_config_is_missing(self):
        with mock.patch.object(server_config, "is_production_build", return_value=True), mock.patch.object(
            vfs, "get_embedded_server_config", return_value=None
        ):
            with self.assertRaisesRegex(
                server_config.ServerConfigError, "configuration is missing"
            ):
                server_config.get_server_endpoint(argv=[])

    def test_endpoint_validation_rejects_url_and_invalid_port(self):
        with self.assertRaises(server_config.ServerConfigError):
            server_config.validate_server_endpoint("https://server.example", 13000)
        with self.assertRaises(server_config.ServerConfigError):
            server_config.validate_server_endpoint("server.example", 0)
        with self.assertRaises(server_config.ServerConfigError):
            server_config.validate_server_endpoint("server.example", True)

    def test_production_options_cannot_read_or_replace_endpoint(self):
        old_host = options.prefs.get("host")
        old_port = options.prefs.get("port")
        try:
            with mock.patch.object(
                server_config, "is_production_build", return_value=True
            ):
                options.set("host", "attacker.example", autosave=False)
                options.set("port", 9999, autosave=False)
                self.assertIsNone(options.get("host"))
                self.assertIsNone(options.get("port"))
            self.assertEqual(options.prefs.get("host"), old_host)
            self.assertEqual(options.prefs.get("port"), old_port)
        finally:
            options.prefs["host"] = old_host
            options.prefs["port"] = old_port

    def test_production_connection_error_does_not_reveal_endpoint(self):
        from libs.game import Game

        game = Game.__new__(Game)
        with mock.patch.object(
            server_config, "is_production_build", return_value=True
        ):
            message = game._connection_failure_message(
                OSError("DNS lookup failed for secret-origin.example:13000")
            )
        self.assertEqual(message, "Failed to connect to the official server.")
        self.assertNotIn("secret-origin.example", message)

    def test_production_load_scrubs_legacy_endpoint_from_settings_file(self):
        old_config_dirs = options.config_dirs
        old_prefs = dict(options.prefs)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                options.config_dirs = SimpleNamespace(user_config_dir=temp_dir)
                settings_path = Path(temp_dir) / "settings.json"
                settings_path.write_bytes(
                    options.fernet.encrypt(
                        json.dumps(
                            {
                                "host": "legacy-secret.example",
                                "port": 14000,
                                "beacons": False,
                            }
                        ).encode()
                    )
                )
                with mock.patch.object(
                    server_config, "is_production_build", return_value=True
                ):
                    options.load()

                persisted = json.loads(
                    options.fernet.decrypt(settings_path.read_bytes()).decode()
                )
                self.assertNotIn("host", persisted)
                self.assertNotIn("port", persisted)
                self.assertFalse(persisted["beacons"])
        finally:
            options.config_dirs = old_config_dirs
            options.prefs.clear()
            options.prefs.update(old_prefs)


class EndpointOptionsMenuTests(unittest.TestCase):
    class FakeOptionsMenu:
        last_instance = None

        def __init__(self, game, title, parent=None):
            self.game = game
            self.title = title
            self.parent = parent
            self.items = []
            self.turning_sensitivity_item_text = "Turning sensitivity"
            self.__class__.last_instance = self

        def add_items(self, items):
            self.items.extend(items)

        def set_music(self, path):
            self.music = path

    class FakeGame:
        def __init__(self):
            self.audio_mngr = SimpleNamespace(
                hrtf=SimpleNamespace(current_model="system default")
            )
            self.replaced = None

        def toggle_item(self, text, key, default=False):
            return text, lambda: None

        def replace(self, menu):
            self.replaced = menu

    def _menu_labels(self, production):
        game = self.FakeGame()
        with mock.patch.object(menus, "OptionsMenu", self.FakeOptionsMenu), mock.patch.object(
            menus, "set_default_sounds"
        ), mock.patch.object(
            server_config, "is_production_build", return_value=production
        ):
            menus.options_menu(game, lambda: None)
        return [item[0] if isinstance(item[0], str) else item[0]() for item in game.replaced.items]

    def test_production_menu_hides_hostname_and_port(self):
        labels = self._menu_labels(production=True)
        self.assertFalse(any("server" in label.lower() for label in labels))
        self.assertFalse(any("hostname" in label.lower() for label in labels))
        self.assertFalse(any("server port" in label.lower() for label in labels))

    def test_source_menu_keeps_developer_endpoint_controls(self):
        labels = self._menu_labels(production=False)
        self.assertTrue(any("Server hostname:" in label for label in labels))
        self.assertTrue(any("Server port:" in label for label in labels))


class ServerConfigPackagingTests(unittest.TestCase):
    def test_packager_reads_editable_build_config_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "build_server_config.json"
            config_path.write_text(
                json.dumps({"host": "game.example.com", "port": 14000}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                args = pack_data._parse_args(
                    ["--server-config", str(config_path)]
                )

        self.assertEqual(args.server_host, "game.example.com")
        self.assertEqual(args.server_port, 14000)

    def test_empty_build_config_fails_before_packaging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "build_server_config.json"
            config_path.write_text(
                json.dumps({"host": "", "port": 13000}), encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                "sys.stderr", new=io.StringIO()
            ), self.assertRaises(SystemExit):
                pack_data._parse_args(["--server-config", str(config_path)])

    def test_packager_embeds_config_without_modifying_source_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "test.txt").write_text("asset", encoding="utf-8")
            output = root / "sounds.dat"

            pack_data.pack_data(data_dir, output, "official.example", 13000)

            self.assertNotIn(b"official.example", output.read_bytes())
            with zipfile.ZipFile(output, "r") as archive:
                embedded = json.loads(
                    vfs.btx_decrypt(
                        archive.read(vfs.SERVER_CONFIG_MEMBER)
                    ).decode("utf-8")
                )
                self.assertEqual(
                    vfs.btx_decrypt(archive.read("test.txt")), b"asset"
                )

            self.assertEqual(
                embedded, {"host": "official.example", "port": 13000}
            )
            self.assertFalse((data_dir / ".bt").exists())

    def test_vfs_keeps_embedded_endpoint_out_of_extracted_temp_files(self):
        # Lazy VFS: nothing lands on disk at mount time — not even the
        # embedded endpoint, which is only ever decrypted in memory.
        old_cwd = Path.cwd()
        old_initialized = vfs.VFS_INITIALIZED
        old_temp_dir = vfs.TEMP_DIR
        old_config = vfs.EMBEDDED_SERVER_CONFIG
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    root = Path(temp_dir)
                    data_dir = root / "source_data"
                    data_dir.mkdir()
                    (data_dir / "test.txt").write_text("asset", encoding="utf-8")
                    pack_data.pack_data(
                        data_dir, root / "sounds.dat", "official.example", 13000
                    )

                    os.chdir(root)
                    vfs._reset_for_tests()
                    vfs.init_vfs()

                    self.assertEqual(
                        vfs.get_embedded_server_config(),
                        {"host": "official.example", "port": 13000},
                    )
                    # Physical-disk truth: the path hooks fake pack members
                    # into listings, so inspect with the saved originals.
                    real_listdir = vfs._ORIGINALS["listdir"]
                    # Mounting extracted nothing (no bulk plaintext dump).
                    self.assertEqual(real_listdir(vfs.TEMP_DIR), [])
                    # Decrypting the endpoint in memory never writes it to disk.
                    self.assertEqual(
                        vfs._INSTANCE.read_member(vfs.SERVER_CONFIG_MEMBER),
                        b'{"host":"official.example","port":13000}',
                    )
                    self.assertEqual(real_listdir(vfs.TEMP_DIR), [])
                    # A real asset only materializes on demand, and the .bt
                    # build members stay off disk even then.
                    materialized = vfs._INSTANCE.materialize("test.txt")
                    self.assertEqual(
                        Path(materialized).read_text(), "asset"
                    )
                    self.assertEqual(real_listdir(vfs.TEMP_DIR), ["test.txt"])
                finally:
                    os.chdir(old_cwd)
                    vfs.cleanup_vfs()
        finally:
            os.chdir(old_cwd)
            vfs.VFS_INITIALIZED = old_initialized
            vfs.TEMP_DIR = old_temp_dir
            vfs.EMBEDDED_SERVER_CONFIG = old_config


    def test_presence_sounds_configure_uses_embedded_endpoint_in_production(self):
        # Compiled builds never keep host/port in options (scrubbed for
        # security) — presence-sound uploads must resolve the endpoint
        # embedded in the encrypted VFS config instead of localhost.
        from libs.game import Game
        from libs.presence_sounds import PresenceSoundManager

        game = Game.__new__(Game)
        manager = PresenceSoundManager(game)
        with mock.patch.object(
            server_config, "is_production_build", return_value=True
        ), mock.patch.object(
            server_config, "_production_endpoint", return_value=("26.0.0.10", 13000)
        ):
            manager.configure(
                {"presence_upload_token": "tok", "presence_sound_http_port": 13001}
            )
        self.assertEqual(manager.base_url, "http://26.0.0.10:13001")

    def test_presence_sounds_configure_uses_options_host_in_source(self):
        # Source builds keep the developer-facing Options host, so the old
        # fallback still applies there.
        from libs.game import Game
        from libs.presence_sounds import PresenceSoundManager

        old_host = options.prefs.get("host")
        old_port = options.prefs.get("port")
        try:
            options.set("host", "192.168.1.5", autosave=False)
            options.set("port", 13000, autosave=False)
            game = Game.__new__(Game)
            manager = PresenceSoundManager(game)
            manager.configure(
                {"presence_upload_token": "tok", "presence_sound_http_port": 13001}
            )
            self.assertEqual(manager.base_url, "http://192.168.1.5:13001")
        finally:
            options.prefs["host"] = old_host
            options.prefs["port"] = old_port


if __name__ == "__main__":
    unittest.main()
