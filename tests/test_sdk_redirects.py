"""Tinfoil's SDK fetches through netguard: a redirect off https is not followed.

The SDK makes bare requests.get and requests.post calls - GitHub's release and Sigstore
bundle, AMD's and Intel's certificates - and requests follows a redirect anywhere, https
to plain http included. sealed.guard_sdk_requests hands each of its modules a stand-in
for requests that sends through netguard.session(), without changing the SDK on disk.
Here the SDK is a fake one, written to a folder of the test's own, in the shapes the
real one takes requests in; nothing reaches the network.

The SDK checks the router's release with sigstore-python, whose trust root TUF's own
client fetches through urllib3, which follows a redirect anywhere too
(sealed.guard_sigstore): those tests use sigstore and TUF as the Private Mode client
installs them, and answer urllib3's connection pools with a fake.
"""

import importlib
import io
import os
import pkgutil
import shutil
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

import requests  # noqa: E402
import urllib3  # noqa: E402

from assistant import netguard, proxy, sealed  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.privacy import PrivacyError  # noqa: E402


def _sigstore():
    """sigstore-python, with TUF's client, as the Private Mode client installs them - or
    None where it is not installed."""
    try:
        import sigstore
        import sigstore._internal.tuf  # noqa: F401
        import tuf.ngclient  # noqa: F401
    except Exception:
        return None
    return sigstore


SIGSTORE = _sigstore()
NOT_INSTALLED = "Tinfoil's client is not installed (tools/install-private-mode.sh)"
# sigstore's modules that take requests: Rekor's, Fulcio's, a timestamp authority's and
# OIDC's clients (sigstore 4.5).
SIGSTORE_REQUESTS = ("sigstore.oidc", "sigstore._internal.rekor",
                     "sigstore._internal.rekor.client",
                     "sigstore._internal.rekor.client_v2",
                     "sigstore._internal.fulcio.client", "sigstore._internal.timestamp")


def sigstore_as_installed(test):
    """sigstore-python as it is before the guard, and put back as it was after `test`:
    the guard changes it for the rest of the process, as the app wants it to. Every
    module of it is loaded first, so that the guard loads none whose requests is not put
    back; its TUF metadata and cache go in a folder of the test's own."""
    if SIGSTORE is None:
        return
    from tuf.ngclient import Updater

    for found in pkgutil.walk_packages(SIGSTORE.__path__, "sigstore.",
                                       onerror=lambda _name: None):
        if found.name not in sys.modules and found.name not in sealed._SIGSTORE_SKIPPED:
            importlib.import_module(found.name)
    patches = [mock.patch.object(module, "requests", requests)
               for name, module in list(sys.modules.items())
               if module is not None
               and (name == "sigstore" or name.startswith("sigstore."))
               and isinstance(getattr(module, "requests", None), types.ModuleType)]
    patches.append(mock.patch.object(sys.modules["sigstore._internal.tuf"], "Updater",
                                     Updater))
    folder = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, folder, True)
    patches.append(mock.patch.dict(os.environ, {
        "XDG_DATA_HOME": os.path.join(folder, "data"),
        "XDG_CACHE_HOME": os.path.join(folder, "cache")}))
    for patch in patches:
        patch.start()
        test.addCleanup(patch.stop)

FILES = {
    "__init__.py": "from .client import SecureClient\n",
    "client.py": """
        from .github import fetch_latest_release

        class SecureClient:
            def __init__(self, **kwargs):
                self.enclave = "enclave.example"

            def verify(self):
                return fetch_latest_release("owner/repo")
        """,
    # As tinfoil.github and tinfoil.attestation.* take it.
    "github.py": """
        import requests

        def fetch_latest_release(repo):
            response = requests.get(f"https://github-proxy.example/repos/{repo}",
                                    timeout=5)
            response.raise_for_status()
            return response.text
        """,
    # A function taken by name, and Session in an annotation made at import.
    "byname.py": """
        from typing import Optional

        import requests
        from requests import post

        HttpSession = Optional[requests.Session]

        def send(url, session: HttpSession = None):
            if session is not None:
                return session.get(url, timeout=5).text
            return post(url, json={}, timeout=5).text
        """,
    # A module the package does not import until it is wanted.
    "later/__init__.py": "",
    "later/collateral.py": """
        import requests

        def fetch(url):
            return requests.get(url, timeout=5).text
        """,
}


class FakeSdk(unittest.TestCase):
    def setUp(self):
        sigstore_as_installed(self)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, text in FILES.items():
            path = os.path.join(self.dir, "tinfoil", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(textwrap.dedent(text))
        # The fake in place of any real one, and the real one back afterwards.
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        for name in [name for name in sys.modules
                     if name == "tinfoil" or name.startswith("tinfoil.")]:
            del sys.modules[name]
        sys.path.insert(0, self.dir)
        self.addCleanup(sys.path.remove, self.dir)
        importlib.invalidate_caches()
        self.sdk = importlib.import_module("tinfoil")
        self.sent = []
        self.answers = {}
        patch = mock.patch("requests.adapters.HTTPAdapter.send",
                           lambda adapter, request, **kwargs: self.adapter_send(request))
        patch.start()
        self.addCleanup(patch.stop)

    def adapter_send(self, request):
        """The network: each URL answered as self.answers says - a redirect to
        another, or 200 - and every request that reached it recorded."""
        self.sent.append(request.url)
        response = requests.Response()
        response.url, response.request = request.url, request
        target = self.answers.get(request.url)
        if target:
            response.status_code = 302
            response.headers["Location"] = target
            response._content = b""
        else:
            response.status_code = 200
            response._content = b"fine"
        return response

    def module(self, name):
        return importlib.import_module(f"tinfoil.{name}")


class RedirectsOffHttpsAreRefused(FakeSdk):
    URL = "https://github-proxy.example/repos/owner/repo"

    def test_unguarded_the_sdk_follows_one_into_the_clear(self):
        # What the SDK does by itself, and why: the plain http fetch went out.
        self.answers[self.URL] = "http://evil.example/release"
        self.assertEqual(self.module("github").fetch_latest_release("owner/repo"), "fine")
        self.assertEqual(self.sent, [self.URL, "http://evil.example/release"])

    def test_guarded_it_is_refused_and_nothing_is_sent_there(self):
        sealed.guard_sdk_requests()
        self.answers[self.URL] = "http://evil.example/release"
        with self.assertRaises(netguard.RedirectRefused) as refused:
            self.module("github").fetch_latest_release("owner/repo")
        self.assertIsInstance(refused.exception, requests.RequestException)
        self.assertEqual(self.sent, [self.URL])

    def test_nor_is_one_into_this_machine_or_its_network(self):
        """As the other apps' guard of the SDK refuses it (Nov 2026): over https, to
        the router's address."""
        sealed.guard_sdk_requests()
        self.answers[self.URL] = "https://192.168.1.1/release"
        with self.assertRaises(netguard.RedirectRefused):
            self.module("github").fetch_latest_release("owner/repo")
        self.assertEqual(self.sent, [self.URL])

    def test_a_redirect_that_stays_on_https_is_followed(self):
        sealed.guard_sdk_requests()
        self.answers[self.URL] = "https://objects.example/release"
        self.assertEqual(self.module("github").fetch_latest_release("owner/repo"), "fine")
        self.assertEqual(self.sent, [self.URL, "https://objects.example/release"])

    def test_a_function_taken_by_name_and_a_module_loaded_later_are_guarded(self):
        given = sealed.guard_sdk_requests()
        self.assertIn("tinfoil.byname", given)
        self.assertIn("tinfoil.later.collateral", given)
        for send in (self.module("byname").send, self.module("later.collateral").fetch):
            self.sent.clear()
            self.answers["https://kds.example/vcek"] = "http://kds.example/vcek"
            with self.assertRaises(netguard.RedirectRefused):
                send("https://kds.example/vcek")
            self.assertEqual(self.sent, ["https://kds.example/vcek"])

    def test_a_session_of_its_own_is_guarded_too(self):
        sealed.guard_sdk_requests()
        session = self.module("byname").requests.Session()
        self.assertIsInstance(session, requests.Session)
        self.answers["https://pcs.example/tcb"] = "http://pcs.example/tcb"
        with self.assertRaises(netguard.RedirectRefused):
            self.module("byname").send("https://pcs.example/tcb", session)

    def test_the_rest_of_requests_is_requests_own_and_twice_is_once(self):
        sealed.guard_sdk_requests()
        stand_in = self.module("github").requests
        self.assertIs(stand_in.RequestException, requests.RequestException)
        self.assertIs(stand_in.Response, requests.Response)
        sealed.guard_sdk_requests()
        self.assertIs(self.module("github").requests, stand_in)
        self.assertIs(sys.modules["requests"], requests)       # the app's own untouched

    def test_attesting_guards_the_sdk_first(self):
        self.answers[self.URL] = "http://evil.example/release"
        with mock.patch.object(proxy, "let_tuf_use_socks"), \
                self.assertRaises(netguard.RedirectRefused):
            sealed.tinfoil_verify("https://nano-gpt.com/api/v1/private/tinfoil", "s")
        self.assertEqual(self.sent, [self.URL])

    def test_without_the_sdk_there_is_nothing_to_guard(self):
        del sys.modules["tinfoil"]
        self.assertEqual(sealed.guard_sdk_requests(), [])


@unittest.skipUnless(SIGSTORE, NOT_INSTALLED)
class SigstoreWithTheSdk(FakeSdk):
    """What the SDK verifies with is guarded with it, and where it cannot be, nothing is
    attested."""

    def test_sigstores_own_requests_users_are_guarded_as_the_sdks_are(self):
        """Rekor's, Fulcio's, a timestamp authority's and OIDC's clients: none fetches
        while an attestation is verified, all of them while signing."""
        given = sealed.guard_sdk_requests()
        self.assertLessEqual(set(SIGSTORE_REQUESTS), set(given))
        self.assertIn("sigstore._internal.tuf", given)
        for name in SIGSTORE_REQUESTS:
            self.assertIsInstance(sys.modules[name].requests, sealed._GuardedRequests,
                                  name)
        self.assertNotIn("sigstore._cli", given)      # its command line: never loaded
        self.answers["https://rekor.example/api/v1/log"] = "http://rekor.example/api/v1/log"
        session = sys.modules["sigstore._internal.rekor.client"].requests.Session()
        with self.assertRaises(netguard.RedirectRefused):
            session.get("https://rekor.example/api/v1/log")
        self.assertEqual(self.sent, ["https://rekor.example/api/v1/log"])

    def test_with_sigstores_internals_elsewhere_nothing_is_attested(self):
        """Another sigstore or TUF, its TUF client not where this expects: the
        attestation is refused, saying why, rather than its trust root fetched
        unguarded; and nothing is fetched."""
        from sigstore._internal import tuf as sigstore_tuf

        patch = mock.patch.object(sigstore_tuf, "Updater", object)
        patch.start()
        self.addCleanup(patch.stop)
        with self.assertRaisesRegex(sealed.Unguarded, "is not tuf.ngclient.Updater"):
            sealed.guard_sdk_requests()
        self.answers[RedirectsOffHttpsAreRefused.URL] = "http://evil.example/release"
        relay = "https://nano-gpt.com/api/v1/private/tinfoil"
        for attest in (lambda: sealed.tinfoil_verify(relay, "s"),
                       sealed.Enclave(relay).current):
            with mock.patch.object(proxy, "let_tuf_use_socks"), \
                    self.assertRaises(PrivacyError) as refused:
                attest()
            self.assertEqual(str(refused.exception),
                             "the private model's enclave could not be attested: "
                             "Sigstore's trust root cannot be fetched with its redirects "
                             "held to https, so nothing is attested: "
                             "sigstore._internal.tuf.Updater is not tuf.ngclient.Updater")
        self.assertEqual(self.sent, [])


TUF_URL = "https://tuf.example/14.root.json"
SIGSTORE_TUF = "https://tuf-repo-cdn.sigstore.dev"


class Wire:
    """urllib3's connection pools: each URL answered as `answers` says - a redirect to
    another, or 200 - a host in `everywhere` redirected wherever it is asked, and every
    request that reached a pool recorded, with its User-Agent. urllib3's own redirect
    handling runs above it, as it would for real."""

    def __init__(self):
        self.sent, self.answers, self.everywhere, self.agents = [], {}, {}, []

    def urlopen(self, pool, method, url, **kwargs):
        whole = url if "://" in url else f"{pool.scheme}://{pool.host}{url}"
        self.sent.append(whole)
        self.agents.append((kwargs.get("headers") or {}).get("User-Agent", ""))
        target = self.answers.get(whole) or self.everywhere.get(pool.host)
        if target:
            return urllib3.HTTPResponse(body=io.BytesIO(b""), status=302,
                                        headers={"Location": target},
                                        preload_content=False, request_url=whole)
        return urllib3.HTTPResponse(body=io.BytesIO(b"fine"), status=200,
                                    preload_content=False, request_url=whole)


@unittest.skipUnless(SIGSTORE, NOT_INSTALLED)
class SigstoresTrustRoot(unittest.TestCase):
    """Fetched through TUF's own client, whose fetcher asks urllib3, which follows a
    redirect anywhere, https to plain http included."""

    def setUp(self):
        sigstore_as_installed(self)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in proxy.VARIABLES + ("NO_PROXY", "no_proxy"):
            os.environ.pop(name, None)
        self.wire = Wire()
        patch = mock.patch.object(
            urllib3.connectionpool.HTTPConnectionPool, "urlopen",
            lambda pool, method, url, **kwargs: self.wire.urlopen(pool, method, url,
                                                                  **kwargs))
        patch.start()
        self.addCleanup(patch.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    @staticmethod
    def fetched(fetcher, url=TUF_URL):
        return b"".join(fetcher.fetch(url))

    @staticmethod
    def guarded_fetcher():
        return sealed._tuf_fetcher_class()(app_user_agent="sigstore-python/4.5.0")

    def updater(self, **kwargs):
        """An Updater as sigstore's TrustUpdater builds one, by the name it imported."""
        from sigstore._internal import tuf as sigstore_tuf
        from sigstore._utils import read_embedded

        return sigstore_tuf.Updater(
            metadata_dir=os.path.join(self.dir, "metadata"),
            metadata_base_url=SIGSTORE_TUF,
            bootstrap=read_embedded("root.json", SIGSTORE_TUF), **kwargs)

    def test_unguarded_tufs_own_fetcher_follows_one_into_the_clear(self):
        """What TUF's client does by itself, and why: urllib3 follows it to plain http."""
        from tuf.ngclient import Urllib3Fetcher

        self.wire.answers[TUF_URL] = "http://evil.example/14.root.json"
        self.assertEqual(self.fetched(Urllib3Fetcher()), b"fine")
        self.assertEqual(self.wire.sent, [TUF_URL, "http://evil.example/14.root.json"])

    def test_one_off_https_or_into_this_network_is_refused_and_nothing_sent_there(self):
        from tuf.api import exceptions

        for target, said in (("http://evil.example/14.root.json", "is not https"),
                             ("https://192.168.1.1/admin",
                              "on this machine or its network")):
            self.wire.sent.clear()
            self.wire.answers[TUF_URL] = target
            with self.assertRaisesRegex(exceptions.DownloadError, said):  # a failed fetch
                self.fetched(self.guarded_fetcher())
            self.assertEqual(self.wire.sent, [TUF_URL])

    def test_one_that_stays_on_https_is_followed_and_a_relative_one_too(self):
        import tuf

        self.wire.answers[TUF_URL] = "https://cdn.example/14.root.json"
        self.assertEqual(self.fetched(self.guarded_fetcher()), b"fine")
        self.assertEqual(self.wire.sent, [TUF_URL, "https://cdn.example/14.root.json"])
        self.wire.sent.clear()
        self.wire.answers[TUF_URL] = "/elsewhere/14.root.json"
        self.assertEqual(self.fetched(self.guarded_fetcher()), b"fine")
        self.assertEqual(self.wire.sent,
                         [TUF_URL, "https://tuf.example/elsewhere/14.root.json"])
        # TUF's own User-Agent, sigstore's before it, as its fetcher sends them
        self.assertEqual(set(self.wire.agents),
                         {f"sigstore-python/4.5.0 python-tuf/{tuf.__version__}"})

    def test_only_a_few_are_followed(self):
        from tuf.api import exceptions

        hops = [TUF_URL] + [f"https://hop{n}.example/14.root.json" for n in range(10)]
        self.wire.answers.update(zip(hops, hops[1:]))
        with self.assertRaisesRegex(exceptions.DownloadError,
                                    f"more than {sealed.TUF_MAX_REDIRECTS} times"):
            self.fetched(self.guarded_fetcher())
        self.assertEqual(self.wire.sent, hops[:sealed.TUF_MAX_REDIRECTS + 1])

    def test_sigstores_trust_root_is_refreshed_through_the_guarded_fetcher(self):
        """sigstore's TrustUpdater builds TUF's Updater with no fetcher: unguarded, a
        refresh sent on into the clear went there; guarded, the refresh fails, and the
        attestation with it, saying why, and nothing is fetched there."""
        from sigstore._internal import tuf as sigstore_tuf
        from sigstore.errors import TUFError

        self.wire.everywhere["tuf-repo-cdn.sigstore.dev"] = "http://evil.example/root.json"
        with self.assertRaises(TUFError):
            sigstore_tuf.TrustUpdater(SIGSTORE_TUF)
        self.assertIn("http://evil.example/root.json", self.wire.sent)   # by itself
        self.wire.sent.clear()
        self.assertIn("sigstore._internal.tuf", sealed.guard_sigstore())

        def verifier(_relay, _secret):
            sigstore_tuf.TrustUpdater(SIGSTORE_TUF)

        with self.assertRaises(PrivacyError) as refused:
            sealed.Enclave("https://nano-gpt.com/api/v1/private/tinfoil",
                           verifier=verifier).current()
        self.assertIn("could not be attested: Failed to refresh TUF metadata (The server "
                      "sent the request on to http://evil.example/root.json, which was "
                      "not followed: ", str(refused.exception))
        self.assertTrue(self.wire.sent)
        self.assertTrue(all(url.startswith(SIGSTORE_TUF + "/") for url in self.wire.sent),
                        self.wire.sent)

    def test_the_updater_it_builds_has_the_guarded_fetcher_and_keeps_one_given(self):
        from tuf.ngclient import Updater, UpdaterConfig

        sealed.guard_sigstore()
        made = self.updater(config=UpdaterConfig(app_user_agent="sigstore-python/4.5.0"))
        self.assertIsInstance(made, Updater)
        self.assertEqual(type(made._fetcher).__name__, "GuardedFetcher")
        mine = self.guarded_fetcher()
        self.assertIs(self.updater(fetcher=mine)._fetcher, mine)
        from sigstore._internal import tuf as sigstore_tuf
        from sigstore._utils import read_embedded
        placed = sigstore_tuf.Updater(os.path.join(self.dir, "m"), SIGSTORE_TUF, None, None,
                                      None, bootstrap=read_embedded("root.json",
                                                                    SIGSTORE_TUF))
        self.assertEqual(type(placed._fetcher).__name__, "GuardedFetcher")  # None in place
        guarded = sigstore_tuf.Updater
        self.assertNotIn("sigstore._internal.tuf", sealed.guard_sigstore())  # twice is
        self.assertIs(sigstore_tuf.Updater, guarded)                          # once

    def test_a_socks_proxy_set_in_settings_still_carries_it(self):
        """proxy.let_tuf_use_socks gives TUF's proxy handling urllib3's SOCKS manager:
        the guarded fetcher goes through the same, and holds its redirects there too."""
        from tuf.api import exceptions
        from tuf.ngclient._internal import proxy as tuf_proxy

        made = []

        class SocksManager(urllib3.PoolManager):   # urllib3.contrib.socks', with PySocks
            def __init__(self, proxy_url, **kwargs):
                super().__init__(**kwargs)
                self.proxy_url, self.through = proxy_url, []
                made.append(self)

            def urlopen(self, method, url, redirect=True, **kwargs):
                self.through.append(url)
                return super().urlopen(method, url, redirect=redirect, **kwargs)

        socks = types.ModuleType("urllib3.contrib.socks")
        socks.SOCKSProxyManager = SocksManager
        for patch in (mock.patch.dict(sys.modules, {"urllib3.contrib.socks": socks}),
                      mock.patch.object(tuf_proxy, "ProxyManager", tuf_proxy.ProxyManager),
                      mock.patch.object(proxy, "_missing_for_socks", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        config = Config(os.path.join(self.dir, "c.json"))
        config.proxy = "socks5h://127.0.0.1:9050"
        self.assertTrue(proxy.apply(config))
        self.addCleanup(proxy.apply, Config(os.path.join(self.dir, "none.json")))
        self.assertTrue(proxy.let_tuf_use_socks())
        sealed.guard_sigstore()
        fetcher = self.updater()._fetcher
        self.wire.answers[TUF_URL] = "https://cdn.example/14.root.json"
        self.assertEqual(self.fetched(fetcher), b"fine")
        self.assertEqual([manager.proxy_url for manager in made],
                         ["socks5h://127.0.0.1:9050"])
        self.assertEqual(made[0].through, [TUF_URL, "https://cdn.example/14.root.json"])
        self.wire.sent.clear()
        self.wire.answers[TUF_URL] = "http://evil.example/14.root.json"
        with self.assertRaisesRegex(exceptions.DownloadError, "is not https"):
            self.fetched(fetcher)
        self.assertEqual(made[0].through[-1], TUF_URL)
        self.assertEqual(self.wire.sent, [TUF_URL])

    def test_what_the_guard_rests_on_is_where_it_expects(self):
        """sigstore 4.5 and tuf 7.0, as packaging/private-mode.lock pins them: a version
        that moves these fails here first (and in the app, refuses to attest:
        Unguarded); a new user of requests in sigstore is named here, to be looked at."""
        import inspect

        from sigstore._internal import tuf as sigstore_tuf
        from tuf.api import exceptions
        from tuf.ngclient import Updater, Urllib3Fetcher
        from tuf.ngclient._internal.proxy import ProxyEnvironment

        self.assertIs(sigstore_tuf.Updater, Updater)
        self.assertTrue(callable(sigstore_tuf.TrustUpdater))
        built = inspect.getsource(sigstore_tuf.TrustUpdater.__init__)
        self.assertIn("Updater(", built)
        self.assertNotIn("fetcher", built)            # it gives none: the guard's is used
        self.assertEqual(list(inspect.signature(Updater.__init__).parameters)[1:7],
                         ["metadata_dir", "metadata_base_url", "target_dir",
                          "target_base_url", "fetcher", "config"])
        self.assertIsInstance(Urllib3Fetcher()._proxy_env, ProxyEnvironment)
        self.assertTrue(callable(Urllib3Fetcher._chunks))
        self.assertTrue(issubclass(exceptions.DownloadHTTPError, exceptions.DownloadError))
        # proxy.let_tuf_use_socks replaces the name ProxyEnvironment makes a proxy's
        # manager by, and the guarded fetcher's requests go through ProxyEnvironment
        self.assertIn("ProxyManager(", inspect.getsource(ProxyEnvironment.get_pool_manager))
        self.assertIn("get_pool_manager(", inspect.getsource(ProxyEnvironment.request))
        users = sorted(name for name, module in sys.modules.items() if module is not None
                       and (name == "sigstore" or name.startswith("sigstore."))
                       and getattr(module, "requests", None) is requests)
        self.assertEqual(users, sorted(SIGSTORE_REQUESTS))


if __name__ == "__main__":
    unittest.main()
