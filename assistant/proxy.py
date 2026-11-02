"""A proxy for the app's own requests: Settings > Advanced > Proxy.

Left empty, the environment's proxy settings stand, as they always have: HTTPS_PROXY,
ALL_PROXY and the rest, which requests and httpx read for themselves. Set, every request
the app makes goes through it - to the models, the web search, the model lists, the
attestation services, and Tinfoil's own client, which reads the same variables - so it
is by those variables, set for this process alone (hostenv.set_own): the programs the
app starts, a browser among them, get the environment they had. A server on this
machine or its own network is still reached directly: NO_PROXY names those, as
netguard does, so a model server next door is not sent the long way round.

`socks5h://` is what reaches a Tor onion service - Tor's SOCKS port is usually
`socks5h://127.0.0.1:9050` - because the proxy, not this machine, resolves the name,
and only Tor can resolve an onion's. A SOCKS proxy needs PySocks for requests and
socksio for httpx, which the AppImage carries; from source they are python3-socks and
python3-socksio (Debian, Ubuntu), python3-pysocks and python3-socksio (Fedora),
python-pysocks and python-socksio (Arch).

Certificates are checked through a proxy as everywhere else: an https request is
tunnelled through it, end to end.
"""

import importlib.util
import os
from urllib.parse import urlparse

from . import hostenv, netguard

SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")
VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
             "all_proxy")
# This machine and its own network, as netguard counts them, in NO_PROXY's spelling.
LOCAL = ("localhost", ".localhost", ".local", "127.0.0.0/8", "::1", "10.0.0.0/8",
         "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")


def _missing_for_socks():
    """What a SOCKS proxy needs that is not installed: [package names]."""
    missing = []
    if importlib.util.find_spec("socks") is None:
        missing.append("PySocks (python3-socks)")
    if importlib.util.find_spec("socksio") is None:
        missing.append("socksio (python3-socksio)")
    return missing


def problem(url):
    """Why `url` cannot be the proxy, or None when it can, or is empty."""
    text = (url or "").strip()
    if not text:
        return None
    if netguard.UNSAFE.search(text):
        return f"Not a proxy address: {text!r}."
    try:
        parsed = urlparse(text)
        scheme, host = parsed.scheme.lower(), parsed.hostname
        parsed.port                 # raises on a port that is not a number
    except ValueError:
        return f"Not a proxy address: {text!r}."
    if scheme not in SCHEMES or not host:
        return (f"Not a proxy address: {text!r}. It starts with socks5h://, socks5://, "
                f"http:// or https://.")
    if scheme.startswith("socks"):
        missing = _missing_for_socks()
        if missing:
            return f"A SOCKS proxy needs {' and '.join(missing)}, not installed here."
    return None


def reaches_onion():
    """Whether this process's requests go through a proxy that looks names up itself,
    which an onion's must: socks5h:// or socks4a://."""
    return any((os.environ.get(name) or "").lower().startswith(("socks5h://", "socks4a://"))
               for name in VARIABLES)


def apply(config):
    """Send this process's requests through config.proxy - or, with none set or one
    that cannot be used, leave them to the environment's settings again."""
    url = (getattr(config, "proxy", "") or "").strip()
    names = VARIABLES + ("NO_PROXY", "no_proxy")
    if not url or problem(url):
        for name in names:
            hostenv.restore_own(name)
        return False
    for name in VARIABLES:
        hostenv.set_own(name, url)
    kept = [part.strip() for part in (hostenv.original("NO_PROXY")
                                      or hostenv.original("no_proxy") or "").split(",")
            if part.strip()]
    no_proxy = ",".join(dict.fromkeys(kept + list(LOCAL)))
    hostenv.set_own("NO_PROXY", no_proxy)
    hostenv.set_own("no_proxy", no_proxy)
    return True
