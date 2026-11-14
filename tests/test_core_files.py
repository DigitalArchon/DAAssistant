"""A crash asks for no core file: a conversation, its captures and attachments, and a key
typed in with no keyring live only in the app's memory (docs/security-and-privacy.md)."""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Started as a shell with `ulimit -c unlimited` would start it, then the app's own
# launcher run as it is from a desktop entry or a terminal, with an argument it answers
# before anything else; then the core size limit it left.
CHILD = r"""
import resource, runpy, sys
soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
if hard == 0:
    print("already none")
    sys.exit(0)
resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))
sys.argv = [sys.argv[1]] + sys.argv[2:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit:
    pass
print("limit", resource.getrlimit(resource.RLIMIT_CORE))
"""


class NoCoreFiles(unittest.TestCase):
    def run_launcher(self, *args):
        done = subprocess.run([sys.executable, "-c", CHILD,
                               os.path.join(ROOT, "run-assistant"), *args],
                              capture_output=True, text=True, timeout=60, cwd=ROOT)
        if "already none" in done.stdout:
            self.skipTest("this test's own process may have no core file at all")
        return done

    def test_every_way_in_asks_for_no_core_file_first(self):
        """--version is answered by the argument parser, before the app does anything
        else: the limit is set before even that."""
        for args in (["--version"], ["--action", "no-such-action"]):
            with self.subTest(args=args):
                done = self.run_launcher(*args)
                self.assertIn("limit (0, 0)", done.stdout, done.stderr)

    def test_a_process_it_starts_has_none_either(self):
        """WebKit's processes, which draw the conversation, are started from the app."""
        from assistant import __main__ as entry
        probe = ("import resource, subprocess, sys\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "from assistant.__main__ import no_core_files\n"
                 "soft, hard = resource.getrlimit(resource.RLIMIT_CORE)\n"
                 "resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))\n"
                 "no_core_files()\n"
                 "print(subprocess.run([sys.executable, '-c', 'import resource; "
                 "print(resource.getrlimit(resource.RLIMIT_CORE))'], capture_output=True,"
                 " text=True).stdout.strip())\n")
        done = subprocess.run([sys.executable, "-c", probe, ROOT], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.stdout.strip(), "(0, 0)", done.stderr)
        self.assertTrue(callable(entry.no_core_files))


if __name__ == "__main__":
    unittest.main()
