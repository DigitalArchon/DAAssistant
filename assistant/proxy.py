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


def in_use():
    """Whether this process's requests go through a proxy: the app's, or the
    environment's."""
    return any(os.environ.get(name) for name in VARIABLES)


def for_url(url):
    """The proxy this process's requests to `url` go through, as requests and httpx
    read it from the environment, where apply() puts the app's own - or None.

    For a client that is given a transport of its own, which httpx then sends nowhere
    but where that transport says, its environment unread: the sealed client's
    (sealed.py), whose one request carried the conversation past the proxy."""
    import urllib.request
    parsed = urlparse(url)
    if urllib.request.proxy_bypass_environment(parsed.hostname or ""):
        return None
    found = urllib.request.getproxies_environment()
    chosen = found.get(parsed.scheme.lower()) or found.get("all")
    if not chosen:
        return None
    return chosen if "://" in chosen else f"http://{chosen}"


def let_tuf_use_socks():
    """Let TUF's fetcher through a SOCKS proxy too: it fetches Sigstore's trust root,
    which Private Mode's attestation checks the enclave's release against.

    tuf.ngclient builds a urllib3 ProxyManager for whatever proxy the environment names,
    and that speaks http and https only. Through Tor, live (Nov 2026), the refresh
    failed - "Failed to refresh TUF metadata" - and no private/ model could be attested.
    For a SOCKS proxy it is given urllib3's SOCKS manager instead, which uses PySocks,
    as a SOCKS proxy needs anyway; any other proxy is left to TUF as it was. Returns
    whether TUF's proxy handling was found where this expects it."""
    try:
        from tuf.ngclient._internal import proxy as tuf_proxy
    except ImportError:
        return False
    original = getattr(tuf_proxy, "ProxyManager", None)
    if original is None:
        return False
    if getattr(original, "da_assistant_socks", False):
        return True

    def manager(proxy_url, *args, **kwargs):
        if str(proxy_url).lower().startswith("socks"):
            from urllib3.contrib.socks import SOCKSProxyManager
            return SOCKSProxyManager(proxy_url, *args, **kwargs)
        return original(proxy_url, *args, **kwargs)

    manager.da_assistant_socks = True
    tuf_proxy.ProxyManager = manager
    return True


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
