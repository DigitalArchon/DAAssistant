"""The files every DA app keeps alike, held to their canonical copy (the DA apps' security
standard, Nov 14 2026): dcap.py, nras.py and ethsig.py are DA Toolkit's
(src/datoolkit/llm/), line for line. They may differ only in their module docstring
(which names the paths of the app they are in), their imports, and the HTTP call: DA
Toolkit's httpx client where this app has netguard's requests session (TRANSPORT, every
one of them listed). Anything else is drift, which fails here rather than waiting for a
review: a change is made in DA Toolkit's copy first, then carried here.

Skipped where DA Toolkit's repository is not beside this one.

Not here: sdk_guard.py. SealedLore's (= DA Toolkit's) holds Tinfoil's and Sigstore's
fetches to a redirect rule of its own; this app's guard (sealed.guard_sdk_requests) holds
them to netguard's, which is stricter - the whole address rule for a redirect's target, a
fetcher given as None in its place still guarded, the refused redirect named in the
attestation's error - and the canonical copy has not taken those up yet
(tests/test_sdk_redirects.py fails 11 of 19 against it).
"""

import ast
import difflib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = Path(__file__).resolve().parents[1] / "assistant"
TOOLKIT = Path(__file__).resolve().parents[2] / "DAToolkit" / "src" / "datoolkit" / "llm"
SHARED = ("dcap.py", "nras.py", "ethsig.py")

# DA Toolkit's words for the HTTP call, and this app's: the only words of the code that
# may differ.
TRANSPORT = (
    ("make_client(ROOT_CRL_URL, timeout=httpx.Timeout(TIMEOUT_SECONDS))",
     "netguard.session(follow=True)"),
    ("make_client(JWKS_URL, timeout=httpx.Timeout(TIMEOUT_SECONDS))",
     "netguard.session(follow=True)"),
    ("httpx.Client", "requests.Session"),
    ("httpx.HTTPError", "requests.exceptions.RequestException"),
    ("ProviderError", "requests.exceptions.RequestException"),
)
# Lines of this app's own beside the imports: datetime.UTC is Python 3.11's, and this app
# runs on 3.10 too.
OWN_LINES = {
    "UTC = timezone.utc  # datetime.UTC is Python 3.11's; this app runs on 3.10 too",
}


def comparable(text, transport=()):
    """`text`'s lines without its leading comments, its module docstring, its top-level
    imports and the lines OWN_LINES allows, with `transport`'s words put in for the
    canonical copy's, and a run of blank lines (where the imports were) as one: what the
    copies must hold alike."""
    tree = ast.parse(text)
    dropped = set()
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        first = body.pop(0)
        dropped.update(range(1, first.end_lineno + 1))      # the comments before it too
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            dropped.update(range(node.lineno, node.end_lineno + 1))
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        if number in dropped or line in OWN_LINES:
            continue
        for theirs, ours in transport:
            line = line.replace(theirs, ours)
        if line.strip() or (lines and lines[-1].strip()):     # where lines were left
            lines.append(line)                                  # out, one blank line
    return lines


@unittest.skipUnless(TOOLKIT.is_dir(), "DA Toolkit's repository is not beside this one")
class SharedFilesAreDaToolkitsCopy(unittest.TestCase):
    def test_the_shared_files_are_da_toolkits_copy_line_for_line(self):
        for name in SHARED:
            with self.subTest(name=name):
                ours = comparable((HERE / name).read_text(encoding="utf-8"))
                canonical = comparable((TOOLKIT / name).read_text(encoding="utf-8"),
                                       TRANSPORT)
                if ours != canonical:
                    diff = "\n".join(difflib.unified_diff(
                        canonical, ours, "DA Toolkit", "DA Assistant", lineterm="", n=1))
                    self.fail(f"assistant/{name} has drifted from DA Toolkit's copy: "
                              f"change it there first, then carry it here\n{diff[:4000]}")


class TheComparisonForgivesOnlyWhatItSays(unittest.TestCase):
    """What the comparison leaves out, and nothing more: a changed line anywhere else is
    still a difference."""

    def test_only_the_docstring_imports_and_the_listed_transport(self):
        theirs = ('"""Docstring naming llm/tee.py,\nover two lines."""\n\nimport httpx\n\n'
                  "from .secure_http import ProviderError, make_client\n\n"
                  "def get(session: httpx.Client):\n"
                  "    try:\n        return session.get(URL)\n"
                  "    except (httpx.HTTPError, ProviderError):\n        raise\n")
        ours = ('# A comment\n"""Docstring naming privacy.py."""\n\nimport requests\n\n'
                "from . import netguard\n\n"
                "UTC = timezone.utc  # datetime.UTC is Python 3.11's; this app runs on "
                "3.10 too\n\n"
                "def get(session: requests.Session):\n"
                "    try:\n        return session.get(URL)\n"
                "    except (requests.exceptions.RequestException, "
                "requests.exceptions.RequestException):\n        raise\n")
        self.assertEqual(comparable(ours), comparable(theirs, TRANSPORT))
        for changed in (ours.replace("session.get(URL)", "session.get(URL, verify=False)"),
                        ours.replace("requests.Session", "object"),
                        ours.replace("        raise\n", "        return None\n")):
            self.assertNotEqual(comparable(changed), comparable(theirs, TRANSPORT))

    def test_an_import_inside_a_function_is_code(self):
        text = '"""Doc."""\nimport os\n\ndef f():\n    import ctypes\n    return ctypes\n'
        self.assertEqual(comparable(text), ["def f():", "    import ctypes",
                                            "    return ctypes"])


if __name__ == "__main__":
    unittest.main()
