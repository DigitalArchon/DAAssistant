"""enc://, the address scheme for the encyclopedia DVD's pictures and animations.

Registered on a WebKit context of the app's own, which every view the app makes uses
(context()):

    enc://image/<id>          a picture, read from the DVD's IMAGE_DATA
    enc://flash/<name>.swf    a page that plays that animation with Ruffle
    enc://flash/ruffle/<file> Ruffle's own scripts and WebAssembly
    enc://flash/swf/<name>    the animation itself, for Ruffle to fetch

The Flash page, Ruffle and the animation share one origin, enc://flash, so Ruffle's
fetches are same-origin and need nothing relaxed. Films and sounds are not served
here: WebKit plays nothing from a custom scheme, and plays the DVD's files directly as
file:// addresses (see reference/encyclopedia_html.py).

Only what the DVD and Ruffle hold is served, by number or by a file name checked
against a pattern - never a path from the address.

Not WebKit's default context: WebKit frees that one from libc's exit handlers, after
Python has shut down, and freeing the Python handler registered on it then segfaulted
the process on every quit. A context of our own is freed while Python still runs.
"""

import html
import re

from .. import gtkcompat
from gi.repository import Gio, GLib

from ..reference import ruffle

if gtkcompat.has_webkit():
    from gi.repository import WebKit2
else:  # pragma: no cover
    WebKit2 = None

SCHEME = "enc"
_SWF = re.compile(r"^[\w-]+\.swf$", re.I)

_encyclopedia = None
_context = None


def install(encyclopedia):
    """Serve enc:// from `encyclopedia`, the latest one given. None changes nothing."""
    global _encyclopedia
    if encyclopedia is not None:
        _encyclopedia = encyclopedia


def context():
    """The WebKit context every view of the app's is made with, enc:// on it."""
    global _context
    if _context is None and WebKit2 is not None:
        _context = WebKit2.WebContext.new()
        _context.register_uri_scheme(SCHEME, _on_request)
        security = _context.get_security_manager()
        security.register_uri_scheme_as_secure(SCHEME)
        security.register_uri_scheme_as_cors_enabled(SCHEME)
    return _context


def resolve(uri, encyclopedia=None):
    """(bytes, MIME type) for an enc:// address, or None. Kept apart from WebKit so it
    can be tested on its own."""
    encyclopedia = encyclopedia or _encyclopedia
    if encyclopedia is None or not uri.startswith(f"{SCHEME}://"):
        return None
    path = uri[len(SCHEME) + 3:].split("?", 1)[0].split("#", 1)[0]
    kind, _, rest = path.partition("/")
    if kind == "image" and rest.isdigit():
        return encyclopedia.image(int(rest))
    if kind == "flash":
        if rest.startswith("ruffle/"):
            return ruffle.file(rest[len("ruffle/"):])
        if rest.startswith("swf/") and _SWF.match(rest[4:]):
            found = encyclopedia.media_path(rest[4:])
            if found:
                with open(found, "rb") as f:
                    return f.read(), "application/x-shockwave-flash"
            return None
        if _SWF.match(rest):
            return flash_page(rest).encode("utf-8"), "text/html"
    return None


def _on_request(request):
    try:
        found = resolve(request.get_uri())
    except Exception as e:      # an unreadable DVD must not take the view down
        found = None
        print(f"[enc] {request.get_uri()}: {e}")
    if found is None:
        request.finish_error(GLib.Error.new_literal(
            Gio.io_error_quark(), f"Not found: {request.get_uri()}",
            int(Gio.IOErrorEnum.NOT_FOUND)))
        return
    data, mime = found
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
    request.finish(stream, len(data), mime)


FLASH_CSP = ("default-src 'none'; script-src enc: 'unsafe-inline' 'wasm-unsafe-eval'; "
             "connect-src enc:; img-src enc: data: blob:; media-src enc: blob:; "
             "style-src 'unsafe-inline'; worker-src blob:")


def flash_page(name):
    """A page that plays `name` with Ruffle, or says how to install it."""
    if ruffle.location() is None:
        body = ("<p>Flash animations are played with Ruffle, which is not installed.</p>"
                "<p>Run <code>tools/install-ruffle.sh</code> from the assistant's folder, "
                "then open the animation again.</p>")
        return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
                f"body{{font:15px system-ui;margin:3em;color:#ccc;background:#222}}"
                f"</style></head><body>{body}</body></html>")
    swf = html.escape(f"enc://flash/swf/{name}")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{FLASH_CSP}">
<style>
html, body {{ margin: 0; height: 100%; background: #202124; overflow: hidden; }}
#player {{ position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; }}
#player > * {{ display: block; width: 100vw; height: 100vh; }}
</style>
<script src="enc://flash/ruffle/ruffle.js"></script></head>
<body><div id="player"></div><script>
window.RufflePlayer = window.RufflePlayer || {{}};
window.RufflePlayer.config = {{
  autoplay: "on", unmuteOverlay: "hidden", splashScreen: false, letterbox: "on",
  contextMenu: "rightClickOnly", showSwfDownload: false, logLevel: "warn",
  openUrlMode: "deny", allowNetworking: "none"
}};
var player = window.RufflePlayer.newest().createPlayer();
document.getElementById("player").appendChild(player);
player.load({{ url: "{swf}" }});
</script></body></html>"""
