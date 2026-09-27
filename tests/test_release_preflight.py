"""What the release preflight decides, and where each rule comes from.

The tool's whole value is that it answers a release question honestly: it asks
``pack_data`` to pack, ``login_attempts`` which doors exist, ``server_config``
what a name means, and the pack what it carries -- so this file pins the
decisions, and pins that the cheap halves are *asked for* rather than restated.
A preflight that computes the fallback port itself, or promotes a pack it never
read back, would pass a release that no player can reach.

Nothing here touches the live package, ``data/`` or a network: fixtures are
temporary directories and the probe is injected.
"""

import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

CLIENT = Path(__file__).resolve().parents[1]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, CLIENT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pack_data = _load("fj_preflight_pack_data", "tools/pack_data.py")
vfs = _load("fj_preflight_vfs", "libs/vfs.py")
preflight = _load("fj_release_preflight", "tools/release_preflight.py")


def _endpoint(host="official.example", port=13000, addresses=("8.8.8.8",)):
    embedded = {"host": host, "port": port}
    if addresses is not None:
        embedded["addresses"] = list(addresses)
    return embedded


class EmbeddedEndpointTests(unittest.TestCase):
    """The pack has to carry an endpoint *and* a door without DNS."""

    def test_a_public_backup_address_is_what_a_release_needs(self):
        self.assertEqual(preflight.embedded_problems(_endpoint()), [])

    def test_a_pack_with_no_backup_is_a_problem(self):
        problems = preflight.embedded_problems(_endpoint(addresses=None))
        self.assertEqual(len(problems), 1)
        self.assertIn("no backup address", problems[0])

    def test_a_private_address_is_refused_it_is_the_packers_own_network(self):
        for address in ("192.168.1.5", "10.1.2.3", "127.0.0.1"):
            problems = preflight.embedded_problems(_endpoint(addresses=(address,)))
            self.assertEqual(len(problems), 1, address)
            self.assertIn("not a public address", problems[0])

    def test_a_hostname_backup_is_allowed_because_a_name_survives_a_move(self):
        self.assertEqual(
            preflight.embedded_problems(_endpoint(addresses=("backup.example.com",))), []
        )

    def test_an_ipv6_literal_is_refused_this_transport_has_none(self):
        problems = preflight.embedded_problems(_endpoint(addresses=("::1",)))
        self.assertEqual(len(problems), 1)
        self.assertIn("not IPv4", problems[0])

    def test_an_endpoint_that_is_not_a_name_and_a_port_is_a_problem(self):
        problems = preflight.embedded_problems({"host": "", "port": 13000})
        self.assertEqual(len(problems), 1)
        self.assertIn("not usable", problems[0])


class CompiledPackageTests(unittest.TestCase):
    """The package being zipped must be code of *this* version."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="bt-preflight-exe-test-")
        self.addCleanup(self._temporary.cleanup)
        self.package = Path(self._temporary.name) / "Beyond Tournament"
        self.package.mkdir()

    def test_an_executable_carrying_this_version_is_the_one_to_zip(self):
        (self.package / "Beyond Tournament.exe").write_bytes(
            b"...BT-1.8.8..."
        )
        ok, text = preflight.compiled_version(self.package, "BT-1.8.8")
        self.assertTrue(ok, text)

    def test_an_executable_from_an_older_build_is_reported_as_stale(self):
        (self.package / "Beyond Tournament.exe").write_bytes(
            b"...BT-1.8.7..."
        )
        ok, text = preflight.compiled_version(self.package, "BT-1.8.8")
        self.assertFalse(ok)
        self.assertIn("re-compile", text)

    def test_a_package_with_no_executable_says_so(self):
        ok, text = preflight.compiled_version(self.package, "BT-1.8.8")
        self.assertFalse(ok)
        self.assertIn("compile the client first", text)


class DdnsTests(unittest.TestCase):
    """The name must still mean the address the pack embedded."""

    def test_an_answer_the_pack_carries_is_a_match(self):
        self.assertEqual(
            preflight.ddns_problems(_endpoint(), [("local", "8.8.8.8"), ("public", "8.8.8.8")]),
            [],
        )

    def test_an_answer_the_pack_does_not_carry_is_a_stale_pack(self):
        problems = preflight.ddns_problems(_endpoint(), [("local", "9.9.9.9")])
        self.assertEqual(len(problems), 1)
        self.assertIn("9.9.9.9", problems[0])
        self.assertIn("Re-pack", problems[0])

    def test_no_answer_anywhere_is_a_problem_even_with_a_backup(self):
        problems = preflight.ddns_problems(_endpoint(), [("local", None), ("public", None)])
        self.assertEqual(len(problems), 1)
        self.assertIn("no answer", problems[0])


class DoorTests(unittest.TestCase):
    """The doors are the login's own walk, over the pack's own address."""

    def test_the_doors_follow_the_shipped_fallback_offset(self):
        # Restating "+2" here would let the tool and the login drift apart, so
        # the offset is moved and the walked doors must move with it.
        with mock.patch.object(preflight.login_attempts, "FALLBACK_OFFSET", 5):
            walked = preflight.doors("official.example", 13000, ("8.8.8.8",))
        self.assertEqual(
            walked,
            (
                ("official.example", 13000),
                ("official.example", 13005),
                ("8.8.8.8", 13000),
                ("8.8.8.8", 13005),
            ),
        )

    def test_an_endpoint_with_no_room_above_it_walks_one_port(self):
        with mock.patch.object(preflight.login_attempts, "FALLBACK_OFFSET", 0):
            walked = preflight.doors("official.example", 13000, ())
        self.assertEqual(walked, (("official.example", 13000),))

    def test_a_backup_door_is_labelled_as_one(self):
        self.assertIn("backup", preflight.door_label(("8.8.8.8", 13000), "official.example"))
        self.assertIn("endpoint",
                      preflight.door_label(("official.example", 13000), "official.example"))


class CollectTests(unittest.TestCase):
    """The wiring: a silent door fails the run, an answered one passes it."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="bt-preflight-test-")
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.package = root / "Beyond Tournament"
        self.package.mkdir()
        assets = root / "data"
        assets.mkdir()
        (assets / "sound.ogg").write_bytes(b"a sound fixture")
        pack_data.pack_data(
            assets, self.package / preflight.PACK_NAME, "official.example", 13000, ["8.8.8.8"]
        )
        # A stand-in for the compiled executable: the check is about the version
        # string it carries, and a real 70 MB binary is not what that question
        # needs. The rule is pinned in CompiledPackageTests either way.
        (self.package / "Beyond Tournament.exe").write_bytes(
            b"fake executable " + preflight.consts.CLIENT_VERSION.encode("ascii")
        )
        self.notes = root / "server_docs"
        self.notes.mkdir()
        for name in preflight.NOTES:
            (self.notes / name).write_bytes(b"notes fixture")
            (self.package / name).write_bytes(b"notes fixture")

    def _collect(self, probe, resolve_to="8.8.8.8"):
        # The name's answer and the three-version rule are pinned elsewhere and
        # both touch the world (a resolver, the server repo): a fixture release
        # supplies them so this test is only about the doors.
        with mock.patch.object(preflight, "ddns_answers",
                               return_value=[("local", "8.8.8.8")]), \
             mock.patch.object(preflight, "versions_line", return_value=(True, "stub")), \
             mock.patch.object(preflight.server_config, "resolve_host",
                               return_value=resolve_to):
            return preflight.collect(self.package, self.notes, probe=probe)

    def test_every_door_answering_is_a_release_ready_to_publish(self):
        def answered(target, port, timeout=None):
            return {"target": target, "port": port, "answer": "reply", "detail": "login_failed"}

        lines, problems = self._collect(answered)
        self.assertEqual(problems, [])
        self.assertEqual(sum(line.startswith("[DOOR]") for line in lines), 4)

    def test_one_silent_door_fails_the_run(self):
        def silent_on_the_fallback(target, port, timeout=None):
            answer = "silent" if port == 13002 else "reply"
            return {"target": target, "port": port, "answer": answer, "detail": ""}

        lines, problems = self._collect(silent_on_the_fallback)
        self.assertEqual(len(problems), 2, problems)  # one per target
        self.assertTrue(all("did not answer" in problem for problem in problems))
        self.assertTrue(any("SILENT" in line for line in lines))

    def test_a_door_whose_name_has_no_answer_is_reported_not_probed(self):
        probed = []

        def spy(target, port, timeout=None):
            probed.append((target, port))
            return {"target": target, "port": port, "answer": "reply", "detail": ""}

        _lines, problems = self._collect(spy, resolve_to=None)
        self.assertEqual(probed, [])
        self.assertTrue(any("has no DNS answer" in problem for problem in problems))


class RepackTests(unittest.TestCase):
    """Packing goes through pack_data, and only a readable pack is promoted."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="bt-preflight-pack-test-")
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.package = root / "Beyond Tournament"
        self.package.mkdir()
        self.assets = root / "data"
        self.assets.mkdir()
        (self.assets / "sound.ogg").write_bytes(b"a sound fixture")
        self.config = root / "build_server_config.json"
        self.config.write_text(
            json.dumps({"host": "official.example", "port": 13000, "addresses": ["8.8.8.8"]}),
            encoding="utf-8",
        )

    def test_the_pack_step_is_pack_data_itself(self):
        calls = []

        def fake_main(argv):
            calls.append(argv)
            output = Path(argv[argv.index("--output") + 1])
            pack_data.pack_data(
                self.assets, output, "official.example", 13000, ["8.8.8.8"]
            )

        with mock.patch.object(preflight.pack_data, "main", side_effect=fake_main):
            resident, problems = preflight.repack(
                self.package, data_dir=self.assets, config_path=self.config, log=lambda _line: None
            )
        self.assertEqual(problems, [])
        self.assertEqual(resident, self.package / preflight.PACK_NAME)
        self.assertTrue(resident.is_file())
        # The packer's own precedence, not this tool's copy of it.
        self.assertEqual(calls[0][calls[0].index("--server-config") + 1], str(self.config))

    def test_a_pack_with_no_backup_is_never_promoted_over_a_good_one(self):
        good = self.package / preflight.PACK_NAME
        pack_data.pack_data(self.assets, good, "official.example", 13000, ["8.8.8.8"])

        def fake_main(argv):
            output = Path(argv[argv.index("--output") + 1])
            pack_data.pack_data(self.assets, output, "official.example", 13000, [])

        with mock.patch.object(preflight.pack_data, "main", side_effect=fake_main):
            resident, problems = preflight.repack(
                self.package, data_dir=self.assets, config_path=self.config, log=lambda _line: None
            )
        self.assertIsNone(resident)
        self.assertEqual(len(problems), 1)
        # The good pack is untouched, and the refused one is beside it to look at.
        self.assertEqual(
            preflight.read_embedded(good)["addresses"], ["8.8.8.8"]
        )
        self.assertTrue((self.package / (preflight.PACK_NAME + ".new")).is_file())


class CompareTests(unittest.TestCase):
    """A re-pack is only 'nothing changed' if the members still mean the same."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="bt-preflight-compare-test-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _pack(self, name, files):
        assets = self.root / name
        assets.mkdir()
        for member, data in files.items():
            path = assets / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        output = self.root / f"{name}.dat"
        preflight.pack_data.pack_data(assets, output, "official.example", 13000, ["8.8.8.8"])
        return output

    def test_an_unchanged_data_tree_reports_nothing_moved(self):
        first = self._pack("one", {"sound.ogg": b"same"})
        second = self._pack("two", {"sound.ogg": b"same"})
        added, removed, changed = preflight.compare_packs(first, second)
        self.assertEqual((added, removed, changed), ([], [], []))

    def test_a_changed_added_and_removed_member_are_all_named(self):
        first = self._pack("before", {"sound.ogg": b"old", "extra.ogg": b"gone"})
        second = self._pack("after", {"sound.ogg": b"new", "added.ogg": b"here"})
        added, removed, changed = preflight.compare_packs(first, second)
        self.assertEqual(added, ["added.ogg"])
        self.assertEqual(removed, ["extra.ogg"])
        self.assertEqual(changed, ["sound.ogg"])

    def test_the_pack_reader_is_the_games_own(self):
        # Decryption that this tool did itself would be a second implementation
        # of the format the client reads a released build with.
        pack = self._pack("vfs", {"sound.ogg": b"a sound fixture"})
        with zipfile.ZipFile(pack) as archive:
            self.assertEqual(
                preflight.btx_decrypt_member(archive, "sound.ogg"), b"a sound fixture"
            )


class ExitCodeTests(unittest.TestCase):
    """A problem is a non-zero exit: the gate is the only thing standing between
    a package and a player, so it has to be the whole answer."""

    def test_a_problem_returns_one_and_a_clean_run_returns_zero(self):
        with mock.patch.object(preflight, "collect", return_value=(["line"], ["one problem"])), \
             mock.patch.object(preflight, "repack", return_value=(None, [])):
            self.assertEqual(preflight.main(["--package", "somewhere"]), 1)
        with mock.patch.object(preflight, "collect", return_value=(["line"], [])), \
             mock.patch.object(preflight, "repack", return_value=(None, [])):
            self.assertEqual(preflight.main(["--package", "somewhere"]), 0)

    def test_a_refused_pack_stops_before_the_checks(self):
        with mock.patch.object(preflight, "collect", side_effect=AssertionError("should not run")), \
             mock.patch.object(preflight, "repack", return_value=(None, ["refused"])):
            self.assertEqual(preflight.main(["--pack", "--package", "somewhere"]), 1)


if __name__ == "__main__":
    unittest.main()
