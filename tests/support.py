"""Imported by every test module before anything of the app's: a cache of the suite's own.

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
