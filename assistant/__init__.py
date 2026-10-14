"""DA Assistant - screen/clipboard queries against an OpenAI-compatible API."""

__version__ = "3.3.0"
APP_ID = "org.digitalarchon.DAAssistant"
APP_NAME = "DA Assistant"

# Before anything else of ours imports requests: see vendored.py.
from . import vendored as _vendored  # noqa: E402

_vendored.activate()

# Before anything imports gi: in the AppImage, what the host's GTK stack lacks. See bundle.py.
from . import bundle as _bundle  # noqa: E402

_bundle.fill_gaps()
