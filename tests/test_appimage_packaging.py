"""Tests for the AppImage: its contract with the host system, and its reproducible build.

The AppImage brings its own Python and uses the host's GTK. What it sets for itself -
SSL_CERT_FILE in AppRun, GI_TYPELIB_PATH for a fallback typelib - has to be handed back
to every host program the app spawns (grim, spectacle, xclip, gsettings, gdbus) as the
user had it. That handover is the part most likely to rot silently: add an export to
AppRun, forget to stash it, and something fails on somebody else's distribution.

The build is checked for the properties that make it reproducible: every input pinned
by hash, every timestamp the commit's. `packaging/build-appimage.sh check` is the real
test of that; these catch a change that would quietly break it.
"""

import ast
import os
import pathlib
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import bundle, hostenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging" / "appimage"
APPRUN = PACKAGING / "AppRun"
SPAWNS_HOST_BINARIES = [
    ROOT / "assistant" / "session.py",
    ROOT / "assistant" / "clipboard.py",
    ROOT / "assistant" / "capture" / "cli.py",
]


class HostEnvironmentHandback(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_outside_an_appimage_children_inherit_unchanged(self):
        os.environ.pop(hostenv.KEYS, None)
        self.assertIsNone(hostenv.host_env())

    def test_overridden_variables_are_restored_to_the_host_values(self):
        os.environ[hostenv.KEYS] = "LD_LIBRARY_PATH PYTHONHOME"
        os.environ["LD_LIBRARY_PATH"] = "/mnt/appimage/usr/lib"
        os.environ["APPIMAGE_HOST_LD_LIBRARY_PATH"] = "/opt/host/lib"
        os.environ["PYTHONHOME"] = "/mnt/appimage/usr"

        env = hostenv.host_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/host/lib")
        # PYTHONHOME had no stash, so the caller never had it set: it must not survive.
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("APPIMAGE_HOST_LD_LIBRARY_PATH", env)
        self.assertNotIn(hostenv.KEYS, env)

    def test_what_the_app_sets_for_itself_is_handed_back_as_it_was(self):
        os.environ[hostenv.KEYS] = "SSL_CERT_FILE"
        os.environ["GI_TYPELIB_PATH"] = "/host/typelibs"
        hostenv.set_for_self("GI_TYPELIB_PATH", "/mnt/appimage/fallback:/host/typelibs")
        self.assertEqual(os.environ["GI_TYPELIB_PATH"], "/mnt/appimage/fallback:/host/typelibs")
        self.assertEqual(hostenv.host_env()["GI_TYPELIB_PATH"], "/host/typelibs")
        os.environ.pop("GI_TYPELIB_PATH")
        os.environ.pop("APPIMAGE_HOST_GI_TYPELIB_PATH")
        hostenv.set_for_self("GI_TYPELIB_PATH", "/mnt/appimage/fallback")
        self.assertNotIn("GI_TYPELIB_PATH", hostenv.host_env())

    def test_unrelated_variables_are_passed_through(self):
        os.environ[hostenv.KEYS] = "LD_LIBRARY_PATH"
        os.environ["XDG_SESSION_TYPE"] = "wayland"
        self.assertEqual(hostenv.host_env()["XDG_SESSION_TYPE"], "wayland")

    def test_every_call_that_spawns_a_host_binary_passes_the_host_environment(self):
        for path in SPAWNS_HOST_BINARIES:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = ast.unparse(node.func)
                if target not in ("subprocess.run", "subprocess.Popen"):
                    continue
                keywords = {kw.arg for kw in node.keywords}
                self.assertIn("env", keywords,
                              f"{path.name}:{node.lineno} spawns a host binary without "
                              f"env=hostenv.host_env()")


class AppRunStashesWhatItOverrides(unittest.TestCase):
    """Every variable AppRun exports must appear in the list hostenv reads."""

    def setUp(self):
        self.text = APPRUN.read_text()

    def _declared_keys(self):
        match = re.search(r'export APPIMAGE_HOST_ENV_KEYS="([^"]*)"', self.text)
        self.assertIsNotNone(match, "AppRun no longer declares APPIMAGE_HOST_ENV_KEYS")
        return set(match.group(1).split())

    def test_declared_keys_cover_every_export(self):
        exported = set(re.findall(r'^export ([A-Z0-9_]+)=', self.text, re.MULTILINE))
        exported -= {"APPIMAGE_HOST_ENV_KEYS"}
        missing = exported - self._declared_keys()
        self.assertEqual(missing, set(),
                         f"AppRun overrides {sorted(missing)} without stashing the host's "
                         f"value; add them to APPIMAGE_HOST_ENV_KEYS")

    def _cert_file(self, env, hide=()):
        """What SSL_CERT_FILE AppRun's certificate block settles on, run on its own
        with `hide` made unreadable by pointing the candidates elsewhere."""
        start = self.text.index('if [ -z "${SSL_CERT_FILE:-}" ]; then')
        end = self.text.index("\nfi\n", start) + 4
        block = self.text[start:end]
        for path in hide:
            block = block.replace(path, "/nonexistent" + path)
        script = f'HERE=/opt/bundle\n{block}\nprintf %s "${{SSL_CERT_FILE-}}"'
        return subprocess.run(["sh", "-c", script], env=env, capture_output=True,
                              text=True, check=True).stdout

    def test_the_bundled_openssl_is_given_a_ca_bundle_that_exists(self):
        # The regression: a host without /usr/lib/ssl (Arch, Fedora) could not verify
        # Sigstore's TUF server, so no private/ model could be attested.
        chosen = self._cert_file({"PATH": os.environ.get("PATH", "")})
        self.assertTrue(chosen and os.access(chosen, os.R_OK), chosen)

    def test_a_host_with_no_bundle_falls_back_to_the_bundled_certifi(self):
        hosts = ["/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                 "/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cert.pem", "/etc/ssl/cert.pem"]
        start = self.text.index('if [ -z "${SSL_CERT_FILE:-}" ]; then')
        self.assertIn("private-mode/certifi/cacert.pem", self.text[start:start + 1200])
        self.assertEqual(self._cert_file({"PATH": os.environ.get("PATH", "")}, hosts), "")

    def test_a_ca_bundle_the_user_chose_is_left_alone(self):
        env = {"PATH": os.environ.get("PATH", ""), "SSL_CERT_FILE": "/my/bundle.pem"}
        self.assertEqual(self._cert_file(env), "/my/bundle.pem")

    def test_the_retired_hotkey_installer_says_where_shortcuts_went(self):
        # Shortcuts are set in the app (Settings > Shortcuts); a binding or a habit that
        # still asks for the old installer is told so, rather than starting the app.
        start = self.text.index('if [ "${1-}" = "--install-hotkeys" ]; then')
        block = self.text[start:self.text.index("fi", start)]
        self.assertIn("Settings > Shortcuts", block)
        self.assertIn("exit 2", block)
        self.assertNotIn("install-hotkeys.sh", self.text)

    def test_it_is_valid_shell(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(APPRUN)]).returncode, 0)

    def test_the_bundled_python_runs_isolated(self):
        # -I: no PYTHONPATH or user site-packages from the host shadowing what was bundled
        self.assertRegex(self.text, r'exec "\$HERE/usr/python/bin/python3" -I ')


class TheOldHotkeyInstallerIsGone(unittest.TestCase):
    def test_not_in_the_tree_or_the_image(self):
        # Replaced by assistant/keybindings.py; see test_shortcuts.Launcher for where
        # bindings now point.
        self.assertFalse((ROOT / "tools" / "install-hotkeys.sh").exists())
        self.assertNotIn("install-hotkeys", (PACKAGING / "build.sh").read_text())


class AppImageCarriesItsVersion(unittest.TestCase):
    """Managers read the version out of the image; nothing else tells them what it is."""

    def setUp(self):
        self.build = (PACKAGING / "build.sh").read_text()
        self.wrapper = (ROOT / "packaging" / "build-appimage.sh").read_text()

    def test_the_desktop_file_in_the_repo_carries_no_version_of_its_own(self):
        # Stamped in at build time instead, so assistant/__init__.py stays the one place
        # a version is bumped.
        text = (PACKAGING / "da-assistant.desktop").read_text()
        self.assertNotIn("X-AppImage-Version", text)

    def test_the_build_stamps_the_version_into_the_desktop_file(self):
        self.assertIn('"X-AppImage-Version=$VERSION"', self.build)

    def test_the_build_writes_appstream_metainfo_naming_the_same_version(self):
        self.assertIn("usr/share/metainfo", self.build)
        self.assertIn('<release version="$VERSION" date="$DATE"/>', self.build)

    def test_the_version_it_stamps_comes_from_the_commit_being_built(self):
        self.assertRegex(self.wrapper,
                         r'VERSION="\$\(git show HEAD:assistant/__init__.py \| sed -n .*__version__')


class ReproducibleBuild(unittest.TestCase):
    def setUp(self):
        self.build = (PACKAGING / "build.sh").read_text()
        self.wrapper = (ROOT / "packaging" / "build-appimage.sh").read_text()

    def test_it_builds_the_commit_with_the_commits_time(self):
        self.assertIn("git archive --format=tar HEAD", self.wrapper)
        self.assertIn('EPOCH="$(git log -1 --format=%ct HEAD)"', self.wrapper)
        self.assertIn('find "$W/AppDir" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +', self.build)

    def test_the_base_image_and_downloads_are_pinned(self):
        pins = (PACKAGING / "pins.sh").read_text()
        self.assertRegex(pins, r'BASE_IMAGE="[^"]+@sha256:[0-9a-f]{64}"')
        self.assertRegex(pins, r'APT_SNAPSHOT="\d{8}T\d{6}Z"')
        for name in ("PBS", "RUNTIME"):
            self.assertRegex(pins, rf'{name}_SHA256="[0-9a-f]{{64}}"')
            self.assertIn(f'fetch "${name}_URL" "${name}_SHA256"', self.build)

    def test_every_package_installed_from_a_lock_is_hash_checked(self):
        for line in re.findall(r'pip"? install[^\n]*-r [^\n]*', self.build):
            self.assertIn("--require-hashes", line, line)
        self.assertIn("--only-binary :all: -r \"$P/requirements.lock\"", self.build)

    def test_every_locked_package_carries_a_hash(self):
        for name in ("requirements", "build-requirements", "sdists"):
            path = PACKAGING / f"{name}.lock"
            if not path.exists():
                self.skipTest(f"{path.name} not generated yet: packaging/build-appimage.sh lock")
            entries = re.findall(r"^([\w.-]+)==([^\s]+)((?:\s*\\\s*--hash=sha256:\w+)*)",
                                 path.read_text(), re.M)
            self.assertTrue(entries, path.name)
            for package, version, hashes in entries:
                self.assertIn("--hash=sha256:", hashes, f"{path.name}: {package}=={version}")

    def test_nothing_of_the_build_machine_gets_in(self):
        self.assertIn("-root-owned -no-xattrs", self.build)
        self.assertIn("--invalidation-mode unchecked-hash", self.build)
        self.assertIn("PYTHONHASHSEED=0", self.build)
        self.assertIn('-ffile-prefix-map=$W/=', self.build)
        self.assertIn("direct_url.json -delete", self.build)


class FallbacksFillOnlyGaps(unittest.TestCase):
    """bundle.fill_gaps adds a bundled typelib only where the host has none, and one
    whose libraries will not load not at all."""

    def setUp(self):
        import shutil
        import tempfile
        self._saved = dict(os.environ)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.host = os.path.join(self.dir, "host")
        os.makedirs(self.host)
        self.fallback = os.path.join(self.dir, "fallback")
        for name in ("Gtk-3.0", "cairo-1.0", "Missing-1.0"):
            os.makedirs(os.path.join(self.fallback, "typelibs", name))
        open(os.path.join(self.host, "Gtk-3.0.typelib"), "w").close()
        bundle._used = None
        self.addCleanup(setattr, bundle, "_used", None)
        self._libraries = dict(bundle.LIBRARIES)
        self.addCleanup(lambda: (bundle.LIBRARIES.clear(),
                                 bundle.LIBRARIES.update(self._libraries)))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_only_what_the_host_lacks_is_added_and_only_for_the_app(self):
        os.environ[hostenv.KEYS] = "SSL_CERT_FILE"
        os.environ.pop("GI_TYPELIB_PATH", None)
        bundle.LIBRARIES["Missing-1.0"] = ("libdoes-not-exist-anywhere.so.9",)
        used = bundle.fill_gaps(self.fallback, host_dirs=(self.host,))
        self.assertEqual([u for u in used if u != "girepository-2.0"], ["cairo-1.0"])
        path = os.environ["GI_TYPELIB_PATH"].split(":")
        self.assertEqual(path, [os.path.join(self.fallback, "typelibs", "cairo-1.0")])
        self.assertNotIn("GI_TYPELIB_PATH", hostenv.host_env())

    def test_outside_the_appimage_it_does_nothing(self):
        os.environ.pop(hostenv.KEYS, None)
        self.assertEqual(bundle.fill_gaps(), [])
        self.assertEqual(bundle.status(), "")

    def test_the_build_carries_what_the_loader_expects(self):
        build = (PACKAGING / "build.sh").read_text()
        for libraries in bundle.LIBRARIES.values():
            for soname in libraries:
                self.assertIn(soname, build)
        self.assertIn('F="$W/AppDir/usr/lib/fallback"', build)
        self.assertEqual(bundle.fallback_dir(), os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))),
            "lib", "fallback"))


if __name__ == "__main__":
    unittest.main()
