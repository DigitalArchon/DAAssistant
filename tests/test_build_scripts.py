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
