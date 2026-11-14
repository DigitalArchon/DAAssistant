"""What arrives from outside is read only so far, and never into a crash (the DA apps'
security standard, Nov 2026).

- Every request asks for gzip and deflate alone, whose output for each piece read is
  bounded, and refuses an answer coded any other way unread.
- A whole answer is read to 32 MB, counted as it is unpacked; an error's or a redirect's
  words to 64 KB, the rest left unread.
- JSON from outside - answers, their streams, nano-gpt's relay, config.json and the model
  cache, a desktop program's - nested past 200 levels is refused as no JSON, and one past
  what Python's parser takes too, never a RecursionError.

Answers come from real urllib3 and httpx responses over a fake wire: nothing reaches the
network.
"""

import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

import requests  # noqa: E402
import urllib3  # noqa: E402

from assistant import gtkcompat  # noqa: E402,F401  (pins GI versions)
from assistant import netguard, safejson  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.keychain import MemoryKeychain  # noqa: E402

URL = "https://api.example/v1/models"
DEEP = "[" * 300 + "]" * 300
DEEPER = "[" * 100_000 + "]" * 100_000


class Wire:
    """requests' adapter, answering every request with `status`, `body` and `headers`
    through a real urllib3 response, and keeping each request it was given."""

    def __init__(self, test, status=200, body=b"{}", headers=None):
        self.status, self.body, self.headers, self.sent = status, body, headers or {}, []
        patch = mock.patch("requests.adapters.HTTPAdapter.send",
                           lambda adapter, request, **kwargs: self.send(adapter, request))
        patch.start()
        test.addCleanup(patch.stop)

    def send(self, adapter, request, **_kwargs):
        self.sent.append(request)
        self.raw = urllib3.HTTPResponse(body=io.BytesIO(self.body), headers=self.headers,
                                        status=self.status, preload_content=False,
                                        decode_content=True, request_url=request.url)
        return adapter.build_response(request, self.raw)


class EveryRequest(unittest.TestCase):
    def test_asks_for_gzip_and_deflate_alone(self):
        wire = Wire(self)
        netguard.session().get(URL)
        self.assertEqual(wire.sent[0].headers["Accept-Encoding"], "gzip, deflate")

    def test_an_answer_coded_otherwise_is_refused_unread(self):
        for coding in ("br", "zstd", "gzip, br"):
            with self.subTest(coding=coding):
                Wire(self, body=b"\x00" * 10, headers={"Content-Encoding": coding})
                with self.assertRaisesRegex(netguard.Undecodable, "was not asked for"):
                    netguard.session().get(URL)

    def test_gzip_and_deflate_are_read_unpacked(self):
        body = json.dumps({"data": [1, 2]}).encode()
        for coding, packed in (("gzip", gzip.compress(body)), ("deflate", zlib.compress(body))):
            with self.subTest(coding=coding):
                Wire(self, body=packed, headers={"Content-Encoding": coding})
                self.assertEqual(netguard.session().get(URL).json(), {"data": [1, 2]})


class WholeAnswers(unittest.TestCase):
    def test_one_past_32_mb_unpacked_is_refused_and_not_read_on(self):
        """33 MB of zeros, 33 KB of gzip on the wire: counted as unpacked."""
        packed = gzip.compress(b"0" * (33 * 1024 * 1024))
        self.assertLess(len(packed), 1024 * 1024)
        wire = Wire(self, body=packed, headers={"Content-Encoding": "gzip"})
        with self.assertRaisesRegex(netguard.TooLarge, "more than 32 MB, once unpacked"):
            netguard.session().get(URL)
        self.assertIsInstance(netguard.TooLarge("x"), requests.RequestException)
        self.assertTrue(wire.raw.closed)

    def test_one_under_it_is_read_whole(self):
        body = b"x" * (2 * 1024 * 1024)
        Wire(self, body=body)
        self.assertEqual(netguard.session().get(URL).content, body)

    def test_an_errors_words_are_read_to_64_kb_streamed_or_not(self):
        body = b'{"error": "' + b"e" * (200 * 1024) + b'"}'
        for stream in (False, True):
            with self.subTest(stream=stream):
                wire = Wire(self, status=500, body=body)
                response = netguard.session().get(URL, stream=stream)
                self.assertEqual(len(response.content), netguard.MAX_ERROR_BYTES)
                self.assertEqual(wire.raw.tell(), netguard.MAX_ERROR_BYTES)
                with self.assertRaises(ValueError):       # cut short: no JSON
                    response.json()

    def test_a_stream_that_answered_is_left_to_its_reader(self):
        Wire(self, body=b"data: 1\n\n")
        response = netguard.session().post(URL, data=b"x", stream=True)
        self.assertFalse(response._content_consumed)
        self.assertEqual(b"".join(response.iter_content(4)), b"data: 1\n\n")


class OutsideJson(unittest.TestCase):
    def test_nested_past_200_levels_is_no_json_and_never_a_recursion_error(self):
        self.assertIsInstance(safejson.loads("[" * 200 + "]" * 200), list)
        for text in (DEEP, DEEPER, DEEP.encode()):
            with self.subTest(length=len(text)), \
                    self.assertRaisesRegex(json.JSONDecodeError, "nested more than 200"):
                safejson.loads(text)
        with self.assertRaises(ValueError):
            safejson.loads('{"a": ' * 300 + "1" + "}" * 300)

    def test_an_answers_json_is_read_through_it(self):
        for text in (DEEP, DEEPER):
            Wire(self, body=text.encode())
            with self.assertRaisesRegex(ValueError, "nested more than 200"):
                netguard.session().get(URL).json()

    def test_a_stream_chunk_nested_too_deep_is_passed_over(self):
        from assistant.llm import LLMClient
        answer = 'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}'
        lines = [f"data: {DEEPER}", answer, "data: [DONE]"]
        result = LLMClient._consume(iter(line.encode() for line in lines), None, None,
                                    None)
        self.assertEqual(result["text"], "ok")

    def test_a_reply_not_streamed_nested_too_deep_is_said_not_raised(self):
        from assistant.llm import LLMClient
        _text, _finish, _done, error = LLMClient._plain_reply(DEEPER)
        self.assertIn("not an answer", error)

    def test_a_config_edited_into_too_deep_json_starts_with_its_defaults(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "config.json")
        with open(path, "w") as f:
            f.write('{"a": ' + DEEPER + "}")
        config = Config(path, keychain=MemoryKeychain())
        self.assertIn("nested more than 200", config.load_error)

    def test_a_desktop_programs_answer_nested_too_deep_is_none(self):
        from assistant import keybindings
        self.assertIsNone(keybindings._hyprland_live(lambda _argv: (0, DEEPER)))

    def test_a_models_reply_nested_too_deep_is_no_object(self):
        from assistant.reference.encyclopedia import read_json
        self.assertEqual(read_json("plan: {" + '"a": ' * 300 + "1" + "}" * 300), {})


class NanoGptsRelay(unittest.TestCase):
    """Private Mode's preflight and its list of models (sealed.py, on httpx)."""

    def client(self, status=200, body=b"{}", headers=None):
        import httpx

        seen = []

        def answer(request):
            seen.append(request)
            return httpx.Response(status, content=body, headers=headers or {})

        client = httpx.Client(transport=httpx.MockTransport(answer),
                              follow_redirects=False)
        self.addCleanup(client.close)
        return client, seen

    def private(self, client):
        from assistant import sealed
        enclave = sealed.Enclave("https://nano-gpt.com/api/v1/private/tinfoil",
                                 verifier=mock.Mock())
        return sealed.PrivateModeClient("https://nano-gpt.com/api/v1", "k", enclave=enclave,
                                        preflight_client=client)

    def test_the_preflight_asks_for_gzip_and_deflate_and_reads_an_error_to_64_kb(self):
        from assistant.privacy import PrivacyError
        client, seen = self.client(status=500, body=b"e" * (200 * 1024))
        relay = self.private(client)
        with mock.patch.object(relay, "check_offered"), \
                self.assertRaisesRegex(PrivacyError, "refused the Private Mode request"):
            relay._preflight("private/x", 10)
        self.assertEqual(seen[0].headers["Accept-Encoding"], "gzip, deflate")

    def test_the_preflight_nested_too_deep_is_no_answer(self):
        from assistant.privacy import PrivacyError
        client, _seen = self.client(body=DEEPER.encode())
        with self.assertRaisesRegex(PrivacyError, "was not JSON"):
            self.private(client)._preflight("private/x", 10)

    def test_an_answer_coded_otherwise_is_refused_unread(self):
        from assistant.privacy import PrivacyError
        client, _seen = self.client(body=b"\x00", headers={"Content-Encoding": "br"})
        with self.assertRaisesRegex(PrivacyError, "compressed as br, which was not asked"):
            self.private(client)._preflight("private/x", 10)

    def test_the_list_of_models_nested_too_deep_is_an_empty_one(self):
        client, seen = self.client(body=DEEPER.encode())
        self.assertEqual(self.private(client).private_ids(), [])
        self.assertEqual(seen[0].headers["Accept-Encoding"], "gzip, deflate")


if __name__ == "__main__":
    unittest.main()
