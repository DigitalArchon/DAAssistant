"""A proxy for every request the app makes (proxy.py), local servers left direct."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

import requests  # noqa: E402

from assistant import hostenv, providers, proxy  # noqa: E402
from assistant.config import Config  # noqa: E402

NAMES = proxy.VARIABLES + ("NO_PROXY", "no_proxy")


class Environment(unittest.TestCase):
    def setUp(self):
        saved = {name: os.environ.get(name) for name in NAMES}
        self.addCleanup(self.restore, saved)
        for name in NAMES:
            os.environ.pop(name, None)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = Config(os.path.join(self.dir, "c.json"))
        patch = mock.patch.object(proxy, "_missing_for_socks", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def restore(saved):
        for name in NAMES:
            hostenv.restore_own(name)
            if saved[name] is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved[name]


class WhatMayBeAProxy(Environment):
    def test_the_kinds_there_are(self):
        for url in ("", "socks5h://127.0.0.1:9050", "socks5://tor:9050",
                    "http://proxy.example:3128", "https://proxy.example"):
            self.assertIsNone(proxy.problem(url), url)

    def test_what_is_not_one(self):
        for url in ("127.0.0.1:9050", "ftp://proxy:21", "socks5h://", "http://a b:1",
                    "http://proxy:port"):
            self.assertIsNotNone(proxy.problem(url), url)

    def test_socks_without_its_packages_says_which(self):
        with mock.patch.object(proxy, "_missing_for_socks",
                               return_value=["PySocks (python3-socks)"]):
            self.assertIn("PySocks", proxy.problem("socks5h://127.0.0.1:9050"))
            self.assertIsNone(proxy.problem("http://proxy:3128"))


class EveryRequestGoesThroughIt(Environment):
    def test_requests_goes_through_it_and_local_servers_do_not(self):
        os.environ["NO_PROXY"] = "intranet.example"
        self.config.proxy = "socks5h://127.0.0.1:9050"
        self.assertTrue(proxy.apply(self.config))
        through = requests.utils.get_environ_proxies("https://nano-gpt.com/api/v1/models")
        self.assertEqual(through.get("https"), "socks5h://127.0.0.1:9050")
        for direct in ("http://192.168.1.20:11434/api/tags", "http://localhost:11434/",
                       "http://ollama.local:11434/", "https://intranet.example/v1"):
            self.assertEqual(requests.utils.get_environ_proxies(direct), {}, direct)

    def test_the_programs_it_starts_get_their_own_environment(self):
        os.environ["HTTPS_PROXY"] = "http://theirs:8080"
        self.config.proxy = "http://ours:3128"
        proxy.apply(self.config)
        self.assertEqual(os.environ["HTTPS_PROXY"], "http://ours:3128")
        env = hostenv.host_env()
        self.assertEqual(env["HTTPS_PROXY"], "http://theirs:8080")
        self.assertNotIn("ALL_PROXY", env)

    def test_emptied_it_is_the_environments_again(self):
        os.environ["HTTPS_PROXY"] = "http://theirs:8080"
        self.config.proxy = "http://ours:3128"
        proxy.apply(self.config)
        self.config.proxy = ""
        self.assertFalse(proxy.apply(self.config))
        self.assertEqual(os.environ["HTTPS_PROXY"], "http://theirs:8080")
        self.assertNotIn("ALL_PROXY", os.environ)


class AnOnionNeedsTor(Environment):
    def onion(self):
        self.config.custom_url = "http://" + "a" * 56 + ".onion/v1"
        return providers.get(self.config, providers.CUSTOM)

    def test_without_a_socks_proxy_it_says_how_to_reach_one(self):
        self.assertIn("socks5h://", self.onion().url_problem())

    def test_with_tor_as_the_proxy_it_may_be_used(self):
        self.config.proxy = "socks5h://127.0.0.1:9050"
        proxy.apply(self.config)
        self.assertIsNone(self.onion().url_problem())


if __name__ == "__main__":
    unittest.main()


class TufThroughSocks(unittest.TestCase):
    """Sigstore's trust root, fetched by TUF for Private Mode's attestation, through a
    SOCKS proxy: TUF's own handling knows only an http(s) proxy (live, Nov 2026)."""

    def setUp(self):
        import types

        class Manager:                      # urllib3's ProxyManager, as TUF uses it
            def __init__(self, url, **kwargs):
                self.url, self.kwargs = url, kwargs

        class SocksManager(Manager):        # urllib3.contrib.socks' SOCKSProxyManager
            pass

        self.Manager, self.SocksManager = Manager, SocksManager
        tuf_proxy = types.ModuleType("tuf.ngclient._internal.proxy")
        tuf_proxy.ProxyManager = Manager
        socks = types.ModuleType("urllib3.contrib.socks")
        socks.SOCKSProxyManager = SocksManager
        self.tuf_proxy = tuf_proxy
        modules = {"tuf": types.ModuleType("tuf"),
                   "tuf.ngclient": types.ModuleType("tuf.ngclient"),
                   "tuf.ngclient._internal": types.ModuleType("tuf.ngclient._internal"),
                   "tuf.ngclient._internal.proxy": tuf_proxy,
                   "urllib3.contrib.socks": socks}
        modules["tuf.ngclient._internal"].proxy = tuf_proxy
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_socks_proxy_gets_urllib3s_socks_manager(self):
        self.assertTrue(proxy.let_tuf_use_socks())
        made = self.tuf_proxy.ProxyManager("socks5h://127.0.0.1:9050", headers={"a": "b"})
        self.assertIsInstance(made, self.SocksManager)
        self.assertEqual(made.url, "socks5h://127.0.0.1:9050")
        self.assertEqual(made.kwargs, {"headers": {"a": "b"}})

    def test_any_other_proxy_is_left_to_tuf(self):
        proxy.let_tuf_use_socks()
        made = self.tuf_proxy.ProxyManager("http://127.0.0.1:3128")
        self.assertIs(type(made), self.Manager)

    def test_twice_is_once(self):
        proxy.let_tuf_use_socks()
        first = self.tuf_proxy.ProxyManager
        self.assertTrue(proxy.let_tuf_use_socks())
        self.assertIs(self.tuf_proxy.ProxyManager, first)

    def test_without_tuf_nothing_is_done(self):
        absent = dict.fromkeys(["tuf", "tuf.ngclient", "tuf.ngclient._internal",
                                "tuf.ngclient._internal.proxy"])
        with mock.patch.dict(sys.modules, absent):
            self.assertFalse(proxy.let_tuf_use_socks())


class AttestationThroughAProxy(Environment):
    """A private/ model's attestation reads Sigstore's trust root, whose servers refuse
    most Tor exits: a failure there says so, through a proxy alone."""

    def attest(self, message):
        from assistant import sealed
        from assistant.privacy import PrivacyError

        def verify(relay, secret):
            raise RuntimeError(message)

        enclave = sealed.Enclave("https://nano-gpt.com/api/v1/private", verifier=verify)
        with self.assertRaises(PrivacyError) as caught:
            enclave.current()
        return str(caught.exception)

    def test_a_trust_root_not_fetched_through_the_proxy_says_why(self):
        self.config.proxy = "socks5h://127.0.0.1:9050"
        proxy.apply(self.config)
        self.assertIn("Sigstore's servers refuse from most Tor exits",
                      self.attest("Failed to refresh TUF metadata"))

    def test_without_a_proxy_the_error_is_as_it_came(self):
        message = self.attest("Failed to refresh TUF metadata")
        self.assertTrue(message.endswith("Failed to refresh TUF metadata"), message)

    def test_any_other_failure_through_the_proxy_is_as_it_came(self):
        self.config.proxy = "http://127.0.0.1:3128"
        proxy.apply(self.config)
        self.assertNotIn("Tor", self.attest("measurement mismatch"))
