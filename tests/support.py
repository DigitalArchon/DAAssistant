"""Imported by every test module before anything of the app's: no core dumps, as the app
has none, and a cache of the suite's own.

The suite turns core dumps off first, with the app's own guard (coredumps.no_core_dumps),
before any test runs: its process undumpable, its memory left out of any dump, its core
size limit 1. Its WebKit processes - every test window's - and anything else a test starts
inherit the limit of 1 and the filter of 0, so one that crashes leaves no core either: a
test's WebKitWebProcess that aborted left a core of 5-6 MB in systemd-coredump on the lab's
Fedora and Omarchy (Nov 14 2026). Discovery imports every test module before it runs any,
and each imports this first.

The app keeps its error log, the WebKit breadcrumb, model lists, the dictionary index and
debug captures under $XDG_CACHE_HOME/da-assistant, decided when it is first imported
(assistant/paths.py). Tests simulate a WebKit that fails, count strikes against WebViews
and write model lists; all of that has to land somewhere that goes away with the run, not
in the cache of whoever runs the tests - where a simulated failure reads as a real one,
and a run killed mid-window leaves a strike that switches their app to plain text.
"""

import atexit
import os
import shutil
import tempfile

from assistant.coredumps import no_core_dumps

no_core_dumps()

if "DA_ASSISTANT_TEST_CACHE" not in os.environ:
    CACHE = tempfile.mkdtemp(prefix="da-assistant-tests-")
    os.environ["DA_ASSISTANT_TEST_CACHE"] = CACHE
    os.environ["XDG_CACHE_HOME"] = CACHE
    atexit.register(shutil.rmtree, CACHE, ignore_errors=True)
else:
    CACHE = os.environ["DA_ASSISTANT_TEST_CACHE"]

# No test reaches the network for a model list. A request that finds no cached list
# fetches one first (models.ensure_listed), from wherever the test's settings point -
# nano-gpt.com with a key of "k", as often as not. A test that wants a list patches
# models._get or models._post itself, which this leaves it free to do.
from assistant import models as _models  # noqa: E402


def _offline(url, **_kwargs):
    raise _models.requests.exceptions.ConnectionError(
        f"the tests make no network requests ({url})")


_models._get = _models._post = _offline
