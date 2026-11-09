"""Tinfoil's SDK fetches through netguard: a redirect off https is not followed.

The SDK makes bare requests.get and requests.post calls - GitHub's release and Sigstore
bundle, AMD's and Intel's certificates - and requests follows a redirect anywhere, https
to plain http included. sealed.guard_sdk_requests hands each of its modules a stand-in
for requests that sends through netguard.session(), without changing the SDK on disk.
Here the SDK is a fake one, written to a folder of the test's own, in the shapes the
real one takes requests in; nothing reaches the network.
"""

import importlib
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

import requests  # noqa: E402

from assistant import netguard, proxy, sealed  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
