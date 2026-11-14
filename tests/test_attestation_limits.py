"""Intel's and NVIDIA's checks (assistant/dcap.py, assistant/nras.py, DA Toolkit's copies,
shared by every DA app) read their JSON to MAX_NESTING (200) levels at most, a deeper answer
refused as one that cannot be read, and their own fetches wait no longer than the
attestation's time leaves (its deadline, on time.monotonic: TeeClient's ATTEMPT_SECONDS),
refused in words once none is left (the DA apps' security standard, its final pass, Nov 14
2026). As DA Toolkit's tests/test_attestation_limits.py. Nothing reaches the network."""

import base64
import os
import sys
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import dcap, nras  # noqa: E402
from assistant.privacy import TeeClient  # noqa: E402

DEEP = "[" * 300 + "]" * 300


class Answer:
    def __init__(self, body, status=200):
        self.content, self.status_code, self.headers = body, status, {}


class Recording:
    """A session that answers every GET with `body` and keeps each request's timeout."""

    def __init__(self, body=b"{}", status=200):
        self.body, self.status, self.timeouts = body, status, []

    def get(self, url, params=None, timeout=None, **_kw):
        self.timeouts.append(timeout)
        return Answer(self.body, self.status)


class JsonIsReadToTwoHundredLevels(unittest.TestCase):
    def test_intels_deeper_answer_is_refused(self):
        self.assertEqual(dcap.MAX_NESTING, 200)
        self.assertEqual(dcap._nesting('{"a": ' + "[" * 199 + "]" * 199 + "}"), 200)
        self.assertEqual(dcap._nesting('{"text": "[[[[ in a string ]"}'), 1)
        with self.assertRaisesRegex(dcap.DcapError, "nested deeper than 200 levels"):
            dcap._loads('{"a": ' + DEEP + "}")
        content = ('{"tcbInfo": {"id": "TDX", "deep": ' + DEEP + '}, "signature": "00"}').encode()
        with self.assertRaisesRegex(dcap.DcapError, "nested deeper"):
            dcap._signed_json(content, "tcbInfo", "", dcap.intel_root(),
                              datetime.now(dcap.UTC))

    def test_nvidias_deeper_token_or_key_set_is_refused(self):
        self.assertEqual(nras.MAX_NESTING, 200)
        deep = base64.urlsafe_b64encode(
            ('{"alg": "ES384", "x": ' + DEEP + "}").encode()).decode().rstrip("=")
        claims = base64.urlsafe_b64encode(b'{"iss": "x"}').decode().rstrip("=")
        signature = base64.urlsafe_b64encode(bytes(96)).decode()
        with self.assertRaisesRegex(nras.NrasError, "nested deeper than 200 levels"):
            nras.verify_token(f"{deep}.{claims}.{signature}", Recording(), "nonce", time.time())
        with mock.patch.object(nras, "_keys", {"at": 0.0, "keys": {}}), \
                self.assertRaisesRegex(nras.NrasError, "nested deeper"):
            nras._jwks(Recording(('{"keys": ' + DEEP + "}").encode()))


class FetchesAreHeldToTheAttestationsTime(unittest.TestCase):
    def test_intels_fetches_wait_only_for_what_is_left(self):
        with mock.patch.object(dcap, "_cache", {}):
            session = Recording(b"collateral")
            dcap._fetch(session, "https://intel.example/a")
            dcap._fetch(session, "https://intel.example/b", deadline=time.monotonic() + 5)
            self.assertEqual(session.timeouts[0], dcap.TIMEOUT_SECONDS)
            self.assertTrue(0 < session.timeouts[1] <= 5)
            with self.assertRaisesRegex(dcap.DcapError, "time ran out before Intel's "
                                                        "collateral could be fetched"):
                dcap._fetch(session, "https://intel.example/c",
                            deadline=time.monotonic() - 1)
            self.assertEqual(len(session.timeouts), 2, "nothing fetched once time is up")

    def test_nvidias_key_fetch_waits_only_for_what_is_left(self):
        session = Recording(b'{"keys": [{"kid": "k1"}]}')
        with mock.patch.object(nras, "_keys", {"at": 0.0, "keys": {}}):
            self.assertEqual(set(nras._jwks(session, deadline=time.monotonic() + 5)), {"k1"})
        self.assertTrue(0 < session.timeouts[0] <= 5)
        with mock.patch.object(nras, "_keys", {"at": 0.0, "keys": {}}), \
                self.assertRaisesRegex(nras.NrasError, "time ran out"):
            nras._jwks(session, deadline=time.monotonic() - 1)

    def test_the_tee_client_gives_its_deadline_to_both(self):
        seen = {}
        with mock.patch.object(dcap, "verify", lambda raw, session, **kw: seen.update(
                intel=kw.get("deadline"))), \
                mock.patch.object(nras, "verify", lambda answer, nonce, session, **kw:
                                  seen.update(nvidia=kw.get("deadline"))):
            client = TeeClient("https://nano-gpt.com/api/v1", "k", "TEE/x")
            client._deadline = 1234.5
            client._intel(b"quote", client.session)
            client._nvidia([], "nonce", client.session)
        self.assertEqual(seen, {"intel": 1234.5, "nvidia": 1234.5})


if __name__ == "__main__":
    unittest.main()
