"""The scripts beside the app: what they take from the machine they run on.

tools/install-private-mode.sh checks the install it has just made by importing the app
from its checkout, which may be in any folder. It is not run whole here - it installs
from the network - but the part of it that reads the machine is.
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


if __name__ == "__main__":
    unittest.main()
