"""The scripts beside the app: what they take from the machine they run on.

tools/install-private-mode.sh checks the install it has just made by importing the app
from its checkout, which may be in any folder; packaging/build-appimage.sh finds the
host's CA bundle for the builder's apt, wherever the distribution keeps it. Neither is
run whole here - one installs from the network, the other builds with Podman - but the
parts of them that read the machine are.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

ROOT = Path(__file__).resolve().parent.parent


class TheInstallCheckTakesAnyFolder(unittest.TestCase):
    SCRIPT = (ROOT / "tools" / "install-private-mode.sh").read_text()

    def check(self):
        """The check's Python, and what it is given after it, as the script has them."""
        found = re.search(r"""if "\$PYTHON" -c '\n(.*?)' (.*?); then""", self.SCRIPT, re.S)
        self.assertIsNotNone(found, "the check is not where this test looks for it")
        return found.group(1), found.group(2)

    def test_the_folder_is_an_argument_not_code(self):
        code, given = self.check()
        self.assertNotIn("$ROOT", code)
        self.assertEqual(given, '"$ROOT"')
        self.assertIn("sys.argv[1]", code)

    def test_a_folder_with_quotes_in_its_name_is_checked(self):
        code, _given = self.check()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        folder = os.path.join(parent, "it's a \"checkout\"; print('injected')")
        os.makedirs(folder)
        os.symlink(ROOT / "assistant", os.path.join(folder, "assistant"))
        done = subprocess.run([sys.executable, "-B", "-c", code, folder],
                              capture_output=True, text=True, timeout=120,
                              cwd=parent)
        # Whether Tinfoil's client is installed here or not, the check ran and said so -
        # from the app in that folder, and nothing else ran.
        self.assertRegex(done.stdout, r"^(ok|FAILED): ", done.stderr)
        self.assertNotIn("injected", done.stdout)
        self.assertNotIn("SyntaxError", done.stderr)
        self.assertEqual(done.returncode, 0 if done.stdout.startswith("ok") else 1)


class TheInstallTakesNothingFromThisMachinesPip(unittest.TestCase):
    """pip ran with this machine's settings: PIP_INDEX_URL, PIP_TRUSTED_HOST or a
    pip.conf naming another index, or plain http, applied to the install. The hashes
    held what was installed to the lock; where it came from, and how, was the
    machine's to say. As SealedLore's build installs (Nov 2026)."""

    SCRIPT = (ROOT / "tools" / "install-private-mode.sh").read_text()

    def install(self):
        joined = re.sub(r"\\\n\s*", " ", self.SCRIPT)
        found = [line.strip() for line in joined.splitlines()
                 if re.search(r"-m pip install\b", line) and not line.lstrip().startswith("#")]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_it_runs_isolated_with_no_config_file_and_by_hash(self):
        command = self.install()
        self.assertTrue(command.startswith('PIP_CONFIG_FILE=/dev/null "$PYTHON" -m pip '),
                        command)
        for option in ("--isolated", "--require-hashes", "--only-binary :all:",
                       "--no-deps", '-r "$LOCK"'):
            self.assertIn(option, command)
        for option in ("--index-url", "--extra-index-url", "--trusted-host", "--find-links"):
            self.assertNotIn(option, command)

    def test_so_pip_reads_none_of_this_machines_settings(self):
        """What pip reads, asked of pip itself with the script's settings: nothing,
        with an index and an insecure host in the environment and a config file named."""
        try:
            import pip  # noqa: F401
        except ImportError:
            self.skipTest("pip is not installed here")
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        config = Path(folder, "pip.conf")
        config.write_text("[global]\nindex-url = http://index.example/simple\n")
        environment = dict(os.environ, PIP_INDEX_URL="http://index.example/simple",
                           PIP_TRUSTED_HOST="index.example", PIP_CONFIG_FILE=str(config),
                           HOME=folder, XDG_CONFIG_HOME=folder)
        seen = subprocess.run([sys.executable, "-m", "pip", "config", "list"],
                              capture_output=True, text=True, timeout=60, env=environment)
        self.assertIn("index.example", seen.stdout)     # as it was
        environment["PIP_CONFIG_FILE"] = "/dev/null"
        seen = subprocess.run([sys.executable, "-m", "pip", "--isolated", "config", "list"],
                              capture_output=True, text=True, timeout=60, env=environment)
        self.assertEqual(seen.returncode, 0, seen.stderr)
        self.assertNotIn("index.example", seen.stdout)


class TheBuildFindsTheHostsCaBundle(unittest.TestCase):
    WRAPPER = (ROOT / "packaging" / "build-appimage.sh").read_text()
    APPRUN = (ROOT / "packaging" / "appimage" / "AppRun").read_text()

    def lookup(self, *candidates):
        """host_ca_bundle, as the script defines it, given `candidates`."""
        found = re.search(r"^host_ca_bundle\(\) \{\n.*?^\}\n", self.WRAPPER, re.S | re.M)
        self.assertIsNotNone(found, "the lookup is not where this test looks for it")
        return subprocess.run(["bash", "-c", found.group(0) + 'host_ca_bundle "$@"',
                               "bash", *candidates],
                              capture_output=True, text=True, timeout=30)

    def test_it_looks_wherever_apprun_looks(self):
        listed = re.search(r"^CA_BUNDLES=\((.*?)\)", self.WRAPPER, re.S | re.M).group(1)
        wrapper = listed.split()
        apprun = [path for path in re.findall(r"(/etc/\S+)", self.APPRUN)
                  if path.endswith((".crt", ".pem"))]
        self.assertTrue(apprun)
        self.assertEqual(wrapper, apprun)
        for path in ("/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cert.pem"):
            self.assertIn(path, wrapper)

    def test_the_first_there_is_used(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        bundle = os.path.join(folder, "ca-bundle.pem")
        Path(bundle).write_text("certificates")
        done = self.lookup(os.path.join(folder, "missing.crt"), bundle,
                           os.path.join(folder, "also-missing.pem"))
        self.assertEqual((done.returncode, done.stdout), (0, bundle + "\n"))

    def test_none_there_is_said_plainly(self):
        done = self.lookup("/nonexistent/one.crt", "/nonexistent/two.pem")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertIn("No CA bundle on this machine", done.stderr)
        self.assertIn("/nonexistent/one.crt /nonexistent/two.pem", done.stderr)
        self.assertIn("ca-certificates", done.stderr)

    def test_it_is_looked_for_only_to_make_the_builder(self):
        made = self.WRAPPER.index('if ! podman image exists "$TAG"; then')
        self.assertGreater(self.WRAPPER.index('ca="$(host_ca_bundle "${CA_BUNDLES[@]}")"'),
                           made)
        self.assertNotRegex(self.WRAPPER, r"(?m)^for ca in ")


if __name__ == "__main__":
    unittest.main()
