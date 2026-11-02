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
