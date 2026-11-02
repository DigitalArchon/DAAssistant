"""HTTPS, or nothing.

Every request this app makes carries the API key, and most carry what was on the screen
or the clipboard. So an address is used only if it is https - or if it never leaves the
local network, which is how a model server on this machine or the one next to it is
reached, and where there is usually no certificate to be had:

    localhost, *.localhost, *.local
    127.0.0.0/8, ::1
    10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
    169.254.0.0/16, fe80::/10            (link-local)
    fc00::/7                             (unique local)

- or if it is a Tor onion service (*.onion), which Tor encrypts and authenticates end to
end whatever the scheme: Tor Browser counts an onion as secure for the same reason.

Anything else over plain http is refused, with the reason, before a byte is sent: not
rewritten to https, which might be another server altogether, and not allowed with a
warning. Certificates are always verified; nothing here or anywhere else switches that
off.

A name is judged as written. It is not looked up to see where it points: an address
that resolves to this network today can resolve elsewhere tomorrow. And it is judged as
requests will read it: an address with a backslash, a space or a control character in
it is refused outright, because Python's parser and urllib3's do not agree on where its
host ends - `http://evil.com\\@localhost` is localhost to one and evil.com to the other.

A redirect is held to the same rule (session()): followed only to an address that could
have been used in the first place, and never from https to plain http.
"""

import ipaddress
import re
from urllib.parse import unquote, urljoin, urlparse

try:
    from requests.exceptions import InvalidURL as _InvalidURL
except ImportError:         # the policy itself needs nothing but the standard library
    _InvalidURL = ValueError

LOCAL_NAMES = ("localhost",)
LOCAL_SUFFIXES = (".localhost", ".local")
ONION_SUFFIX = ".onion"
LOCAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10", "fc00::/7"))
UNSAFE = re.compile(r"[\\\s\x00-\x1f\x7f]")


class InsecureURL(ValueError):
    pass


class RedirectRefused(_InvalidURL):
    """A redirect to somewhere the request could not have been sent in the first place.
    A requests exception, so that every caller already handles it, and a ValueError."""


def is_local(host):
    """Whether `host`, as written, is on this machine or its own network."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return any(address.version == network.version and address in network
               for network in LOCAL_NETWORKS)


def is_onion(host):
    """Whether `host` is a Tor onion service."""
    return (host or "").strip().lower().rstrip(".").endswith(ONION_SUFFIX)


def _same_host(ours, theirs):
    """Whether urllib3's reading of a host is the one netguard judged. Brackets, case,
    a trailing dot and a percent-encoded zone are only spelling."""
    def plain(host):
        return unquote((host or "").strip("[]").lower().rstrip("."))
    return plain(ours) == plain(theirs)


def _requests_host(text):
    """The host requests will connect to for `text`, by urllib3's parser - or None when
    urllib3 is not there to ask, and "" when it cannot read the address at all."""
    try:
        from urllib3.util import parse_url
    except ImportError:
        return None
    try:
        return parse_url(text).host or ""
    except Exception:
        return ""


def problem(url):
    """Why `url` may not be used, or None when it may."""
    text = (url or "").strip()
    if not text:
        return "No address given."
    if UNSAFE.search(text):
        return (f"Not a web address: {text!r}. An address has no spaces, backslashes or "
                f"control characters in it.")
    try:
        parsed = urlparse(text)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
    except ValueError:              # "http://[fd00::5", half typed
        return f"Not a web address: {text!r}."
    if scheme not in ("http", "https") or not host:
        return (f"Not a web address: {text!r}. It must start with https:// - or "
                f"http:// for this machine or its local network.")
    if scheme == "https":
        return None
    if not (is_local(host) or is_onion(host)):
        return (f"{text} is not https, so the API key and everything sent with it would "
                f"cross the network unencrypted. Use an https:// address. Plain http is "
                f"accepted only for this machine and its local network (localhost, "
                f"10.x, 172.16-31.x, 192.168.x, .local), and for Tor onion services.")
    theirs = _requests_host(text)
    if theirs is not None and not _same_host(host, theirs):
        return f"Not a web address: {text!r}."
    return None


def check(url):
    """Raise InsecureURL unless `url` may be used. Returns it stripped."""
    found = problem(url)
    if found:
        raise InsecureURL(found)
    return url.strip()


def redirect_problem(source, target):
    """Why a redirect from `source` to `target` may not be followed, or None."""
    found = problem(target)
    if found:
        return f"The server sent the request on to {target}, which was not followed: {found}"
    if urlparse(source).scheme.lower() == "https" and \
            urlparse(target).scheme.lower() != "https":
        return (f"The server sent the request on from https to {target}, which was not "
                f"followed: an https request is never carried on over plain http.")
    return None


def session():
    """A requests Session that follows a redirect only where netguard would have let the
    request go in the first place, and never from https to plain http.

    requests follows redirects by itself, and a 307 or 308 carries the body along: the
    whole conversation, captures and all. It takes the Authorization header off when the
    host changes, but nothing else, so without this a provider - or anything answering
    at its address - could send the next copy of a conversation anywhere, in the clear.
    A refused redirect raises RedirectRefused, before anything is sent to it."""
    import requests

    class GuardedSession(requests.Session):
        def get_redirect_target(self, response):
            target = super().get_redirect_target(response)
            if target:
                found = redirect_problem(response.url, urljoin(response.url, target))
                if found:
                    raise RedirectRefused(found, response=response)
            return target

    return GuardedSession()
