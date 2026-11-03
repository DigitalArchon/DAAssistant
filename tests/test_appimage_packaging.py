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
import shlex
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import bundle, hostenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging" / "appimage"
APPRUN = PACKAGING / "AppRun"
# The modules known to run host programs; the test looks through every module, and
# these must be among what it finds.
SPAWNS_HOST_BINARIES = [
    ROOT / "assistant" / "session.py",
    ROOT / "assistant" / "clipboard.py",
    ROOT / "assistant" / "capture" / "cli.py",
    ROOT / "assistant" / "keybindings.py",
]
SUBPROCESS_SPAWN = re.compile(r"subprocess\.(run|Popen|call|check_call|check_output"
                              r"|getoutput|getstatusoutput)")
OTHER_SPAWN = re.compile(r"os\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*)"
                         r"|GLib\.spawn\w*|Gio\.Subprocess(\.\w+)?")


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

    def test_the_runtimes_own_variables_are_not_handed_on(self):
        os.environ[hostenv.KEYS] = "SSL_CERT_FILE"
        for key in ("APPIMAGE", "APPDIR", "OWD", "ARGV0"):
            os.environ[key] = f"/mnt/{key.lower()}"
        env = hostenv.host_env()
        for key in ("APPIMAGE", "APPDIR", "OWD", "ARGV0"):
            self.assertNotIn(key, env)

    def test_a_link_opens_with_the_host_environment(self):
        os.environ[hostenv.KEYS] = "SSL_CERT_FILE"
        os.environ["SSL_CERT_FILE"] = "/mnt/appimage/cacert.pem"
        os.environ["APPIMAGE_HOST_SSL_CERT_FILE"] = "/etc/host.pem"
        os.environ["APPIMAGE"] = "/home/u/DA.AppImage"
        launched = []

        class Context:
            def __init__(self):
                self.set, self.unset = {}, set()

            def setenv(self, key, value):
                self.set[key] = value

            def unsetenv(self, key):
                self.unset.add(key)

        def launch(uri, context):
            launched.append((uri, context))

        from gi.repository import Gio
        with mock.patch.object(Gio, "AppLaunchContext", Context), \
                mock.patch.object(Gio.AppInfo, "launch_default_for_uri", launch):
            hostenv.open_uri("https://example.org/")
        uri, context = launched[0]
        self.assertEqual(uri, "https://example.org/")
        self.assertEqual(context.set["SSL_CERT_FILE"], "/etc/host.pem")
        self.assertIn("APPIMAGE", context.unset)
        self.assertIn(hostenv.KEYS, context.unset)

    def test_outside_an_appimage_a_link_opens_with_ours(self):
        os.environ.pop(hostenv.KEYS, None)
        launched = []
        from gi.repository import Gio
        with mock.patch.object(Gio.AppInfo, "launch_default_for_uri",
                               lambda uri, context: launched.append(context)):
            hostenv.open_uri("mailto:someone@example.org")
        self.assertEqual(launched, [None])

    def test_unrelated_variables_are_passed_through(self):
        with mock.patch.dict(os.environ, {hostenv.KEYS: "LD_LIBRARY_PATH",
                                          "XDG_SESSION_TYPE": "wayland"}):
            self.assertEqual(hostenv.host_env()["XDG_SESSION_TYPE"], "wayland")

    def test_every_call_that_spawns_a_host_binary_passes_the_host_environment(self):
        # Every module of the app, not a list of the ones known to spawn: a new caller
        # is exactly the one nobody thought to add.
        spawning = set()
        for path in sorted((ROOT / "assistant").rglob("*.py")):
            where = path.relative_to(ROOT)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                    self.fail(f"{where}:{node.lineno} imports from subprocess: call it as "
                              "subprocess.run or subprocess.Popen, which this test sees")
                if not isinstance(node, ast.Call):
                    continue
                target = ast.unparse(node.func)
                if OTHER_SPAWN.fullmatch(target):
                    self.fail(f"{where}:{node.lineno} spawns with {target}, which this "
                              "test cannot hold to hostenv.host_env(): use subprocess")
                if not SUBPROCESS_SPAWN.fullmatch(target):
                    continue
                spawning.add(where.as_posix())
                env = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}.get("env")
                self.assertEqual(env, "hostenv.host_env()",
                                 f"{where}:{node.lineno} spawns a host binary without "
                                 f"env=hostenv.host_env()")
        self.assertLessEqual(
            {path.relative_to(ROOT).as_posix() for path in SPAWNS_HOST_BINARIES}, spawning)


class TheSuitesOwnCache(unittest.TestCase):
    """Everything the app writes to its cache goes under paths.CACHE_DIR, and the tests
    point that somewhere of their own (tests/support.py)."""

    def test_every_cache_path_is_under_the_suites_cache(self):
        from assistant import errors, imaging, models, paths, webkitguard
        from assistant.reference import dictionaries
        self.assertTrue(paths.CACHE_DIR.startswith(support.CACHE))
        for path in (errors.LOG_FILE, webkitguard.STATE_FILE, models.CACHE_FILE,
                     dictionaries.CACHE_DIR):
            self.assertTrue(path.startswith(paths.CACHE_DIR), path)
        with open(imaging.__file__, encoding="utf-8") as f:
            self.assertIn("paths.CACHE_DIR", f.read())


class TheTwoLocksAgree(unittest.TestCase):
    """The Privacy Mode client's directory is ahead of the app's site-packages on the
    path, so a package both lock had better be the same version in each: 3.4.0 shipped
    cryptography 50.0.2 and ran 50.0.1 from the client's copy, an older OpenSSL."""

    @staticmethod
    def pins(path):
        import re
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            return {re.sub(r"[-_.]+", "-", name).lower(): version for name, version in
                    re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", f.read(), re.M)}

    def test_every_package_in_both_is_the_same_version(self):
        app = self.pins("packaging/appimage/requirements.lock")
        private = self.pins("packaging/private-mode.lock")
        shared = app.keys() & private.keys()
        self.assertIn("cryptography", shared)
        self.assertEqual({name: private[name] for name in shared},
                         {name: app[name] for name in shared})

    def test_the_lock_and_the_build_keep_them_so(self):
        with open(os.path.join(ROOT, "packaging/lock-private-mode.sh"), encoding="utf-8") as f:
            self.assertIn("--constraint appimage/requirements.lock", f.read())
        with open(os.path.join(ROOT, "packaging/appimage/build.sh"), encoding="utf-8") as f:
            build = f.read()
        self.assertIn("private-mode/ would shadow the app's own packages", build)
        self.assertIn("gh-releases-zsync|DigitalArchon|DAAssistant|latest|", build)
        self.assertIn("zsyncmake", build)

    def test_socks_proxies_have_what_they_need(self):
        self.assertIn("pysocks", self.pins("packaging/appimage/requirements.lock"))
        self.assertIn("socksio", self.pins("packaging/private-mode.lock"))

    def test_the_lock_meets_every_floor_the_in_file_sets(self):
        # A relock keeps what the lock already pins, so a floor added for an advisory
        # (certifi's root, pyasn1's and dnspython's denials of service) is only as good
        # as the relock that followed it.
        def version(text):
            return tuple(int(part) for part in text.split("."))

        private = self.pins("packaging/private-mode.lock")
        with open(os.path.join(ROOT, "packaging/private-mode.in"), encoding="utf-8") as f:
            floors = re.findall(r"^([A-Za-z0-9_.-]+)>=([0-9.]+)$", f.read(), re.M)
        self.assertIn(("pyasn1", "0.6.5"), floors)
        self.assertIn(("dnspython", "2.9.0"), floors)
        for name, floor in floors:
            locked = private[re.sub(r"[-_.]+", "-", name).lower()]
            self.assertGreaterEqual(version(locked), version(floor), f"{name}=={locked}")

    def test_a_relock_takes_no_index_from_this_machine(self):
        """UV_NO_CONFIG skips only uv's config files: an index, a find-links folder or
        an insecure host named in the environment would still be used."""
        with open(os.path.join(ROOT, "packaging/lock-private-mode.sh"), encoding="utf-8") as f:
            script = f.read()
        start = script.index('unset "${!UV_@}"')
        end = script.index("\nfi\n", script.index('if [ "${UPGRADE:-0}" = 1 ]', start)) + 4
        planted = {"UV_INDEX_URL": "https://mirror.example/simple",
                   "UV_EXTRA_INDEX_URL": "https://mirror.example/extra",
                   "UV_DEFAULT_INDEX": "https://mirror.example/simple",
                   "UV_INDEX": "mirror=https://mirror.example/simple",
                   "UV_FIND_LINKS": "/tmp/wheels", "UV_INSECURE_HOST": "mirror.example"}
        for upgrade, flag in (("0", ""), ("1", "--upgrade")):
            env = dict(planted, PATH=os.environ.get("PATH", ""), UPGRADE=upgrade)
            shell = (f'set -euo pipefail\nCACHE=/cache\n{script[start:end]}\n'
                     'env | grep "^UV_" | cut -d= -f1 | sort\necho "flag ${upgrade:+$upgrade}"')
            out = subprocess.run(["bash", "-c", shell], env=env, capture_output=True,
                                 text=True, check=True).stdout.split()
            self.assertEqual(out[:3], ["UV_CACHE_DIR", "UV_NO_CONFIG", "UV_PYTHON_DOWNLOADS"])
            self.assertEqual(out[3:], ["flag"] + ([flag] if flag else []))
        self.assertIn('${upgrade:+"$upgrade"}', script)


class AppRunStashesWhatItOverrides(unittest.TestCase):
    """Every variable AppRun exports must appear in the list hostenv reads."""

    def setUp(self):
        self.text = APPRUN.read_text()

    def _declared_keys(self):
        match = re.search(r'export APPIMAGE_HOST_ENV_KEYS="([^"]*)"', self.text)
        self.assertIsNotNone(match, "AppRun no longer declares APPIMAGE_HOST_ENV_KEYS")
        return set(match.group(1).split())

    def test_declared_keys_cover_every_export(self):
        # Indented too: every export AppRun makes is inside an if.
        exported = set()
        for line in re.findall(r"^\s*export\s+(.+)$", self.text, re.MULTILINE):
            exported.update(word.split("=", 1)[0] for word in shlex.split(line, comments=True))
        self.assertIn("SSL_CERT_FILE", exported)
        stashes = {name for name in exported if name.startswith(hostenv.PREFIX)}
        missing = exported - stashes - self._declared_keys()
        self.assertEqual(missing, set(),
                         f"AppRun overrides {sorted(missing)} without stashing the host's "
                         f"value; add them to APPIMAGE_HOST_ENV_KEYS")

    def _apprun(self, env, here="/opt/bundle", hide=()):
        """The environment AppRun starts the app with: AppRun up to its exec, run as
        if it sat in `here`, with the CA bundles in `hide` made unreadable by pointing
        the candidates elsewhere. Without the variables the shell keeps for itself."""
        head = self.text[:self.text.index('exec "$HERE/usr/python/bin/python3"')]
        for path in hide:
            head = head.replace(path, "/nonexistent" + path)
        out = subprocess.run(["sh", "-c", head + "env -0\n", os.path.join(here, "AppRun")],
                             env=env, capture_output=True, check=True).stdout
        started = dict(item.split("=", 1) for item in out.decode().split("\0") if item)
        for shells_own in ("PWD", "OLDPWD", "SHLVL", "_"):
            started.pop(shells_own, None)
        return started

    def test_the_host_programs_get_back_what_apprun_changed(self):
        """Whatever AppRun changes, by whatever means, hostenv hands back: the app's
        children see the environment the AppImage was started with."""
        path = os.environ.get("PATH", "")
        for host in ({"PATH": path},
                     {"PATH": path, "SSL_CERT_FILE": "/my/bundle.pem"},
                     {"PATH": path, "REQUESTS_CA_BUNDLE": "/my/requests.pem"}):
            started = self._apprun(host)
            self.assertNotEqual(started, host)
            with mock.patch.dict(os.environ, started, clear=True), \
                    mock.patch.dict(hostenv._own, clear=True):
                self.assertEqual(hostenv.host_env(), host)

    def _certificates(self, env, here="/opt/bundle", hide=()):
        started = self._apprun(env, here, hide)
        return started.get("SSL_CERT_FILE"), started.get("REQUESTS_CA_BUNDLE")

    def test_the_bundled_openssl_is_given_a_ca_bundle_that_exists(self):
        # The regression: a host without /usr/lib/ssl (Arch, Fedora) could not verify
        # Sigstore's TUF server, so no private/ model could be attested.
        chosen, requests_bundle = self._certificates({"PATH": os.environ.get("PATH", "")})
        self.assertTrue(chosen and os.access(chosen, os.R_OK), chosen)
        self.assertEqual(requests_bundle, chosen)

    def test_a_host_with_no_bundle_falls_back_to_the_bundled_certifi(self):
        import shutil
        import tempfile
        hosts = ["/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                 "/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cert.pem", "/etc/ssl/cert.pem"]
        # A space in the AppImage's mount path, as an extracted one can have
        here = os.path.join(tempfile.mkdtemp(), "Squashfs root")
        self.addCleanup(shutil.rmtree, os.path.dirname(here), True)
        private = os.path.join(here, "usr/lib/da-assistant/private-mode/certifi/cacert.pem")
        site = os.path.join(here, "usr/python/lib/python3.12/site-packages/certifi/cacert.pem")
        for certifi in (private, site):
            os.makedirs(os.path.dirname(certifi))
            open(certifi, "w").close()
        env = {"PATH": os.environ.get("PATH", "")}
        self.assertEqual(self._certificates(env, here, hosts), (private, private))
        os.remove(private)
        self.assertEqual(self._certificates(env, here, hosts), (site, site))
        os.remove(site)
        self.assertEqual(self._certificates(env, here, hosts), (None, None))

    def test_a_ca_bundle_the_user_chose_is_left_alone(self):
        env = {"PATH": os.environ.get("PATH", ""), "SSL_CERT_FILE": "/my/bundle.pem"}
        self.assertEqual(self._certificates(env), ("/my/bundle.pem", "/my/bundle.pem"))
        env["REQUESTS_CA_BUNDLE"] = "/my/requests.pem"
        self.assertEqual(self._certificates(env), ("/my/bundle.pem", "/my/requests.pem"))

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

    def _commands(self):
        """build.sh's commands, one to a line: a command continued over several lines
        is joined into one."""
        return re.sub(r"\\\n\s*", " ", self.build).splitlines()

    def test_every_download_is_https_only(self):
        # A hash proves what arrived, not that nobody saw it asked for: no download
        # may fall back to http, through a redirect either.
        curls = []
        for path in sorted((ROOT / "packaging").rglob("*.sh")) + sorted((ROOT / "tools").glob("*.sh")):
            for line in re.sub(r"\\\n\s*", " ", path.read_text()).splitlines():
                if re.search(r"\bcurl\s+-", line) and not line.lstrip().startswith("#"):
                    curls.append(path.name)
                    self.assertIn("--proto '=https'", line, f"{path.name}: {line.strip()}")
        self.assertLessEqual({"build.sh", "lock-private-mode.sh", "install-ruffle.sh"}, set(curls))
        # the source archives are fetched by Python, which follows a redirect to http
        self.assertIn("class HttpsOnly(urllib.request.HTTPRedirectHandler)", self.build)
        self.assertNotIn("urllib.request.urlopen(", self.build)

    def test_every_package_installed_from_a_lock_is_hash_checked(self):
        installs = [line for line in self._commands() if re.search(r'pip"? install .*-r ', line)]
        for line in installs:
            self.assertIn("--require-hashes", line, line)
        locks = {re.search(r'-r "([^"]+)"', line)[1] for line in installs}
        self.assertLessEqual({"$P/build-requirements.lock", "$P/requirements.lock",
                              "$W/src/packaging/private-mode.lock"}, locks)
        self.assertIn("--only-binary :all: -r \"$P/requirements.lock\"", self.build)

    def test_every_locked_package_carries_a_hash(self):
        for path in [PACKAGING / f"{name}.lock" for name in
                     ("requirements", "build-requirements", "sdists")] + \
                [ROOT / "packaging" / "private-mode.lock"]:
            if not path.exists():
                self.skipTest(f"{path.name} not generated yet: packaging/build-appimage.sh lock")
            entries = re.findall(r"^([\w.-]+)==([^\s]+)((?:\s*\\\s*--hash=sha256:\w+)*)",
                                 path.read_text(), re.M)
            self.assertTrue(entries, path.name)
            for package, version, hashes in entries:
                self.assertIn("--hash=sha256:", hashes, f"{path.name}: {package}=={version}")

    def test_the_encryption_stack_is_named_at_the_locks_versions(self):
        """tinfoil accepts any tinfoil-ehbp>=0.3.0: named only by what it pulls in, a
        relock would take a newer one - 0.4.0, a new request framing nobody here has
        read - for the library that seals and opens every private request."""
        root = ROOT / "packaging"
        lock = (root / "private-mode.lock").read_text()
        wanted = (root / "private-mode.in").read_text()
        for name in ("tinfoil", "tinfoil-ehbp", "pyhpke"):
            locked = re.search(rf"^{re.escape(name)}==(\S+)", lock, re.M)
            self.assertTrue(locked, f"{name} is not in private-mode.lock")
            self.assertRegex(wanted, rf"(?m)^{re.escape(name)}=={re.escape(locked[1])}$",
                             f"private-mode.in must name {name}=={locked[1]}")

    def test_nothing_of_the_build_machine_gets_in(self):
        self.assertIn("-root-owned -no-xattrs", self.build)
        self.assertIn("--invalidation-mode unchecked-hash", self.build)
        self.assertIn("PYTHONHASHSEED=0", self.build)
        self.assertIn('-ffile-prefix-map=$W/=', self.build)
        self.assertIn("direct_url.json -delete", self.build)


class PillowsFontsAreLeftOut(unittest.TestCase):
    """Pillow's font module is the only thing that loads the Brotli it bundles (FreeType reads
    WOFF2 with it), and Brotli 1.2.0 has CVE-2026-111497 (Oct 2026). The build leaves the module
    and its libraries out, and stops if anything still needs them. That holds only while the
    app draws no text with Pillow."""

    def setUp(self):
        self.build = (PACKAGING / "build.sh").read_text()

    def test_the_font_module_and_its_libraries_are_left_out(self):
        for part in ("PIL/_imagingft.*.so", "pillow.libs/libfreetype-*.so*",
                     "pillow.libs/libharfbuzz-*.so*", "pillow.libs/libbrotli*.so*"):
            self.assertIn(part, self.build)
        self.assertIn("readelf -d", self.build)

    def test_the_app_draws_no_text_with_pillow(self):
        source = ROOT / "assistant"
        for path in source.rglob("*.py"):
            text = path.read_text()
            for used in ("ImageFont", "_imagingft", ".text(", "truetype("):
                if used == ".text(" and "ImageDraw" not in text:
                    continue
                self.assertNotIn(used, text, f"{path.relative_to(ROOT)} uses Pillow's fonts: "
                                 "the AppImage leaves them out (packaging/appimage/build.sh)")


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

    def test_the_hosts_own_search_path_comes_first(self):
        """A typelib the host provides through GI_TYPELIB_PATH - /usr/local, Nix - is
        the one used; the bundled ones only fill in after it."""
        os.environ[hostenv.KEYS] = "SSL_CERT_FILE"
        os.environ["GI_TYPELIB_PATH"] = "/usr/local/lib/girepository-1.0:/nix/typelibs"
        bundle.fill_gaps(self.fallback, host_dirs=(self.host,))
        bundled = [os.path.join(self.fallback, "typelibs", name)
                   for name in ("Missing-1.0", "cairo-1.0")]
        self.assertEqual(os.environ["GI_TYPELIB_PATH"].split(":"),
                         ["/usr/local/lib/girepository-1.0", "/nix/typelibs"] + bundled)
        self.assertEqual(hostenv.host_env()["GI_TYPELIB_PATH"],
                         "/usr/local/lib/girepository-1.0:/nix/typelibs")

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
