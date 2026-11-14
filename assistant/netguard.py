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

A redirect is not followed at all by a request that carries a key, the conversation, a
question or a search query (session(), as every such request's client makes it): it is
an error naming where the address pointed, and nothing is sent there. A public fetch that
carries neither - Intel's and NVIDIA's evidence, Tinfoil's releases - may follow one
(session(follow=True)), but only to an address that could have been used in the first
place, never from https to plain http, and never from a server elsewhere into this
machine or its network.
"""

import ipaddress
import re
from urllib.parse import urljoin, urlparse

try:
    from requests.exceptions import ContentDecodingError as _Undecodable
    from requests.exceptions import InvalidURL as _InvalidURL
    from requests.exceptions import RequestException as _RequestException
except ImportError:         # the policy itself needs nothing but the standard library
    _InvalidURL = _RequestException = _Undecodable = ValueError

# How much of an answer is read whole (session): a reply not streamed, a model list, an
# attestation, Intel's and NVIDIA's answers - decoded, counted as it is unpacked. Past
# it, TooLarge, and nothing more is read. A streamed reply is held to its lines instead
# (streams.read_lines, 32 MB a line).
MAX_BODY_BYTES = 32 * 1024 * 1024
# The most of an error's, or a redirect's, answer that is read: its words are a few
# hundred bytes, and the rest is left unread.
MAX_ERROR_BYTES = 64 * 1024
# What every request says it takes compressed: gzip and deflate, whose output for each
# piece read is bounded, so that the caps count what was unpacked. Never br or zstd,
# whose output for one piece has no bound: requests asks for them itself wherever their
# libraries are installed (urllib3's make_headers). An answer coded any other way is
# refused unread (Undecodable). The DA apps' security standard (Nov 2026).
ACCEPT_ENCODING = "gzip, deflate"
_ASKED_CODINGS = ("gzip", "deflate", "identity")
_READ_PIECE = 64 * 1024

LOCAL_NAMES = ("localhost",)
LOCAL_SUFFIXES = (".localhost", ".local")
ONION_SUFFIX = ".onion"
LOCAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10", "fc00::/7"))
UNSAFE = re.compile(r"[\\\s\x00-\x1f\x7f]")
# Everything of a text up to its last "@" before a query or a fragment, the scheme and
# "//" aside: where an address that cannot be read may have a user name and password.
_UP_TO_AT = re.compile(r"^((?:[^:/?#]+:)?//)?[^?#]*@")


class InsecureURL(ValueError):
    pass


class RedirectRefused(_InvalidURL):
    """A redirect to somewhere the request could not have been sent in the first place.
    A requests exception, so that every caller already handles it, and a ValueError."""


class TooLarge(_RequestException):
    """An answer far larger than any it should be, once unpacked: not read on. A requests
    exception, so that every caller already says it as a failed request."""


class Undecodable(_Undecodable):
    """An answer compressed in a way it was not asked for (ACCEPT_ENCODING): refused
    unread, as requests would refuse one it could not decode."""


class ProxyRefused(_InvalidURL):
    """A request not made, because the proxy set in Settings cannot be used and it would
    have gone around it (proxy.refusal())."""


def _unescaped(host):
    """`host` with an IPv6 literal's zone taken off - or None if it has a "%" that is
    not a zone's. Only an IPv6 address has a zone ("fe80::1%eth0", written %25 in a
    URL); anywhere else a "%" is an escape, which urllib3 decodes into another name:
    "127.0.0.1%2eevil.com" was judged 127.0.0.1, and requests went to 127.0.0.1.evil.com."""
    host = host.strip("[]")
    if "%" not in host:
        return host
    address, _zone = host.split("%", 1)
    return address if ":" in address else None


def is_local(host):
    """Whether `host`, as written, is on this machine or its own network.

    An address written with a trailing dot - "192.168.1.50." - is not that address: no
    parser of addresses takes it, so the resolver looks it up as a name, and DNS says
    where it goes. It used to be judged as the address without its dot, and plain http
    to it allowed. A name with one is the same name: "localhost." is localhost. From DA
    Vibe Manager's netpolicy (Nov 2026)."""
    text = (host or "").strip().lower()
    host = _unescaped(text.rstrip("."))
    if not host:
        return False
    if host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if text.endswith("."):
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return any(address.version == network.version and address in network
               for network in LOCAL_NETWORKS)


def is_loopback(host):
    """Whether `host`, as written, is this machine itself - 127.0.0.0/8, ::1, localhost
    and a name under .localhost - where what is sent never leaves it. Any other local
    host (is_local) is taken for another machine, nothing being looked up, and plain http
    to it can be read by anyone on the network between, a café's or a hotel's Wi-Fi
    included. As SealedLore's is_loopback_host (Nov 2026)."""
    text = (host or "").strip().lower()
    if not is_local(text):
        return False
    name = _unescaped(text.rstrip(".")) or ""
    if name in LOCAL_NAMES or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def in_the_clear(url):
    """Whether a request to `url` crosses a network unencrypted: plain http - or a proxy
    spoken to in anything but https, SOCKS included - to another machine on this one
    (is_local, not is_loopback). Allowed - problem() lets it go - but anyone on that
    network can read it, a key and a password with it. Anywhere else plain http is
    refused outright (problem)."""
    try:
        parsed = urlparse(str(url or "").strip())
        host = parsed.hostname or ""
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    return bool(scheme) and scheme != "https" and is_local(host) and not is_loopback(host)


def credentials(url):
    """The user name and password written into `url`, as one text - "" if it has none,
    or cannot be read as an address. They go with every request to it, as Basic
    credentials: in the clear over plain http, key or no key."""
    try:
        parsed = urlparse(str(url or "").strip())
        return ":".join(part for part in (parsed.username, parsed.password) if part)
    except ValueError:
        return ""


def is_onion(host):
    """Whether `host` is a Tor onion service."""
    host = (host or "").strip().lower().rstrip(".")
    return "%" not in host and host.endswith(ONION_SUFFIX)


def _same_host(ours, theirs):
    """Whether urllib3's reading of a host is the one netguard judged. Brackets, case,
    a trailing dot and a percent-encoded zone are only spelling; any other escape is
    not, and decoding it on both sides would have hidden the very difference this is
    here to find."""
    def plain(host):
        return (host or "").strip("[]").lower().rstrip(".").replace("%25", "%")
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
    """Why `url` may not be used, or None when it may. The address is named as shown()
    has it, without a password written into it: --probe prints these, the chat,
    Settings and the startup notice show them, and they are copied into bug reports."""
    text = (url or "").strip()
    if not text:
        return "No address given."
    if UNSAFE.search(text):
        return (f"Not a web address: {shown(text)!r}. An address has no spaces, "
                f"backslashes or control characters in it.")
    try:
        parsed = urlparse(text)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        # Raises on a port no address can have, which requests would refuse in words
        # that quote the address whole: "Failed to parse: https://alice:secret@...".
        parsed.port
    except ValueError:              # "http://[fd00::5", half typed
        return f"Not a web address: {shown(text)!r}."
    if scheme not in ("http", "https") or not host:
        return (f"Not a web address: {shown(text)!r}. It must start with https:// - or "
                f"http:// for this machine or its local network.")
    if scheme == "https":
        return None
    if not (is_local(host) or is_onion(host)):
        return (f"{shown(text)} is not https, so the API key and everything sent with "
                f"it would cross the network unencrypted. Use an https:// address. "
                f"Plain http is accepted only for this machine and its local network "
                f"(localhost, 10.x, 172.16-31.x, 192.168.x, .local), and for Tor onion "
                f"services.")
    theirs = _requests_host(text)
    if theirs is not None and not _same_host(host, theirs):
        return f"Not a web address: {shown(text)!r}."
    return None


# What a browser takes off both ends of a link's address, and out of it anywhere: C0
# controls and spaces, and tabs and line breaks (WHATWG URL parsing).
_LINK_ENDS = "".join(map(chr, range(0x21)))
_LINK_BREAKS = re.compile(r"[\t\n\r]")


def insecure_link(url):
    """Whether a link to `url` would be opened over plain http somewhere other than this
    machine, its own network or an onion service: a page anyone on the way could read
    and change. https, mailto: and every other scheme are not, whatever they are.

    For links in what the app shows - an answer, a dictionary's entry, an article - which
    the browser opens, and reads as a browser does: the scheme is found as it would find
    it, and a host that cannot be judged as written (a backslash or a space in the
    address, a % escape, no host at all) is taken for one elsewhere. Only an address
    that is plainly local, by the rules every request of the app's own is held to, goes
    without the warning."""
    text = str(url or "").strip(_LINK_ENDS)
    if not _LINK_BREAKS.sub("", text).lower().startswith("http:"):
        return False
    if UNSAFE.search(text):
        return True
    try:
        host = urlparse(text).hostname
    except ValueError:
        return True
    return not (is_local(host) or is_onion(host))


def shown(url):
    """`url` as it may be shown, or copied into a bug report: without the user name and
    password an address may carry (https://alice:secret@host/v1).

    For any text at all, and never raising: what is said of an address that cannot be
    used names it too. One that cannot be read as an address - half typed, without a
    scheme ("alice:secret@host"), or with a port no address can have - says nothing
    certain of where its host begins, so all it has up to its last "@" goes."""
    text = str(url or "")
    try:
        parsed = urlparse(text)
        parsed.port                 # raises on a port that is not a number
    except ValueError:
        parsed = None
    if parsed is None or not parsed.netloc:
        return _UP_TO_AT.sub(r"\1…@", text, count=1)
    if "@" not in parsed.netloc:
        return text
    return parsed._replace(netloc="…@" + parsed.netloc.rsplit("@", 1)[1]).geturl()


def without_password(url):
    """`url` with the user name and password written into it taken out - as shown() does,
    but leaving an address, not a placeholder, where they were: what config.json keeps
    of an address whose password is in the keychain. Text with neither is returned as
    it is."""
    text = str(url or "")
    if "@" not in text:
        return text
    try:
        parsed = urlparse(text)
        parsed.port                 # raises on a port that is not a number
    except ValueError:
        parsed = None
    if parsed is None or not parsed.netloc:
        return _UP_TO_AT.sub(r"\1", text, count=1)
    if "@" not in parsed.netloc:
        return text
    return parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[1]).geturl()


def origin(url):
    """`url`'s origin - scheme, host and port - as one text, "https://host:443": what an
    API key is kept for (config.Config.key_withheld). The host's case and a port left at
    its scheme's default are only spelling; a user name and password are not part of it.
    "" for anything that cannot be read as a web address."""
    try:
        parsed = urlparse(str(url or "").strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if not host or scheme not in ("http", "https"):
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port or (443 if scheme == 'https' else 80)}"


def key_problem(key, name="the provider"):
    """Why `key` cannot be sent as an API key, or None. A key is plain ASCII with no
    spaces in it; one pasted with a line break, or a zero-width space from a web page,
    was refused by requests in words that quoted it whole, and those were shown in the
    chat - Copy, Save and bug reports included - or raised from deep in http.client."""
    if key and (not key.isascii() or UNSAFE.search(key) or " " in key):
        return (f"The API key for {name} has a space, a line break or another character "
                f"no API key has in it, picked up where it was copied from, perhaps. "
                f"Enter it again in Settings > Providers.")
    return None


def check(url):
    """Raise InsecureURL unless `url` may be used. Returns it stripped."""
    found = problem(url)
    if found:
        raise InsecureURL(found)
    return url.strip()


def redirected(source, target, status):
    """What a request answered with a redirect it did not follow says: where it pointed,
    named without the password an address may carry (shown()), and what to do if the
    address has moved. A redirect re-sends the request - a 307 or 308 its body, the
    conversation, too - to wherever it points, so none is followed by a request that
    carries a key or anything of the user's (session()). From SealedLore (e947938)."""
    where = f" to {shown(target)}" if target else ""
    return (f"{shown(source)} redirected (HTTP {status}){where}, which was not followed: "
            f"that would have sent the request, and what it carries, there. If the "
            f"address has moved, put the new one in Settings > Providers.")


def redirect_problem(source, target):
    """Why a redirect from `source` to `target` may not be followed, or None.

    Into this machine or its network, only from a server there itself. Held to the rule
    for addresses alone, a provider out on the internet - or anything answering at its
    address - could send a request on to the router's admin page, a printer or a model
    server here, a 307's body, the conversation, along with it, over https as readily as
    over http. A server on this network may still send a request on to another there;
    an onion service is not on this network. (SealedLore's rule, Nov 2026.)"""
    found = problem(target)
    if found:
        return (f"The server sent the request on to {shown(target)}, which was not "
                f"followed: {found}")
    if urlparse(source).scheme.lower() == "https" and \
            urlparse(target).scheme.lower() != "https":
        return (f"The server sent the request on from https to {shown(target)}, which "
                f"was not followed: an https request is never carried on over plain "
                f"http.")
    try:
        from_here = is_local(urlparse(source).hostname)
    except ValueError:              # an address that cannot be read is no local one
        from_here = False
    if is_local(urlparse(target).hostname) and not from_here:
        return (f"The server sent the request on to {shown(target)}, on this machine or "
                f"its network, which was not followed: a server elsewhere never sends a "
                f"request in there.")
    return None


def session(follow=False):
    """A requests Session that follows no redirect: one raises RedirectRefused, saying
    where it pointed (redirected), before anything is sent there. Every request that
    carries a key, the conversation, a question or a search query is made with one: the
    models, their lists, the web search. Up to 4.3.1 they followed a redirect wherever
    netguard allowed, and a 307 or 308 re-sent the conversation to the new host - without
    the key, which requests takes off when the host changes, but with everything else.

    `follow`: for a public fetch that carries neither (Intel's and NVIDIA's evidence,
    Tinfoil's releases, RedPill's session records): it follows a redirect, but only where
    netguard would have let the request go in the first place, never from https to
    plain http, and never from a server elsewhere into this machine or its network
    (redirect_problem).

    And a proxy is held to the same rule. A server on this machine or its own network
    is reached directly, whatever the environment says: plain http to it through a
    proxy elsewhere carried the key and the conversation across the network in the
    clear, and NO_PROXY cannot name all of it - requests reads no IPv6 network there,
    nor a name written with a trailing dot. Anything else is not sent at all while a
    proxy set in Settings cannot be used (ProxyRefused), rather than sent around it."""
    import requests

    from . import proxy

    class GuardedSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.headers["Accept-Encoding"] = ACCEPT_ENCODING

        def get_redirect_target(self, response):
            target = super().get_redirect_target(response)
            if target:
                absolute = urljoin(response.url, target)
                found = (redirect_problem(response.url, absolute) if follow
                         else redirected(response.url, absolute, response.status_code))
                if found:
                    try:
                        response.close()        # its connection, back to the pool
                    except Exception:
                        pass
                    raise RedirectRefused(found, response=response)
            return target

        def prepare_request(self, request):
            # Before requests checks the headers itself, which refuses such a key in
            # words that quote it whole.
            bearer = str((request.headers or {}).get("Authorization") or "")
            if bearer.startswith("Bearer ") and key_problem(bearer[7:]):
                raise requests.exceptions.InvalidHeader(
                    key_problem(bearer[7:], urlparse(request.url).hostname))
            return super().prepare_request(request)

        def send(self, request, **kwargs):
            # Every request comes through here, redirects included, with the proxies
            # requests chose for it.
            if is_local(urlparse(request.url).hostname):
                kwargs["proxies"] = {}
                request.headers.pop("Proxy-Authorization", None)
            elif proxy.refusal():
                raise ProxyRefused(f"Nothing was sent: the proxy set in Settings cannot "
                                   f"be used. {proxy.refusal()}", request=request)
            # Read here, never by requests, which reads a body whole whatever its size:
            # a reply not streamed to MAX_BODY_BYTES, and an error's or a redirect's
            # words - streamed or not, since what reads them reads them whole - to
            # MAX_ERROR_BYTES (_read_whole).
            streamed = kwargs.get("stream", False)
            kwargs["stream"] = True
            response = super().send(request, **kwargs)
            coding = _unasked_coding(response.headers)
            if coding:
                response.close()
                raise Undecodable(f"{shown(request.url)} answered compressed as {coding}, "
                                  f"which was not asked for; it was not read",
                                  request=request, response=response)
            if not streamed or response.status_code >= 300:
                _read_whole(response)
            response.json = lambda **_kwargs: _json_of(response)
            return response

    return GuardedSession()


def _unasked_coding(headers):
    """The content coding an answer was sent in that was not asked for (ACCEPT_ENCODING),
    or None."""
    for coding in str(headers.get("Content-Encoding") or "").lower().split(","):
        if coding.strip() and coding.strip() not in _ASKED_CODINGS:
            return coding.strip()
    return None


def _read_whole(response):
    """`response`'s body, read once and kept (requests' `_content`): at most
    MAX_BODY_BYTES unpacked for one that answered, TooLarge past it with nothing more
    read; at most MAX_ERROR_BYTES of an error's or a redirect's, the rest left unread."""
    if response._content_consumed or response._content is not False:
        return                          # read already, or given whole
    cut = response.status_code >= 300
    limit = MAX_ERROR_BYTES if cut else MAX_BODY_BYTES
    most = limit if cut else limit + 1  # one byte too many says it is too large
    body = bytearray()
    while len(body) < most:
        piece = _read_piece(response, min(_READ_PIECE, most - len(body)))
        if not piece:
            break
        body += piece
    if len(body) > limit:
        if not cut:
            response.close()
            raise TooLarge(f"{shown(response.url)} sent more than "
                           f"{limit // (1024 * 1024)} MB, once unpacked, which is no "
                           f"answer; it was not read further", response=response)
        del body[limit:]
    response._content = bytes(body)
    response._content_consumed = True
    if cut:
        response.close()                # the rest of it unread


def _read_piece(response, amount):
    """Up to `amount` more bytes of `response`'s body, unpacked, with urllib3's errors
    as requests would raise them (Response.iter_content)."""
    import requests
    import urllib3

    raw = response.raw
    try:
        if hasattr(raw, "stream"):
            return raw.read(amount, decode_content=True)
        return raw.read(amount)
    except urllib3.exceptions.ProtocolError as e:
        raise requests.exceptions.ChunkedEncodingError(e) from e
    except urllib3.exceptions.DecodeError as e:
        raise requests.exceptions.ContentDecodingError(e) from e
    except urllib3.exceptions.ReadTimeoutError as e:
        raise requests.exceptions.ConnectionError(e) from e
    except urllib3.exceptions.SSLError as e:
        raise requests.exceptions.SSLError(e) from e


def _json_of(response):
    """`response.json()`, read to its cap (_read_whole) and through safejson: JSON nested
    past 200 levels is refused as no JSON (a ValueError), never a crash."""
    from . import safejson
    _read_whole(response)
    return safejson.loads(response.content)
