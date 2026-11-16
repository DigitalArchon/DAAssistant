"""The modules DA Media Browser copies line for line from this app (its core/ holds
them, differing only in their docstrings and imports): services.py, levels.py,
chutes_e2ee.py, chutes.py, tinfoil_direct.py and anthropic.py. They may import only the
standard library, cryptography, requests, httpx and Tinfoil's SDK, and of this app's own
only the modules DA Media Browser has too (or its own words for them), never the
interface, the settings or the engine: what is app-only is passed in."""

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = Path(__file__).resolve().parents[1] / "assistant"
SHARED = ("services.py", "levels.py", "chutes_e2ee.py", "chutes.py", "tinfoil_direct.py",
          "anthropic.py")
# This app's modules a shared one may use: DA Media Browser has each (privacy.py is its
# private_ai.py; streams.read_lines its netguard.lines_of; proxy.for_url its
# netguard.env_proxy_for), or one of these shared modules themselves.
ALLOWED_OWN = {"netguard", "safejson", "privacy", "dcap", "streams", "sealed", "proxy",
               "services", "levels", "chutes_e2ee", "chutes"}
ALLOWED_OUTSIDE = {"cryptography", "requests", "httpx", "ehbp", "tinfoil"}


def imports(path):
    """(the app's own modules, the other packages) `path` imports, anywhere in it."""
    own, outside = set(), set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                if node.module:
                    own.add(node.module.split(".")[0])
                else:
                    own.update(alias.name for alias in node.names)
            else:
                outside.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            outside.update(alias.name.split(".")[0] for alias in node.names)
    return own, outside


class TheSharedCoreTakesItsHooks(unittest.TestCase):
    def test_a_shared_module_imports_nothing_of_this_apps_but_what_both_apps_have(self):
        for name in SHARED:
            with self.subTest(name=name):
                own, outside = imports(HERE / name)
                self.assertLessEqual(own, ALLOWED_OWN, f"{name} imports app-only code")
                stray = {package for package in outside
                         if package not in ALLOWED_OUTSIDE
                         and package not in sys.stdlib_module_names}
                self.assertEqual(stray, set(), f"{name} imports {stray}")

    def test_the_pure_ones_touch_neither_the_network_nor_a_file(self):
        for name in ("services.py", "levels.py", "chutes_e2ee.py"):
            with self.subTest(name=name):
                own, outside = imports(HERE / name)
                self.assertFalse(outside & {"requests", "httpx", "socket", "urllib3",
                                            "tinfoil", "ehbp"})
                self.assertFalse(own & {"netguard", "sealed", "proxy", "chutes"})


if __name__ == "__main__":
    unittest.main()
