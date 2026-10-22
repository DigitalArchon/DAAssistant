"""The sealed client (assistant/sealed.py) against the contract of nano-gpt's own Private
Mode client, @nanogpt/private-mode on npm, as last read: tests/data/nanogpt_private_mode_proxy.json
records that release (SealedLoreDocs/tools/private_mode_check compares it with npm's latest and
refreshes the record). Every field we may seal is one the proxy lets through, every x-nanogpt
header we name is one it sends or reads, the preflight's fields and the relay's paths are its.

Run:  python3 -m unittest tests.test_private_mode_proxy -v
"""

import inspect
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import sealed

HERE = os.path.dirname(os.path.abspath(__file__))


class SealedClientWithinTheProxysContract(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "data", "nanogpt_private_mode_proxy.json")) as f:
            self.proxy = json.load(f)
        self.source = inspect.getsource(sealed)

    def test_the_record_names_a_release(self):
        self.assertRegex(self.proxy["reviewed_version"], r"^\d+\.\d+\.\d+$")

    def test_every_field_we_may_seal_is_one_the_proxy_lets_through(self):
        self.assertLessEqual(sealed.ALLOWED_FIELDS, set(self.proxy["body_fields"]))
        # the router-only cache field goes inside the sealed body, never through the filter
        self.assertNotIn(sealed.CACHE_SECRET_FIELD, self.proxy["body_fields"])

    def test_every_header_we_name_is_one_the_proxy_sends_or_reads(self):
        headers = set(re.findall(r'"(x-nanogpt-[a-z-]+)"', self.source))
        self.assertTrue(headers)
        self.assertLessEqual(headers, set(self.proxy["request_headers"]) | set(self.proxy["reply_headers"]))
        self.assertIn(f'"{self.proxy["reply_header_private_mode_value"]}"', self.source)

    def test_the_preflight_and_the_relays_paths_are_the_proxys(self):
        fields = set(re.findall(r'"(cacheScope|upstreamModel|requestBodyBytes|operation)"', self.source))
        self.assertLessEqual(fields, set(self.proxy["preflight_request_fields"]) | set(self.proxy["preflight_reply_fields"]))
        self.assertEqual(self.proxy["relay_paths"]["preflight"], sealed.RELAY_PATH + "/preflight")
        self.assertEqual(self.proxy["relay_paths"]["chat"], sealed.RELAY_PATH + "/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
