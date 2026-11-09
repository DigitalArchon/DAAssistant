"""Privacy Mode: attestation, reply signatures, the sealed client, and the rule that a
private conversation never leaves its attested models.

The TEE checks run against a fake nano-gpt that attests, signs and streams like the
real one (the report data layout, NRAS's JWT list and EIP-191 signatures as measured
live, Sept 2026). Intel's and NVIDIA's verification is checked against a real
attestation captured with its collateral (tests/data/tee_attestation.json), at the
moment it was captured, and against forgeries of it; the certificate rules Intel's own
chain keeps, against synthetic chains under a root of their own. The end-to-end
encrypted client runs only where Tinfoil's SDK is installed
(tools/install-private-mode.sh), against a mocked transport.

Run:  python3 -m unittest tests.test_privacy -v
"""

import base64
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401  (pins GI versions)
from assistant import dcap, models, netguard, nras, privacy, sealed
from assistant.config import Config
from assistant.ethsig import (address_of, keccak256, personal_hash, recover,
                              recover_personal, sign)
from assistant.llm import LLMClient, LLMError
from assistant.privacy import ModelRetired, PrivacyError, PrivacySession, TeeClient
from assistant.ui.markdown_html import turn_html

KEY = 0xB25C7DB31FEED9122727BF0939DC769A96564B2DE4C4726D035B36ECF1E5B364
ADDRESS = "0x5ce9454909639D2D17A3F753ce7d93fa0b9aB12E"
API_URL = "https://nano-gpt.com/api/v1/chat/completions"
TEXT = "TEE/glm-5.3-flash"
VISION = "TEE/qwen3.8-27b"


def jwt(claims):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{part({'alg': 'ES384'})}.{part(claims)}.c2ln"


def fake_quote(report_data):
    """A version 4 TDX quote with every field in place and nothing signed: enough for
    parse_quote, and for tests that stand in for Intel's verification."""
    header = (struct.pack("<HHI", 4, dcap.ECDSA_P256, dcap.TDX_TEE_TYPE) + bytes(4)
              + dcap.INTEL_QE_VENDOR_ID + bytes(20))
    body = bytes(520) + report_data
    inner = struct.pack("<HI", dcap.PCK_CHAIN, 0)
    cert = bytes(dcap.QE_REPORT_LEN) + bytes(64) + struct.pack("<H", 0) + inner
    signature_data = bytes(128) + struct.pack("<HI", dcap.QE_REPORT_DATA, len(cert)) + cert
    return (header + body + struct.pack("<I", len(signature_data)) + signature_data).hex()


class FakeResponse:
    def __init__(self, status=200, body=None, lines=(), content=None, headers=None):
        self.status_code = status
        self._body = body
        self._lines = list(lines)
        self.text = json.dumps(body) if body is not None else ""
        self.content = content if content is not None else self.text.encode()
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def sse_lines(reply, response_id="chatcmpl-42"):
    """A streamed reply as requests' iter_lines yields it: a chunk per word, then DONE."""
    words = reply.split(" ")
    pieces = [words[0]] + [" " + word for word in words[1:]]
    return [b"data: " + json.dumps({"id": response_id,
                                    "choices": [{"delta": {"content": piece}}]}).encode()
            for piece in pieces] + [b"data: [DONE]"]


UP_TO_DATE = dcap.Result(status="UpToDate", platform="UpToDate", qe="UpToDate",
                         notes=["Intel's certificate chain verifies"])


class FakeNanoGpt:
    """A requests.Session standing in for nano-gpt: TEE attestation and signatures, NRAS,
    and the chat endpoint. Records every request so a test can say none was made.

    Intel's and NVIDIA's verification are stood in for by `intel` (a dcap.Result to
    return, or an exception to raise) and `nvidia` (a GPU count, or an exception); the
    real ones are tested against real evidence in RealEvidence below."""

    def __init__(self, nonce_bound=True, gpu=True, evidence=True, signer=KEY,
                 attestation_status=200, signature_status=200, reply="Hello there",
                 intel=UP_TO_DATE, nvidia=8, nras_status=200, attestation_extra=None):
        self.nonce_bound = nonce_bound
        # More of the attestation's body: what RedPill's gateway adds, say.
        self.attestation_extra = attestation_extra or {}
        self.gpu = gpu
        self.evidence = evidence
        self.signer = signer
        self.attestation_status = attestation_status
        self.signature_status = signature_status
        self.reply = reply
        self.intel = intel
        self.nvidia = nvidia if gpu else nras.NrasError("NVIDIA rejected the GPU evidence")
        self.nras_status = nras_status
        self.gets = []
        self.posts = []

    def intel_verify(self, raw, session):
        if isinstance(self.intel, BaseException):
            raise self.intel
        return self.intel

    def nvidia_verify(self, answer, nonce, session):
        if isinstance(self.nvidia, BaseException):
            raise self.nvidia
        return self.nvidia

    def tee_client(self, url, key, model):
        return TeeClient(url, key, model, session=self, intel=self.intel_verify,
                         nvidia=self.nvidia_verify)

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append(url)
        if "/tee/attestation" in url:
            if self.attestation_status != 200:
                return FakeResponse(self.attestation_status, {"error": "no"})
            nonce = params["nonce"]
            address = address_of(KEY).lower()
            report = bytes.fromhex(address[2:]) + bytes(12) + (
                bytes.fromhex(nonce) if self.nonce_bound else bytes(32))
            return FakeResponse(200, {
                "signing_address": address,
                "attestation": {"report_data": report.hex()},
                "intel_quote": fake_quote(report),
                "nvidia_payload": json.dumps(
                    {"nonce": nonce,
                     "evidence_list": [{"evidence": "e"}] if self.evidence else []}),
                **self.attestation_extra,
            })
        if "/tee/signature/" in url:
            if self.signature_status != 200:
                return FakeResponse(self.signature_status, {"error": "none kept"})
            text = "aa:bb"
            signature = sign(personal_hash(text.encode()), self.signer, 12345).hex()
            return FakeResponse(200, {"text": text, "signature": signature})
        return FakeResponse(404, {})

    def post(self, url, json=None, headers=None, data=None, stream=False, timeout=None):
        self.posts.append({"url": url, "data": data, "json": json})
        if url.startswith(privacy.NRAS_URL):
            # As NVIDIA's tokens are: for the nonce it was sent, and current.
            now = int(time.time())
            return FakeResponse(self.nras_status,
                                [["JWT", jwt({"x-nvidia-overall-att-result": self.gpu,
                                              "eat_nonce": (json or {}).get("nonce"),
                                              "nbf": now - 60, "exp": now + 3600})],
                                 {}])
        if url == API_URL:
            return FakeResponse(200, None, sse_lines(self.reply))
        return FakeResponse(404, {})

    @property
    def chat_posts(self):
        return [p for p in self.posts if p["url"] == API_URL]


def session_for(server, text=TEXT, vision=VISION):
    return PrivacySession(API_URL, "k", text, vision, tee_factory=server.tee_client)


class TempConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"
        self.config.api_url = API_URL
        # The TEE/ models FakeNanoGpt attests, which were the defaults until the
        # private/ ones (Nov 2026): what these tests send to, whatever the defaults.
        self.config.private_text_model = TEXT
        self.config.private_premium_model = "TEE/glm-5.3"
        self.config.private_vision_model = VISION

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# ---- signatures ----------------------------------------------------------------------

class Signatures(unittest.TestCase):
    def test_keccak_and_addresses_match_known_vectors(self):
        self.assertEqual(keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(keccak256(b"abc").hex(),
                         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
        self.assertEqual(address_of(1), "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf")
        self.assertEqual(address_of(KEY), ADDRESS)

    def test_a_signature_recovers_its_signer_and_only_its_signer(self):
        digest = personal_hash(b"req:resp")
        signature = sign(digest, KEY, 987654321)
        self.assertEqual(recover(digest, signature), ADDRESS)
        self.assertEqual(recover_personal("req:resp", signature.hex()), ADDRESS)
        self.assertNotEqual(recover(personal_hash(b"tampered"), signature), ADDRESS)


# ---- TEE attestation -----------------------------------------------------------------

class TeeAttestation(unittest.TestCase):
    def client(self, **server):
        return FakeNanoGpt(**server).tee_client(API_URL, "k", TEXT)

    def test_a_bound_quote_intel_and_nvidia_vouch_for_is_fully_attested(self):
        attestation = self.client().attest()
        self.assertEqual(attestation.level, "full")
        self.assertTrue(attestation.gpu_ok)
        self.assertEqual(attestation.gpus, 8)
        self.assertEqual(attestation.tcb_status, "UpToDate")
        self.assertEqual(attestation.signing_address, address_of(KEY).lower())
        self.assertIn("binds the signing key to our nonce", attestation.detail)
        self.assertIn("vouches for 8 GPUs", attestation.detail)
        self.assertEqual(attestation.headline, "TEE attested")

    def test_no_gpu_evidence_is_partial_and_says_so(self):
        attestation = self.client(evidence=False).attest()
        self.assertEqual(attestation.level, "partial")
        self.assertIsNone(attestation.gpu_ok)
        self.assertIn("no GPU evidence", attestation.headline)

    def test_an_unbound_nonce_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "did not bind"):
            self.client(nonce_bound=False).attest()

    def test_a_quote_intel_does_not_vouch_for_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "failed verification: .*revoked"):
            self.client(intel=dcap.DcapError("the PCK certificate has been revoked")).attest()

    def test_a_tcb_that_is_not_current_is_partial_and_named(self):
        stale = dcap.Result(status="OutOfDate", platform="OutOfDate", qe="UpToDate")
        attestation = self.client(intel=stale).attest()
        self.assertEqual(attestation.level, "partial")
        self.assertIn("Intel TCB OutOfDate", attestation.headline)

    def test_intel_out_of_reach_is_partial_not_a_pass(self):
        attestation = self.client(intel=dcap.Result(collateral=False)).attest()
        self.assertEqual(attestation.level, "partial")
        self.assertIn("Intel TCB unchecked", attestation.headline)

    def test_without_cryptography_nothing_is_attested_and_it_says_what_to_install(self):
        """It used to pass with "signatures unchecked": the quote's bytes, unchecked,
        named the key, and every reply that key signed then read "signed by attested
        enclave"."""
        for intel, nvidia in ((ImportError("cryptography"), ImportError("cryptography")),
                              (UP_TO_DATE, ImportError("cryptography"))):
            with self.assertRaisesRegex(PrivacyError, "cannot be checked without Python's "
                                                      "cryptography package "
                                                      r"\(python3-cryptography\)"):
                self.client(intel=intel, nvidia=nvidia).attest()

    def test_a_verdict_nvidia_did_not_sign_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "GPU attestation failed: .*nonce"):
            self.client(nvidia=nras.NrasError("not made for this request's nonce")).attest()
        with self.assertRaisesRegex(PrivacyError, "NVIDIA rejected"):
            self.client(gpu=False).attest()
        with self.assertRaisesRegex(PrivacyError, "NVIDIA rejected"):
            self.client(nras_status=400).attest()

    def test_nvidia_out_of_reach_is_partial(self):
        self.assertIn("GPUs unverified", self.client(nras_status=503).attest().headline)
        keys = self.client(nvidia=nras.KeysUnavailable("keys could not be fetched"))
        self.assertIn("NVIDIA signature unchecked", keys.attest().headline)

    def test_nvidia_is_asked_about_the_gpu_evidence_with_our_nonce(self):
        """nano-gpt's copy of the GPU evidence names a nonce of its own, and NVIDIA was
        sent that one: evidence replayed with the nonce it was made for came back
        vouched for, for that nonce. Now NVIDIA checks it against ours."""
        address = address_of(KEY).lower()
        ours = []

        def respond(nonce):
            ours.append(nonce)
            report = bytes.fromhex(address[2:]) + bytes(12) + bytes.fromhex(nonce)
            return 200, {"signing_address": address, "intel_quote": fake_quote(report),
                         "nvidia_payload": json.dumps({
                             "nonce": "0d" * 32, "arch": "HOPPER",
                             "evidence_list": [{"evidence": "e"}]})}

        server = ShapedNanoGpt(respond)
        server.tee_client(API_URL, "k", TEXT).attest()
        self.assertEqual(server.nras_nonces, ours)
        sent = next(p["json"] for p in server.posts if p["url"] == privacy.NRAS_URL)
        self.assertEqual((sent["arch"], sent["evidence_list"]), ("HOPPER",
                                                                 [{"evidence": "e"}]))

    def test_a_model_offering_no_attestation_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "offers no attestation"):
            self.client(attestation_status=400).attest()

    def test_reply_signatures(self):
        client = self.client()
        client.attest()
        self.assertEqual(client.check_reply("chatcmpl-1")[0], privacy.SIGNED)

        other = self.client(signer=1)
        other.attest()
        self.assertEqual(other.check_reply("chatcmpl-1")[0], privacy.MISMATCH)

        none_kept = self.client(signature_status=500)
        none_kept.attest()
        self.assertEqual(none_kept.check_reply("chatcmpl-1")[0], privacy.UNSIGNED)
        self.assertEqual(client.check_reply(None)[0], privacy.UNSIGNED)


# ---- real evidence -------------------------------------------------------------------

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                       "tee_attestation.json")


def _has_cryptography():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def key_usage(cert_sign):
    """keyUsage as Intel's certificates have it: a CA's key signs certificates and
    CRLs, any other's signs data."""
    from cryptography import x509
    return x509.KeyUsage(digital_signature=not cert_sign, content_commitment=not cert_sign,
                         key_encipherment=False, data_encipherment=False,
                         key_agreement=False, key_cert_sign=cert_sign, crl_sign=cert_sign,
                         encipher_only=False, decipher_only=False)


def synthetic_cert(name, key, issuer=None, ca=None, path_length=None, cert_sign=None):
    """A certificate named `name` for `key`, signed by `issuer` - (its certificate, its
    key) - or by itself; with basicConstraints if `ca` is given, and keyUsage if
    `cert_sign` is."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
    issuer_cert, issuer_key = issuer or (None, key)
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer_cert.subject if issuer_cert else subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(datetime(2018, 1, 1, tzinfo=timezone.utc))
               .not_valid_after(datetime(2049, 1, 1, tzinfo=timezone.utc)))
    if ca is not None:
        builder = builder.add_extension(x509.BasicConstraints(ca=ca, path_length=path_length),
                                        critical=True)
    if cert_sign is not None:
        builder = builder.add_extension(key_usage(cert_sign), critical=True)
    return builder.sign(issuer_key, hashes.SHA256())


class FixtureSession:
    """Serves one real attestation, captured live from TEE/glm-5.3-flash with Intel's
    collateral and NVIDIA's answer and key, exactly as they came (tests/data)."""

    def __init__(self, fixture, intel_status=200, jwks=None):
        self.fixture = fixture
        self.intel_status = intel_status
        self.jwks = fixture["jwks"] if jwks is None else jwks
        self.closed = False

    def close(self):
        self.closed = True

    def get(self, url, params=None, headers=None, timeout=None):
        if "/tee/attestation" in url:
            return FakeResponse(200, {
                "signing_address": self.fixture["signing_address"],
                "intel_quote": self.fixture["intel_quote"],
                "nvidia_payload": json.dumps({"nonce": self.fixture["nonce"],
                                              "evidence_list": [{"evidence": "e"}]})})
        if url == nras.JWKS_URL:
            return FakeResponse(200, self.jwks)
        for item in self.fixture["collateral"]:
            if item["url"] == url and (item["params"] or None) == (params or None):
                if self.intel_status != 200:
                    return FakeResponse(self.intel_status, {})
                return FakeResponse(item["status"], None,
                                    content=base64.b64decode(item["body"]),
                                    headers=item["headers"])
        return FakeResponse(404, {})

    def post(self, url, json=None, headers=None, timeout=None, **_kw):
        if url == privacy.NRAS_URL:
            return FakeResponse(200, self.fixture["nras_answer"])
        return FakeResponse(404, {})


class KeysOutOfReach(FakeNanoGpt):
    """nano-gpt as FakeNanoGpt has it, a quote binding our nonce and all, but NVIDIA's
    service answering with the verdict captured in tests/data - made for the nonce of
    that request - and its signing keys out of reach."""

    def __init__(self, fixture, **kwargs):
        super().__init__(**kwargs)
        self.fixture = fixture

    def get(self, url, params=None, headers=None, timeout=None):
        if url == nras.JWKS_URL:
            self.gets.append(url)
            return FakeResponse(503, {})
        return super().get(url, params, headers, timeout)

    def post(self, url, json=None, **kwargs):
        if url == privacy.NRAS_URL:
            self.posts.append({"url": url, "json": json})
            return FakeResponse(200, self.fixture["nras_answer"])
        return super().post(url, json=json, **kwargs)


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class RealEvidence(unittest.TestCase):
    """Intel's and NVIDIA's real signatures, checked at the moment they were captured."""

    def setUp(self):
        with open(FIXTURE) as f:
            self.fixture = json.load(f)
        self.now = self.fixture["now"]
        self.when = datetime.fromtimestamp(self.now, timezone.utc)
        self.quote = bytes.fromhex(self.fixture["intel_quote"])
        self.session = FixtureSession(self.fixture)
        dcap._cache.clear()
        nras._keys.update(at=0.0, keys={})

    def tearDown(self):
        dcap._cache.clear()
        nras._keys.update(at=0.0, keys={})

    def intel(self, raw=None, session=None):
        return dcap.verify(raw or self.quote, session or self.session, now=self.when)

    def nvidia(self, answer=None, nonce=None, session=None, now=None):
        return nras.verify(answer or self.fixture["nras_answer"],
                           nonce or self.fixture["nonce"], session or self.session,
                           now=now or self.now)

    # Intel

    def test_the_quote_verifies_up_to_intels_root_and_is_current(self):
        result = self.intel()
        self.assertTrue(result.collateral)
        self.assertEqual(result.status, "UpToDate")
        self.assertEqual((result.platform, result.module, result.qe),
                         ("UpToDate", "UpToDate", "UpToDate"))
        self.assertEqual(result.fmspc, "20a06f000000")

    def test_a_revoked_tcb_is_refused_whatever_else_is_named(self):
        """Only the worst status was compared with Revoked, and one this app does not
        list ranks above it: a platform Intel had revoked passed as partial beside it."""
        for platform, module, qe in (("Revoked", "TDRelaunchAdvised", "UpToDate"),
                                     ("UpToDate", "Revoked", "SomethingNew"),
                                     ("SomethingNew", "UpToDate", "Revoked")):
            dcap._cache.clear()
            with mock.patch.object(dcap, "platform_level",
                                   return_value={"tcbStatus": platform}), \
                    mock.patch.object(dcap, "module_status", return_value=module), \
                    mock.patch.object(dcap, "qe_status", return_value=qe), \
                    self.assertRaisesRegex(dcap.DcapError, "revoked this platform"):
                self.intel()
        dcap._cache.clear()
        with mock.patch.object(dcap, "module_status", return_value="TDRelaunchAdvised"):
            result = self.intel()
        self.assertEqual(result.status, "TDRelaunchAdvised")    # partial, and named

    def test_a_changed_report_breaks_the_quote_signature(self):
        tampered = bytearray(self.quote)
        tampered[dcap.HEADER_LEN + 520] ^= 1            # the report data: our nonce
        with self.assertRaisesRegex(dcap.DcapError, "quote's signature"):
            self.intel(bytes(tampered))

    def test_a_debug_td_is_refused(self):
        debug = bytearray(self.quote)
        debug[dcap.HEADER_LEN + 120] |= 0x01            # TDATTRIBUTES.DEBUG
        with self.assertRaisesRegex(dcap.DcapError, "debug TD"):
            self.intel(bytes(debug))

    def test_a_chain_to_any_other_root_is_refused(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Intel SGX Root CA"),
                          x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME,
                                             "Intel Corporation"),
                          x509.NameAttribute(x509.NameOID.LOCALITY_NAME, "Santa Clara"),
                          x509.NameAttribute(x509.NameOID.STATE_OR_PROVINCE_NAME, "CA"),
                          x509.NameAttribute(x509.NameOID.COUNTRY_NAME, "US")])
        # Intel's root in every respect but its key, extensions included.
        impostor = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(1)
                    .not_valid_before(datetime(2018, 1, 1, tzinfo=timezone.utc))
                    .not_valid_after(datetime(2049, 1, 1, tzinfo=timezone.utc))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=1),
                                   critical=True)
                    .add_extension(key_usage(cert_sign=True), critical=True)
                    .sign(key, hashes.SHA256()))
        original = dcap.intel_root
        dcap.intel_root = lambda: impostor
        try:
            with self.assertRaisesRegex(dcap.DcapError, "signature does not verify"):
                self.intel()
        finally:
            dcap.intel_root = original

    def test_a_revocation_list_intel_did_not_sign_is_refused(self):
        forged = json.loads(json.dumps(self.fixture))
        crl = bytearray(base64.b64decode(forged["collateral"][0]["body"]))
        crl[-5] ^= 1                                    # inside the CRL's signature
        forged["collateral"][0]["body"] = base64.b64encode(bytes(crl)).decode()
        with self.assertRaisesRegex(dcap.DcapError, "CRL is not signed"):
            self.intel(session=FixtureSession(forged))

    def test_tcb_information_intel_did_not_sign_is_refused(self):
        forged = json.loads(json.dumps(self.fixture))
        body = base64.b64decode(forged["collateral"][2]["body"]).decode()
        body = body.replace('"tcbEvaluationDataNumber":', '"tcbEvaluationDataNumber":1', 1)
        forged["collateral"][2]["body"] = base64.b64encode(body.encode()).decode()
        with self.assertRaisesRegex(dcap.DcapError, "signature on its tcbInfo"):
            self.intel(session=FixtureSession(forged))

    def test_tcb_information_signed_with_a_pck_chain_is_refused(self):
        """A PCK certificate chains to Intel's root just as the TCB signing one does,
        and its key lives in the very platform whose TCB is being judged."""
        from urllib.parse import quote as url_quote
        pck_chain = dcap.parse_quote(self.quote).pck_chain_pem.decode()
        for index, header, key in ((2, "TCB-Info-Issuer-Chain", "tcbInfo"),
                                   (3, "SGX-Enclave-Identity-Issuer-Chain",
                                    "enclaveIdentity")):
            dcap._cache.clear()
            forged = json.loads(json.dumps(self.fixture))
            forged["collateral"][index]["headers"][header] = url_quote(pck_chain)
            with self.assertRaisesRegex(dcap.DcapError, f"Intel's {key} isn't signed by "
                                                        f"Intel's TCB signing certificate"):
                self.intel(session=FixtureSession(forged))

    def test_collateral_out_of_reach_is_reported_not_passed(self):
        result = self.intel(session=FixtureSession(self.fixture, intel_status=503))
        self.assertFalse(result.collateral)
        self.assertEqual(result.status, "")
        self.assertIn("could not be fetched", result.notes[-1])

    def test_collateral_that_arrived_but_cannot_be_read_is_refused(self):
        """Out of reach is "unchecked", a partial attestation. What did arrive and
        cannot be read was taken for the same - a CRL that is no CRL, an issuer chain or
        a signed document left out, JSON that does not parse - so that an answer shaped
        to fail parsing, by whatever answered at Intel's address, bought a pass as
        "unchecked" for a revoked platform. It is refused now, as a bad signature is.
        From DA Vibe Manager's copy of this module (Nov 2026)."""
        def forged(index, body=None, drop_header=None):
            copy = json.loads(json.dumps(self.fixture))
            if body is not None:
                copy["collateral"][index]["body"] = base64.b64encode(body).decode()
            if drop_header:
                del copy["collateral"][index]["headers"][drop_header]
            return FixtureSession(copy)

        for session, refusal in (
                (forged(0, b"<html>not a CRL</html>"), "a CRL from Intel could not be read"),
                (forged(1, b"<html>not a CRL</html>"), "a CRL from Intel could not be read"),
                (forged(2, drop_header="TCB-Info-Issuer-Chain"),
                 "Intel's response had no TCB-Info-Issuer-Chain"),
                (forged(3, drop_header="SGX-Enclave-Identity-Issuer-Chain"),
                 "Intel's response had no SGX-Enclave-Identity-Issuer-Chain"),
                (forged(2, b'{"signature": "00"}'), "Intel's tcbInfo was missing"),
                (forged(3, b'{"enclaveIdentity": {"id": "TD_QE"'),
                 "Intel's collateral could not be read")):
            dcap._cache.clear()
            with self.subTest(refusal), self.assertRaisesRegex(dcap.DcapError, refusal):
                self.intel(session=session)

    def test_an_attestation_whose_collateral_cannot_be_read_is_refused_not_partial(self):
        copy = json.loads(json.dumps(self.fixture))
        copy["collateral"][0]["body"] = base64.b64encode(b"<html>maintenance</html>").decode()
        client = TeeClient(API_URL, "k", self.fixture["model"], session=FixtureSession(copy),
                           intel=lambda raw, s: dcap.verify(raw, s, now=self.when),
                           nvidia=lambda a, n, s: nras.verify(a, n, s, now=self.now))
        with mock.patch.object(privacy.secrets, "token_hex",
                               return_value=self.fixture["nonce"]), \
                self.assertRaisesRegex(PrivacyError, "failed verification: a CRL from "
                                                     "Intel could not be read"):
            client.attest()

    def test_the_quoting_enclave_is_intels_whether_or_not_its_collateral_can_be_had(self):
        """It is what makes the attestation key the QE's, not any enclave's the platform
        certified. It was checked only against Intel's QE identity, fetched with the
        collateral: out of reach, or made so, a quote such an enclave signed passed as
        partial."""
        quote = dcap.parse_quote(self.quote)
        dcap.qe_is_intels(dcap.INTEL_TD_QE, quote.qe_report)       # the real one is
        other = bytearray(quote.qe_report)
        other[128:160] = b"\x11" * 32                               # another's MRSIGNER
        with self.assertRaisesRegex(dcap.DcapError, "not signed by Intel"):
            dcap.qe_is_intels(dcap.INTEL_TD_QE, bytes(other))
        # The real quote, held to an identity its QE does not have, with Intel's
        # collateral out of reach: refused, not partial.
        impostor = dict(dcap.INTEL_TD_QE, mrsigner="11" * 32)
        with mock.patch.object(dcap, "INTEL_TD_QE", impostor), \
                self.assertRaisesRegex(dcap.DcapError, "not signed by Intel"):
            self.intel(session=FixtureSession(self.fixture, intel_status=503))

    def test_the_td_qe_identity_pinned_is_the_one_intel_signed(self):
        item = next(c for c in self.fixture["collateral"]
                    if c["url"] == dcap.QE_IDENTITY_URL)
        signed = json.loads(base64.b64decode(item["body"]))["enclaveIdentity"]
        self.assertEqual(dcap.INTEL_TD_QE, {key: signed[key] for key in dcap.INTEL_TD_QE})

    # NVIDIA

    def test_nvidias_verdict_verifies_for_every_gpu(self):
        self.assertEqual(self.nvidia(), 8)

    def test_a_verdict_for_another_nonce_is_refused(self):
        with self.assertRaisesRegex(nras.NrasError, "nonce"):
            self.nvidia(nonce="00" * 32)

    def test_an_old_verdict_is_refused(self):
        with self.assertRaisesRegex(nras.NrasError, "validity window"):
            self.nvidia(now=self.now + 86400)

    def test_edited_claims_break_nvidias_signature(self):
        answer = json.loads(json.dumps(self.fixture["nras_answer"]))
        header, claims, signature = answer[0][1].split(".")
        decoded = json.loads(base64.urlsafe_b64decode(claims + "=="))
        decoded["jti"] = "replayed"
        claims = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
        answer[0][1] = f"{header}.{claims}.{signature}"
        with self.assertRaisesRegex(nras.NrasError, "signature does not verify"):
            self.nvidia(answer)

    def test_a_gpu_token_swapped_for_another_is_refused(self):
        answer = json.loads(json.dumps(self.fixture["nras_answer"]))
        gpus = sorted(answer[1])
        answer[1][gpus[0]] = answer[1][gpus[1]]
        with self.assertRaisesRegex(nras.NrasError, "not the one"):
            self.nvidia(answer)

    def test_a_key_nvidia_does_not_publish_is_refused(self):
        session = FixtureSession(self.fixture, jwks={"keys": []})
        with self.assertRaisesRegex(nras.NrasError, "does not publish"):
            self.nvidia(session=session)

    def attest_with_nvidias_keys_out_of_reach(self, nonce, now):
        """The attestation, made with `nonce`, at `now`, of a TEE model whose GPU
        evidence NVIDIA answered for with the captured verdict."""
        server = KeysOutOfReach(self.fixture)
        client = TeeClient(API_URL, "k", TEXT, session=server, intel=server.intel_verify)
        try:
            with mock.patch.object(privacy.secrets, "token_hex", return_value=nonce), \
                    mock.patch("time.time", return_value=now):
                return client.attest()
        finally:
            self.assertIn(nras.JWKS_URL, server.gets, "NVIDIA's keys were not looked for")

    def test_with_nvidias_keys_out_of_reach_a_verdict_for_another_nonce_is_refused(self):
        """Without its keys NVIDIA's signatures cannot be checked, and only the overall
        verdict used to be read: one made for an earlier request passed as "unchecked".
        The answer comes over TLS from NVIDIA itself, so its nonce and its time are
        held to even then."""
        with self.assertRaisesRegex(PrivacyError, "GPU attestation failed: NVIDIA's "
                                                  "verdict was not made for this "
                                                  "request's nonce"):
            self.attest_with_nvidias_keys_out_of_reach("0d" * 32, self.now)

    def test_with_nvidias_keys_out_of_reach_an_old_verdict_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "GPU attestation failed: a token is "
                                                  "outside its validity window"):
            self.attest_with_nvidias_keys_out_of_reach(self.fixture["nonce"],
                                                       self.now + 86400)

    def test_without_nvidias_keys_each_gpus_verdict_is_still_checked(self):
        """Only the overall verdict, its nonce and its time were read without NVIDIA's
        keys: a GPU whose token said its measurements failed, or a GPU token the
        overall one does not list, passed as "unchecked"."""
        no_keys = FixtureSession(self.fixture, jwks={"no": "keys"})
        with self.assertRaises(nras.KeysUnavailable):
            self.nvidia(session=no_keys)                    # as it came: unchecked
        label, overall = self.fixture["nras_answer"][0]
        tokens = dict(self.fixture["nras_answer"][1])
        gpu = sorted(tokens)[0]
        header, claims, signature = tokens[gpu].split(".")
        failing = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        failing["measres"] = "fail"
        part = base64.urlsafe_b64encode(json.dumps(failing).encode()).decode().rstrip("=")
        tokens[gpu] = f"{header}.{part}.{signature}"
        # Listed by the overall verdict, digest and all: only the signatures, which go
        # unchecked here, would have told it from NVIDIA's.
        o_header, o_claims, o_signature = overall.split(".")
        verdict = json.loads(base64.urlsafe_b64decode(o_claims + "=" * (-len(o_claims) % 4)))
        verdict["submods"][gpu][1][1] = hashlib.sha256(tokens[gpu].encode()).hexdigest()
        o_part = base64.urlsafe_b64encode(json.dumps(verdict).encode()).decode().rstrip("=")
        forged = [[label, f"{o_header}.{o_part}.{o_signature}"], tokens]
        with self.assertRaisesRegex(nras.NrasError, f"{gpu} failing: measurements"):
            self.nvidia(answer=forged, session=no_keys)
        unlisted = [[label, overall], dict(tokens)]
        with self.assertRaisesRegex(nras.NrasError, "not the one NVIDIA's verdict lists"):
            self.nvidia(answer=unlisted, session=no_keys)

    def test_keys_fetched_before_are_used_when_fresh_ones_cannot_be_had(self):
        """Older than an hour, but NVIDIA's, fetched over TLS from its own service: a
        signature checked against them beats one not checked at all."""
        self.assertEqual(self.nvidia(), 8)                  # fetched now, and kept
        nras._keys["at"] = dcap.since_boot() - nras.KEYS_TTL - 1
        no_keys = FixtureSession(self.fixture, jwks={"no": "keys"})
        self.assertEqual(self.nvidia(session=no_keys), 8)
        label, overall = self.fixture["nras_answer"][0]
        forged = overall[:-4] + ("AAAA" if not overall.endswith("AAAA") else "BBBB")
        with self.assertRaisesRegex(nras.NrasError, "signature does not verify"):
            self.nvidia(answer=[[label, forged], self.fixture["nras_answer"][1]],
                        session=no_keys)

    def test_with_nvidias_keys_out_of_reach_a_current_verdict_is_partial(self):
        attestation = self.attest_with_nvidias_keys_out_of_reach(self.fixture["nonce"],
                                                                 self.now)
        self.assertEqual(attestation.level, "partial")
        self.assertIn("NVIDIA signature unchecked", attestation.headline)
        self.assertIn("NVIDIA's verdict unchecked", attestation.summary)

    # where their requests go

    def test_without_a_session_given_each_uses_netguards(self):
        """They made their own requests.Session, which follows a redirect wherever it
        points, https to plain http included. netguard's follows one only where the
        request could have been sent in the first place."""
        import inspect

        import requests
        for module in (privacy, dcap, nras):
            self.assertNotIn("requests.Session(", inspect.getsource(module))
        plain = AssertionError("a plain requests.Session was made")
        with mock.patch.object(netguard, "session", return_value=self.session) as made, \
                mock.patch.object(requests, "Session", side_effect=plain):
            self.assertEqual(dcap.verify(self.quote, now=self.when).status, "UpToDate")
            self.assertTrue(self.session.closed, "dcap.verify left its own session open")
            self.assertEqual(nras.verify(self.fixture["nras_answer"], self.fixture["nonce"],
                                         now=self.now), 8)
            self.assertIs(TeeClient(API_URL, "k", TEXT).session, self.session)
        self.assertEqual(made.call_count, 3)

    # together

    def test_the_whole_attestation_is_full(self):
        client = TeeClient(API_URL, "k", self.fixture["model"], session=self.session,
                           intel=lambda raw, s: dcap.verify(raw, s, now=self.when),
                           nvidia=lambda a, n, s: nras.verify(a, n, s, now=self.now))
        original = privacy.secrets.token_hex
        privacy.secrets.token_hex = lambda _n: self.fixture["nonce"]
        try:
            attestation = client.attest()
        finally:
            privacy.secrets.token_hex = original
        self.assertEqual(attestation.headline, "TEE attested")
        self.assertEqual(attestation.level, "full")
        self.assertEqual((attestation.gpus, attestation.tcb_status), (8, "UpToDate"))
        self.assertEqual(attestation.signing_address, self.fixture["signing_address"])


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class IntelsChainRules(unittest.TestCase):
    """verify_chain held a certificate to its issuer's name, signature and dates, and
    no more: any certificate Intel's root vouched for - the TCB signing one, a PCK
    certificate whose key a compromised platform gave up - could issue certificates of
    its own that passed as Intel's. And Intel's TCB information and QE identity were
    believed signed by any certificate chaining to the root. Synthetic chains, under a
    root of their own; the real one is RealEvidence's."""

    NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)

    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives.asymmetric import ec
        cls.keys = {name: ec.generate_private_key(ec.SECP256R1())
                    for name in ("root", "ca", "leaf")}

    def root(self, path_length=1):
        return synthetic_cert("Test Root CA", self.keys["root"], ca=True,
                              path_length=path_length, cert_sign=True)

    def under(self, issuer, issuer_key, name, key, **extensions):
        return synthetic_cert(name, self.keys[key], (issuer, self.keys[issuer_key]),
                              **extensions)

    def test_a_chain_of_authorities_allowed_to_sign_verifies(self):
        root = self.root()
        ca = self.under(root, "root", "Test CA", "ca", ca=True, path_length=0,
                        cert_sign=True)
        leaf = self.under(ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
        dcap.verify_chain([leaf, ca], root, self.NOW)

    def test_a_chain_through_a_certificate_that_is_no_ca_is_refused(self):
        root = self.root()
        signing = self.under(root, "root", "Intel SGX TCB Signing", "ca", ca=False,
                             cert_sign=False)
        leaf = self.under(signing, "ca", "Intel SGX PCK Certificate", "leaf", ca=False,
                          cert_sign=False)
        with self.assertRaisesRegex(dcap.DcapError, "CN=Intel SGX TCB Signing signed a "
                                                    "certificate, and is not a "
                                                    "certificate authority"):
            dcap.verify_chain([leaf, signing], root, self.NOW)
        # Nor one that does not say what it is.
        bare = self.under(root, "root", "Bare", "ca")
        leaf = self.under(bare, "ca", "Test leaf", "leaf", ca=False)
        with self.assertRaisesRegex(dcap.DcapError, "Bare signed a certificate, and is "
                                                    "not a certificate authority"):
            dcap.verify_chain([leaf, bare], root, self.NOW)

    def test_an_authority_beyond_its_path_length_is_refused(self):
        root = self.root(path_length=0)
        ca = self.under(root, "root", "Test CA", "ca", ca=True, path_length=0,
                        cert_sign=True)
        leaf = self.under(ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
        with self.assertRaisesRegex(dcap.DcapError, "Test Root CA may have 0 certificate "
                                                    "authorities below it, and the chain "
                                                    "puts 1 there"):
            dcap.verify_chain([leaf, ca], root, self.NOW)

    def test_an_authority_whose_key_is_not_for_certificates_is_refused(self):
        root = self.root()
        ca = self.under(root, "root", "Test CA", "ca", ca=True, path_length=0,
                        cert_sign=False)
        leaf = self.under(ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
        with self.assertRaisesRegex(dcap.DcapError, "Test CA signed a certificate with "
                                                    "a key not meant for that"):
            dcap.verify_chain([leaf, ca], root, self.NOW)

    def signed_tcb_info(self, signer_name, through_ca=False):
        """Intel's TCB info as its service sends it, signed by a certificate named
        `signer_name` that the root issued - or, `through_ca`, that a CA under the root
        issued."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.serialization import Encoding
        root = self.root()
        issuers = [root]
        if through_ca:
            issuers.insert(0, self.under(root, "root", "Intel SGX PCK Platform CA", "ca",
                                         ca=True, path_length=0, cert_sign=True))
        signer = self.under(issuers[0], "ca" if through_ca else "root", signer_name,
                            "leaf", ca=False, cert_sign=False)
        raw = json.dumps({"id": "TDX", "fmspc": "20a06f000000"}, separators=(",", ":"))
        r, s = decode_dss_signature(self.keys["leaf"].sign(raw.encode(),
                                                           ec.ECDSA(hashes.SHA256())))
        signature = (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()
        content = f'{{"tcbInfo":{raw},"signature":"{signature}"}}'.encode()
        chain = b"".join(cert.public_bytes(Encoding.PEM) for cert in [signer] + issuers)
        return dcap._signed_json(content, "tcbInfo", chain.decode(), root, self.NOW)

    def test_tcb_info_signed_by_any_but_the_tcb_signing_certificate_is_refused(self):
        info, signer = self.signed_tcb_info("Intel SGX TCB Signing")
        self.assertEqual(info["fmspc"], "20a06f000000")
        for name in ("Intel SGX PCK Certificate", "Intel SGX TCB Signing 2"):
            with self.assertRaisesRegex(dcap.DcapError, "Intel's tcbInfo isn't signed by "
                                                        "Intel's TCB signing certificate"):
                self.signed_tcb_info(name)

    def test_the_tcb_signing_certificate_must_be_the_roots_own(self):
        """Named as Intel's TCB signing certificate, but issued by a CA under the root:
        its chain verifies, a CA allowed to issue and all, and its name was all that was
        asked of it. Now it must be the only certificate between the signature and the
        root, as in the other apps' copies of this module (Nov 2026)."""
        root = self.root()
        ca = self.under(root, "root", "Intel SGX PCK Platform CA", "ca", ca=True,
                        path_length=0, cert_sign=True)
        signer = self.under(ca, "ca", "Intel SGX TCB Signing", "leaf", ca=False,
                            cert_sign=False)
        dcap.verify_chain([signer, ca], root, self.NOW)             # a chain that holds
        with self.assertRaisesRegex(dcap.DcapError, "Intel's tcbInfo isn't signed by "
                                                    "Intel's TCB signing certificate"):
            self.signed_tcb_info("Intel SGX TCB Signing", through_ca=True)


@unittest.skipUnless(hasattr(time, "CLOCK_BOOTTIME"), "no CLOCK_BOOTTIME here")
class ClocksThatCountSleep(unittest.TestCase):
    """The end-to-end encrypted enclave's five-minute attestation, Intel's collateral
    and NVIDIA's keys were aged by time.monotonic, which stops while the machine is
    suspended: a laptop that slept for hours took them all for as fresh as when it
    went to sleep. Here the boot clock moves on while the monotonic one stands still,
    as across a suspend."""

    def setUp(self):
        self.boot = 1000.0
        real = time.clock_gettime

        def clock_gettime(clock):
            return self.boot if clock == time.CLOCK_BOOTTIME else real(clock)

        for patch in (mock.patch.object(time, "clock_gettime", clock_gettime),
                      mock.patch.object(time, "monotonic", return_value=50.0)):
            patch.start()
            self.addCleanup(patch.stop)

    def sleep(self, hours):
        self.boot += hours * 3600

    def test_the_boot_clock_is_read_and_the_monotonic_one_only_without_it(self):
        self.assertEqual(dcap.since_boot(), 1000.0)
        with mock.patch.object(dcap, "_BOOTTIME", None):
            self.assertEqual(dcap.since_boot(), 50.0)
        with mock.patch.object(time, "clock_gettime", side_effect=OSError):
            self.assertEqual(dcap.since_boot(), 50.0)

    def test_the_sealed_enclave_is_attested_again_after_a_sleep(self):
        verified = []
        enclave = sealed.Enclave(sealed.relay_url(API_URL),
                                 verifier=lambda relay, secret: verified.append(1) or 1)
        enclave.current()
        enclave.current()
        self.assertEqual(len(verified), 1)
        self.sleep(2)
        enclave.current()
        self.assertEqual(len(verified), 2)

    def test_intels_collateral_is_fetched_again_after_a_sleep(self):
        dcap._cache.clear()
        self.addCleanup(dcap._cache.clear)
        session = mock.Mock()
        session.get.return_value = FakeResponse(200, None, content=b"collateral")
        for _ in range(2):
            self.assertEqual(dcap._fetch(session, dcap.ROOT_CRL_URL)[0], b"collateral")
        self.assertEqual(session.get.call_count, 1)
        self.sleep(2)
        dcap._fetch(session, dcap.ROOT_CRL_URL)
        self.assertEqual(session.get.call_count, 2)

    def test_a_tee_attestation_is_made_again_after_a_sleep(self):
        server = FakeNanoGpt()
        session = session_for(server)
        session.ensure(TEXT)
        session.ensure(TEXT)
        self.sleep(1)
        session.ensure(TEXT)
        self.assertEqual(len([g for g in server.gets if "/tee/attestation" in g]), 2)

    def test_nvidias_keys_are_fetched_again_after_a_sleep(self):
        nras._keys.update(at=0.0, keys={})
        self.addCleanup(nras._keys.update, at=0.0, keys={})
        session = mock.Mock()
        session.get.return_value = FakeResponse(200, {"keys": [{"kid": "k1"}]})
        for _ in range(2):
            self.assertIn("k1", nras._jwks(session))
        self.assertEqual(session.get.call_count, 1)
        self.sleep(2)
        nras._jwks(session)
        self.assertEqual(session.get.call_count, 2)


# ---- the session ---------------------------------------------------------------------

class InstanceEvidence:
    """Chutes' per-instance attestation, built with a real key: each instance's
    certificate key is bound into its quote's report data, and signs a body carrying the
    nonce, the quote and the GPU evidence - the shape TEE/kimi-k3 answers with. The
    signed quote is under "quote", as live in Nov 2026; `quote_fields` names others
    (Sept 2026's "tdx_quote"), or none."""

    def __init__(self, count=2):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.keys, self.certs = [], []
        for i in range(count):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME,
                                                 "attestation-service")])
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(i + 1)
                    .not_valid_before(datetime(2026, 1, 1, tzinfo=timezone.utc))
                    .not_valid_after(datetime(2027, 1, 1, tzinfo=timezone.utc))
                    .sign(key, hashes.SHA256()))
            self.keys.append(key)
            self.certs.append(cert)
        self.gpu_nonce = "ab" * 32

    def body(self, nonce, sign_with=None, bind=True, signed_nonce=None, other_quote=False,
             failed=(), quote_fields=("quote",)):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        evidence = []
        for i, (key, cert) in enumerate(zip(self.keys, self.certs)):
            spki = cert.public_key().public_bytes(Encoding.DER,
                                                  PublicFormat.SubjectPublicKeyInfo)
            bound = hashlib.sha256(spki).digest() if bind else bytes(32)
            quote = bytes.fromhex(fake_quote(bytes.fromhex(self.gpu_nonce) + bound))
            signed_quote = bytes.fromhex(fake_quote(bytes(64))) if other_quote else quote
            gpu = [{"arch": "HOPPER", "evidence": "e", "certificate": "c"}]
            signed_evidence = {field: base64.b64encode(signed_quote).decode()
                               for field in quote_fields}
            signed_evidence["nvtrust_evidence"] = json.dumps(gpu)
            attested = json.dumps({"nonce": signed_nonce or nonce,
                                   "evidence": signed_evidence}).encode()
            signer = sign_with or key
            signature = signer.sign(attested, padding.PKCS1v15(), hashes.SHA256())
            evidence.append({
                "instance_id": f"inst-{i}", "tee_type": "tdx",
                "quote": base64.b64encode(quote).decode(),
                "gpu_evidence": gpu,
                "certificate": base64.b64encode(cert.public_bytes(Encoding.DER)).decode(),
                "signature": base64.b64encode(signature).decode(),
                "attested_body": base64.b64encode(attested).decode()})
        return {"evidence": evidence, "failed_instance_ids": list(failed)}


class ShapedNanoGpt(FakeNanoGpt):
    """nano-gpt answering /tee/attestation with a given body, or an error."""

    def __init__(self, respond, **kwargs):
        super().__init__(**kwargs)
        self.respond = respond
        self.nras_nonces = []

    def get(self, url, params=None, headers=None, timeout=None):
        if "/tee/attestation" in url:
            self.gets.append(url)
            status, body = self.respond(params["nonce"])
            return FakeResponse(status, body)
        return super().get(url, params, headers, timeout)

    def post(self, url, json=None, **kwargs):
        if url.startswith(privacy.NRAS_URL):
            self.nras_nonces.append(json.get("nonce"))
        return super().post(url, json=json, **kwargs)

    def nvidia_verify(self, answer, nonce, session):
        self.verified_nonces = getattr(self, "verified_nonces", []) + [nonce]
        return super().nvidia_verify(answer, nonce, session)


@unittest.skipUnless(_has_cryptography(), "the cryptography package is not installed")
class PerInstanceAttestation(unittest.TestCase):
    """TEE/kimi-k3 and the other Chutes models (live, Sept and Nov 2026)."""

    @classmethod
    def setUpClass(cls):
        cls.instances = InstanceEvidence()

    def attest(self, **shape):
        server = ShapedNanoGpt(lambda nonce: (200, self.instances.body(nonce, **shape)))
        client = server.tee_client(API_URL, "k", "TEE/kimi-k3")
        return client, server, client.attest()

    def test_every_instance_bound_and_signed_is_fully_attested(self):
        client, server, attestation = self.attest()
        self.assertEqual(attestation.level, "full")
        self.assertEqual(attestation.gpus, 16)                  # 8 per instance, faked
        self.assertIn("2 instances, each key bound to our nonce", attestation.summary)
        self.assertIn("signs no replies", attestation.detail)
        # NVIDIA's verdict is sought for the nonce the quote binds, as measured live.
        self.assertEqual(server.nras_nonces, [self.instances.gpu_nonce] * 2)
        self.assertEqual(server.verified_nonces, [self.instances.gpu_nonce] * 2)
        state, detail = client.check_reply("chatcmpl-1")
        self.assertEqual(state, privacy.UNSIGNED)
        self.assertIn("signs no replies", detail)

    def test_a_body_for_another_nonce_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "did not sign our nonce"):
            self.attest(signed_nonce="00" * 32)

    def test_a_signature_by_another_key_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "did not sign its evidence"):
            self.attest(sign_with=InstanceEvidence(1).keys[0])

    def test_a_key_the_quote_does_not_bind_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "quote does not bind"):
            self.attest(bind=False)

    def test_a_signed_body_for_another_quote_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "signed a different quote"):
            self.attest(other_quote=True)

    def test_the_quote_signed_under_its_earlier_name_still_counts(self):
        _client, _server, attestation = self.attest(quote_fields=("tdx_quote",))
        self.assertEqual(attestation.level, "full")

    def test_a_signed_body_with_no_quote_in_it_is_refused(self):
        with self.assertRaisesRegex(PrivacyError, "signed a different quote"):
            self.attest(quote_fields=())

    def test_any_instance_that_failed_to_attest_refuses_them_all(self):
        with self.assertRaisesRegex(PrivacyError, "1 of 3 .* could not be attested"):
            self.attest(failed=["inst-9"])


class AttestationShapesAndOutages(unittest.TestCase):
    def attest(self, status, body, model="TEE/x"):
        server = ShapedNanoGpt(lambda nonce: (status, body(nonce) if callable(body)
                                              else body))
        return server.tee_client(API_URL, "k", model).attest()

    def test_an_attestation_relayed_for_the_relays_own_nonce_is_refused(self):
        body = {"attestation_type": "chutes", "nonce": "f9" * 32,
                "all_attestations": [{"intel_quote": "", "gpu_evidence": []}]}
        with self.assertRaisesRegex(PrivacyError, "relayed by its provider \\(chutes\\), "
                                                  "was made for a nonce the relay chose"):
            self.attest(200, body)

    def test_nano_gpts_reason_is_given_and_tinfoil_models_point_to_their_twin(self):
        reason = ("Fresh nonce-bound attestation is not available for Tinfoil via this "
                  "endpoint.")
        with self.assertRaises(PrivacyError) as caught:
            self.attest(400, {"error": reason}, model="TEE/gemma4-31b")
        self.assertIn(reason, str(caught.exception))
        self.assertIn("private/ version", str(caught.exception))

    def test_a_provider_outage_says_so(self):
        with self.assertRaisesRegex(PrivacyError, "Unable to reach RedPill.*provider's "
                                                  "side.*tried again"):
            self.attest(502, {"error": "Unable to reach RedPill attestation service"})

    def test_a_provider_too_busy_to_attest_says_so(self):
        with self.assertRaisesRegex(PrivacyError, "HTTP 429: Unable to reach Chutes.*"
                                                  "provider's side.*tried again"):
            self.attest(429, {"error": "Unable to reach Chutes TEE evidence service"})

    def test_an_unknown_shape_names_what_it_held(self):
        with self.assertRaisesRegex(PrivacyError, "does not recognise .*holds novel"):
            self.attest(200, {"novel": 1})


class Session(TempConfig):
    def test_off_means_no_session(self):
        self.config.privacy_mode = False
        self.assertIsNone(PrivacySession.start(self.config))

    def test_on_with_an_ordinary_model_refuses_rather_than_falls_back(self):
        self.config.privacy_mode = True
        self.config.private_text_model = "anthropic/claude-sonnet-5"
        with self.assertRaisesRegex(PrivacyError, "not a TEE model.*Nothing was sent"):
            PrivacySession.start(self.config)

    def test_the_models_are_fixed_when_the_conversation_starts(self):
        self.config.privacy_mode = True
        session = PrivacySession.start(self.config)
        self.config.privacy_mode = False
        self.config.private_text_model = "TEE/something-else"
        self.assertEqual(session.text_model, TEXT)
        self.assertEqual(session.vision_model, VISION)
        self.assertFalse(session.allows("TEE/something-else"))

    def test_a_conversation_lists_and_sends_to_only_the_models_its_operation_uses(self):
        self.config.privacy_mode = True
        image = PrivacySession.start(self.config, uses=("vision",))
        self.assertEqual(image.models(), [VISION])
        self.assertFalse(image.allows(TEXT))
        self.assertNotIn(TEXT, image.details())
        self.assertIn(f"{VISION}  (vision)", image.details())
        with self.assertRaisesRegex(PrivacyError, "not one of this private conversation"):
            image.ensure(TEXT)
        translate = PrivacySession.start(self.config, uses=("text", "vision"))
        self.assertEqual(translate.models(), [TEXT, VISION])
        self.config.private_vision_model = TEXT
        both = PrivacySession.start(self.config)
        self.assertEqual(both.models(), [TEXT])
        self.assertEqual(both.role(TEXT), "text and vision")

    def test_the_caveats_are_for_the_models_the_conversation_uses(self):
        sealed_only = PrivacySession(API_URL, "k", "private/glm-5-3", "private/glm-5-3-flash",
                                     uses=("vision",))
        text = " ".join(sealed_only.caveats())
        self.assertIn("ciphertext", text)
        self.assertNotIn("TEE", text)
        self.assertNotIn("signed record", text)
        mixed = PrivacySession(API_URL, "k", TEXT, "private/glm-5-3-flash")
        self.assertEqual(len(mixed.caveats()), 2)
        self.assertIn("signed record", mixed.caveats()[1])

    def test_a_private_conversation_takes_only_tee_models(self):
        session = PrivacySession(API_URL, "k", TEXT, VISION, uses=("text",),
                                 premium_model="TEE/glm-5.3")
        self.assertEqual(session.models(), [TEXT])
        session.enlist("TEE/glm-5.3")
        self.assertEqual(session.role("TEE/glm-5.3"), "premium")
        session.enlist("TEE/kimi-k3")
        self.assertEqual(session.role("TEE/kimi-k3"), "chosen")
        self.assertTrue(session.allows("TEE/kimi-k3"))
        with self.assertRaisesRegex(PrivacyError, "not a TEE model"):
            session.enlist("openai/gpt-5")
        self.assertFalse(session.allows("openai/gpt-5"))

    def test_the_premium_option_answers_with_the_private_premium_model(self):
        self.config.privacy_mode = True
        self.config.private_premium_model = "TEE/glm-5.3"
        session = PrivacySession.start(self.config, uses=("premium",))
        self.assertEqual(session.models(), ["TEE/glm-5.3"])
        self.config.use_premium = True
        self.assertEqual(self.config.active_text_model(), "TEE/glm-5.3")
        self.config.private_premium_model = "openai/gpt-5"
        self.assertTrue(any("premium" in p for p in privacy.problems(self.config)))

    def test_a_tee_enclave_is_attested_once_and_events_say_so(self):
        server = FakeNanoGpt()
        session = session_for(server)
        events = []
        session.listener = lambda *event: events.append(event[:2])
        session.ensure(TEXT)
        session.ensure(TEXT)
        attestations = [g for g in server.gets if "/tee/attestation" in g]
        self.assertEqual(len(attestations), 1)
        self.assertEqual(events, [("attesting", TEXT), ("attested", TEXT)])

    def test_a_refusal_is_announced(self):
        session = session_for(FakeNanoGpt(nonce_bound=False))
        events = []
        session.listener = lambda *event: events.append(event[0])
        with self.assertRaisesRegex(PrivacyError, "nothing was sent"):
            session.ensure(TEXT)
        self.assertEqual(events, ["attesting", "refused"])

    def test_problems_catch_a_missing_client_for_private_models(self):
        self.config.private_text_model = "private/glm-5-3"
        found = privacy.problems(self.config)
        if sealed.available()[0]:
            self.assertEqual(found, [])
        else:
            self.assertTrue(any("install-private-mode" in f for f in found))
        self.config.api_url = "https://api.example.com/v1/chat/completions"
        self.assertTrue(any("not nano-gpt's" in f for f in privacy.problems(self.config)))

    def test_problems_name_the_cryptography_package_tee_models_need(self):
        self.assertEqual(privacy.problems(self.config), [])
        with mock.patch.object(privacy, "_cryptography", False):
            found = privacy.problems(self.config)
            self.assertEqual(found, [
                "The cryptography package (python3-cryptography) is not installed, and "
                f"without it {TEXT}, TEE/glm-5.3 and {VISION} cannot be attested: what a "
                "TEE model proves is Intel's and NVIDIA's signatures."])
            self.config.private_premium_model = TEXT        # named once, whatever its roles
            self.assertIn(f"{TEXT} and {VISION} cannot", privacy.problems(self.config)[0])
            self.config.privacy_mode = True
            with self.assertRaisesRegex(PrivacyError, "Privacy Mode is on, but the "
                                                      "cryptography package.*Nothing was "
                                                      "sent"):
                PrivacySession.start(self.config)
            # End-to-end encrypted models are sealed by Tinfoil's client, which brings
            # its own.
            for key in ("private_text_model", "private_premium_model",
                        "private_vision_model"):
                setattr(self.config, key, "private/glm-5-3")
            with mock.patch.object(sealed, "available", return_value=(True, "tinfoil")):
                self.assertEqual(privacy.problems(self.config), [])


class TeeAttestationsAge(TempConfig):
    """A TEE/ model's attestation was kept for the conversation's whole life, however
    long that ran: an enclave restarted, moved or replaced hours later was never asked
    again. Now one that is 15 minutes old is made again before the next send, and the
    window hears of it as it did of the first."""

    def session(self, server):
        self.now = 1000.0
        session = PrivacySession(API_URL, "k", TEXT, VISION, tee_factory=server.tee_client,
                                 clock=lambda: self.now)
        self.events = []
        session.listener = lambda event, model, detail: self.events.append(event)
        return session

    def attestations_asked(self, server):
        return len([g for g in server.gets if "/tee/attestation" in g])

    def test_one_15_minutes_old_is_made_again_before_the_next_send(self):
        server = FakeNanoGpt()
        session = self.session(server)
        first = session.ensure(TEXT)
        self.now += privacy.TEE_MAX_AGE_SECONDS - 1
        self.assertIs(session.ensure(TEXT), first)
        self.now += 1
        again = session.ensure(TEXT)
        self.assertIsNot(again, first)
        self.assertIs(session.attestations[TEXT], again)
        self.assertEqual(self.attestations_asked(server), 2)
        self.assertEqual(self.events, ["attesting", "attested"] * 2)

    def test_the_message_after_goes_only_once_it_is_attested_again(self):
        server = FakeNanoGpt()
        session = self.session(server)
        client = LLMClient(self.config)
        client.session = server
        client.stream(TEXT, [{"role": "user", "content": "hi"}], privacy=session)
        self.now += 20 * 60
        client.stream(TEXT, [{"role": "user", "content": "and then"}], privacy=session)
        self.assertEqual(self.events, ["attesting", "attested", "sent", "receipt"] * 2)
        self.assertEqual(len(server.chat_posts), 2)

    def test_one_that_fails_again_sends_nothing_and_is_tried_at_the_next_send(self):
        server = FakeNanoGpt()
        session = self.session(server)
        session.ensure(TEXT)
        self.now += 20 * 60
        server.nonce_bound = False
        with self.assertRaisesRegex(PrivacyError, "did not bind.*nothing was sent"):
            session.ensure(TEXT)
        self.assertEqual(self.events[2:], ["attesting", "refused"])
        server.nonce_bound = True
        session.ensure(TEXT)
        self.assertEqual(self.events[4:], ["attesting", "attested"])
        self.assertEqual(self.attestations_asked(server), 3)


class StrictAttestation(TempConfig):
    """config.privacy_strict. An enclave that proved less than it could - anything that
    shows the banner amber - is refused instead of used, and a reply whose signature
    does not hold has its model attested again before anything more is sent to it."""

    def session(self, server, strict=True):
        session = PrivacySession(API_URL, "k", TEXT, VISION, tee_factory=server.tee_client,
                                 strict=strict)
        self.events = []
        session.listener = lambda event, model, detail: self.events.append(event)
        return session

    def attestations_asked(self, server):
        return len([g for g in server.gets if "/tee/attestation" in g])

    def send(self, server, session, text="hi"):
        client = LLMClient(self.config)
        client.session = server
        return client.stream(TEXT, [{"role": "user", "content": text}], privacy=session)

    def test_it_is_read_when_the_conversation_starts_and_kept(self):
        self.config.privacy_mode = True
        self.assertFalse(PrivacySession.start(self.config).strict)
        self.config.privacy_strict = True
        session = PrivacySession.start(self.config)
        self.config.privacy_strict = False
        self.assertTrue(session.strict)

    def test_an_attestation_that_fell_short_is_refused_naming_what_it_lacked(self):
        for server, lacked in (
                (FakeNanoGpt(evidence=False), "no GPU evidence"),
                (FakeNanoGpt(intel=dcap.Result(status="OutOfDate", platform="OutOfDate",
                                               qe="UpToDate")), "Intel TCB OutOfDate"),
                (FakeNanoGpt(intel=dcap.Result(collateral=False)), "Intel TCB unchecked"),
                (FakeNanoGpt(nras_status=503), "GPUs unverified"),
                (FakeNanoGpt(nvidia=nras.KeysUnavailable("NVIDIA's keys are out of reach")),
                 "NVIDIA signature unchecked")):
            session = self.session(server)
            with self.assertRaisesRegex(PrivacyError, f"proved less than it could "
                                                      f"\\({lacked}\\), and strict "
                                                      f"attestation is on; nothing was "
                                                      f"sent"):
                session.ensure(TEXT)
            self.assertEqual(self.events, ["attesting", "refused"])
            self.assertNotIn(TEXT, session.attestations)
            # Not strict, the same enclave is used, with its shortfall in the headline.
            self.assertIn(lacked, self.session(server, strict=False).ensure(TEXT).headline)

    def test_nothing_is_sent_to_an_enclave_that_fell_short(self):
        server = FakeNanoGpt(evidence=False)
        with self.assertRaisesRegex(LLMError, "no GPU evidence.*strict attestation is on; "
                                              "nothing was sent"):
            self.send(server, self.session(server), "secret")
        self.assertEqual(server.chat_posts, [])

    def test_one_that_proved_everything_is_used_as_ever(self):
        server = FakeNanoGpt()
        self.assertEqual(self.send(server, self.session(server)), "Hello there")

    def test_an_enclave_on_an_older_release_is_refused(self):
        class OlderRelease:
            def check_offered(self, model):
                pass

            def attest(self, model):
                return privacy.Attestation(model=model, kind=privacy.E2EE,
                                           release="v0.0.150", latest_release="v0.0.155")

        model = "private/glm-5-3"
        session = PrivacySession(API_URL, "k", model, model, strict=True,
                                 sealed_factory=lambda *a: OlderRelease())
        with self.assertRaisesRegex(PrivacyError, r"proved less than it could \(an older "
                                                  r"release than v0\.0\.155\)"):
            session.ensure(model)
        lenient = PrivacySession(API_URL, "k", model, model,
                                 sealed_factory=lambda *a: OlderRelease())
        self.assertEqual(lenient.ensure(model).latest_release, "v0.0.155")

    def test_after_a_signature_mismatch_the_next_send_attests_again(self):
        server = FakeNanoGpt(signer=1)              # a key the attestation did not name
        session = self.session(server)
        self.send(server, session)
        self.assertEqual(session.pop_receipt().state, privacy.MISMATCH)
        server.signer = KEY                         # and then the attested one again
        self.assertEqual(self.send(server, session, "and then"), "Hello there")
        self.assertEqual(self.events, ["attesting", "attested", "sent", "receipt"] * 2)
        self.assertEqual(session.pop_receipt().state, privacy.SIGNED)
        self.send(server, session, "and on")        # signed: nothing to attest again
        self.assertEqual(self.events[8:], ["sent", "receipt"])
        self.assertEqual(self.attestations_asked(server), 2)
        self.assertEqual(len(server.chat_posts), 3)

    def test_after_a_reply_left_unsigned_the_next_send_attests_again(self):
        server = FakeNanoGpt(signature_status=500)
        session = self.session(server)
        session.ensure(TEXT)
        self.assertEqual(session.check_reply(TEXT, "chatcmpl-1").state, privacy.UNSIGNED)
        session.ensure(TEXT)
        self.assertEqual(self.attestations_asked(server), 2)

    def test_a_provider_that_signs_no_replies_is_not_attested_again_for_that(self):
        class SignsNothing:
            """Chutes' shape: every instance attested, no key named to sign replies."""

            def __init__(self):
                self.attested = 0

            def attest(self):
                self.attested += 1
                return privacy.Attestation(model="TEE/kimi-k3", kind=privacy.TEE)

            def check_reply(self, response_id):
                return privacy.UNSIGNED, "this model's provider signs no replies"

        provider = SignsNothing()
        session = PrivacySession(API_URL, "k", "TEE/kimi-k3", VISION, uses=("text",),
                                 tee_factory=lambda *args: provider, strict=True)
        session.ensure("TEE/kimi-k3")
        session.check_reply("TEE/kimi-k3", "chatcmpl-1")
        session.ensure("TEE/kimi-k3")
        self.assertEqual(provider.attested, 1)

    def test_without_it_a_mismatch_is_shown_and_nothing_attested_again(self):
        server = FakeNanoGpt(signer=1)
        session = self.session(server, strict=False)
        session.ensure(TEXT)
        self.assertEqual(session.check_reply(TEXT, "chatcmpl-1").state, privacy.MISMATCH)
        session.ensure(TEXT)
        self.assertEqual(self.attestations_asked(server), 1)


class TheMainLoopNeverWaitsForAnAttestation(unittest.TestCase):
    """ensure() held the session's lock across the network - several requests, each
    allowed 30 seconds - and enlist(), which the chat window calls on the main loop when
    a model is picked from its menu or Send is pressed, took the same lock: the whole
    app froze while a model was attested. Now only what needs an attestation's answer
    waits for it, and outside the lock."""

    def session(self, refuse=False):
        """A session whose TEXT attestation, once entered, waits for self.release;
        refuse=True makes its attestations fail."""
        server = FakeNanoGpt(nonce_bound=not refuse)
        self.entered, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        self.attested = []
        test = self

        class Slow(TeeClient):
            def attest(this):
                test.attested.append(this.model)
                if this.model == TEXT:
                    test.entered.set()
                    test.release.wait(10)
                return super().attest()

        def factory(url, key, model):
            return Slow(url, key, model, session=server, intel=server.intel_verify,
                        nvidia=server.nvidia_verify)

        session = PrivacySession(API_URL, "k", TEXT, VISION, tee_factory=factory)
        self.events = []
        session.listener = lambda event, model, detail: self.events.append((event, model))
        return session

    def in_thread(self, work):
        """work() on a thread of its own: (thread, outcome), the outcome holding its
        "result" or its "error" once it is done."""
        outcome = {}

        def run():
            try:
                outcome["result"] = work()
            except Exception as e:
                outcome["error"] = e

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.release.set(), thread.join(10)))
        return thread, outcome

    def test_enlist_answers_at_once_while_a_model_is_attested(self):
        session = self.session()
        attesting, _ = self.in_thread(lambda: session.ensure(TEXT))
        self.assertTrue(self.entered.wait(5))
        enlisting, outcome = self.in_thread(
            lambda: (session.enlist(TEXT), session.enlist("TEE/kimi-k3")))
        enlisting.join(1)
        self.assertFalse(enlisting.is_alive(), "enlist() waited for the attestation")
        self.assertNotIn("error", outcome)
        self.assertTrue(session.allows("TEE/kimi-k3"))
        self.assertTrue(attesting.is_alive(), "the attestation was over too soon to tell")
        self.release.set()
        attesting.join(5)
        self.assertEqual(self.events, [("attesting", TEXT), ("attested", TEXT)])

    def test_another_model_is_attested_meanwhile(self):
        session = self.session()
        attesting, _ = self.in_thread(lambda: session.ensure(TEXT))
        self.assertTrue(self.entered.wait(5))
        other, outcome = self.in_thread(lambda: session.ensure(VISION))
        other.join(2)
        self.assertFalse(other.is_alive(), "one model's attestation waited for another's")
        self.assertEqual(outcome["result"].model, VISION)
        self.assertTrue(attesting.is_alive())

    def test_a_model_asked_for_twice_at_once_is_attested_once(self):
        session = self.session()
        first, one = self.in_thread(lambda: session.ensure(TEXT))
        self.assertTrue(self.entered.wait(5))
        second, two = self.in_thread(lambda: session.ensure(TEXT))
        second.join(0.2)
        self.assertTrue(second.is_alive(), "the second ask did not wait for the first")
        self.release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(self.attested, [TEXT])
        self.assertIs(one["result"], two["result"])
        self.assertEqual(self.events, [("attesting", TEXT), ("attested", TEXT)])

    def test_a_refusal_is_given_to_every_thread_that_waited_for_it(self):
        session = self.session(refuse=True)
        first, one = self.in_thread(lambda: session.ensure(TEXT))
        self.assertTrue(self.entered.wait(5))
        second, two = self.in_thread(lambda: session.ensure(TEXT))
        second.join(0.2)
        self.release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(self.attested, [TEXT])
        for outcome in (one, two):
            self.assertIsInstance(outcome.get("error"), PrivacyError)
            self.assertIn("did not bind", str(outcome["error"]))
            self.assertIn("nothing was sent", str(outcome["error"]))
        self.assertEqual(self.events, [("attesting", TEXT), ("refused", TEXT)])
        self.assertNotIn(TEXT, session.attestations)


# ---- the client ----------------------------------------------------------------------

class PrivateRequests(TempConfig):
    def client(self, server):
        client = LLMClient(self.config)
        client.session = server
        return client

    def test_a_tee_reply_is_attested_first_then_checked_for_its_signature(self):
        server = FakeNanoGpt()
        session = session_for(server)
        order = []
        session.listener = lambda event, model, detail: order.append(event)
        text = self.client(server).stream(TEXT, [{"role": "user", "content": "hi"}],
                                          privacy=session)
        self.assertEqual(text, "Hello there")
        self.assertEqual(order, ["attesting", "attested", "sent", "receipt"])
        receipt = session.pop_receipt()
        self.assertEqual(receipt.state, privacy.SIGNED)
        self.assertTrue(any("/tee/signature/chatcmpl-42" in g for g in server.gets))
        self.assertEqual(privacy.receipt_badge(receipt)[1], "ok")

    def test_a_tee_request_goes_where_the_session_attested(self):
        """An open private conversation attested at nano-gpt.com, and posted to wherever
        api_url said at the time: a hand edit, taken up by the next save."""
        server = FakeNanoGpt()
        session = session_for(server)
        self.config.api_url = "https://llm-gateway.example.net/v1/chat/completions"
        text = self.client(server).stream(TEXT, [{"role": "user", "content": "private"}],
                                          privacy=session)
        self.assertEqual(text, "Hello there")
        self.assertEqual(len(server.chat_posts), 1)
        self.assertFalse(any("llm-gateway" in post["url"] for post in server.posts))

    def test_nothing_is_sent_when_the_enclave_is_not_attested(self):
        server = FakeNanoGpt(nonce_bound=False)
        with self.assertRaisesRegex(LLMError, "nothing was sent"):
            self.client(server).stream(TEXT, [{"role": "user", "content": "secret"}],
                                       privacy=session_for(server))
        self.assertEqual(server.chat_posts, [])

    def test_nothing_is_sent_without_the_cryptography_package(self):
        server = FakeNanoGpt(intel=ImportError("cryptography"))
        session = session_for(server)
        events = []
        session.listener = lambda event, model, detail: events.append(event)
        with self.assertRaisesRegex(LLMError, "python3-cryptography.*nothing was sent"):
            self.client(server).stream(TEXT, [{"role": "user", "content": "secret"}],
                                       privacy=session)
        self.assertEqual(server.chat_posts, [])
        self.assertEqual(events, ["attesting", "refused"])

    def test_nothing_is_sent_to_a_model_outside_the_conversation(self):
        server = FakeNanoGpt()
        for model in ("anthropic/claude-sonnet-5", "TEE/some-other-model"):
            with self.assertRaisesRegex(LLMError, "nothing was sent"):
                self.client(server).stream(model, [{"role": "user", "content": "x"}],
                                           privacy=session_for(server))
        self.assertEqual(server.posts, [])

    def test_a_private_request_never_searches_the_web(self):
        self.config.web_search_enabled = True
        server = FakeNanoGpt()
        client = self.client(server)
        client.search = lambda *a, **k: self.fail("searched in Privacy Mode")
        client.craft_query = lambda *a, **k: self.fail("crafted a query in Privacy Mode")
        client.stream(TEXT, [{"role": "user", "content": "x"}], web_search=True,
                      privacy=session_for(server))

    def test_ocr_goes_through_the_same_checks(self):
        server = FakeNanoGpt(reply="NO TEXT FOUND")
        session = session_for(server)
        text = self.client(server).complete(VISION, [{"role": "user", "content": "ocr"}],
                                            image_base64="AAAA", privacy=session)
        self.assertEqual(text, "NO TEXT FOUND")
        self.assertIn(VISION, session.attestations)
        self.assertEqual(session.pop_receipt().model, VISION)

    def test_the_connection_test_uses_the_private_model_when_privacy_is_on(self):
        self.config.privacy_mode = True
        server = FakeNanoGpt(reply="ok")
        client = self.client(server)
        original = PrivacySession.__init__

        def with_fake_tee(this, *args, **kwargs):
            kwargs["tee_factory"] = server.tee_client
            original(this, *args, **kwargs)

        PrivacySession.__init__ = with_fake_tee
        try:
            ok, message = client.test_connection()
        finally:
            PrivacySession.__init__ = original
        self.assertTrue(ok, message)
        self.assertIn("TEE attested", message)
        self.assertIn("signed", message)
        self.assertEqual(json.loads(server.chat_posts[0]["data"])["model"], TEXT)


# ---- the catalogue -------------------------------------------------------------------

class Catalogue(unittest.TestCase):
    def test_private_models_are_priced_and_described_from_their_tee_twins(self):
        entries = [{"id": "TEE/gemma4-31b", "name": "Gemma 4 31B",
                    "capabilities": {"vision": True}, "context_length": 262144,
                    "pricing": {"prompt": 0.4, "completion": "1.0"}},
                   {"id": "openai/gpt-5", "pricing": {"prompt": 1}}]
        made = models.private_entries(["private/gemma4-31b", "private/unknown"], entries)
        twin = models.normalise(made[0])
        self.assertEqual(twin["id"], "private/gemma4-31b")
        self.assertTrue(twin["vision"])
        self.assertAlmostEqual(twin["prompt_price"], 0.42)
        self.assertAlmostEqual(twin["completion_price"], 1.05)
        self.assertIn("end-to-end encrypted", twin["name"])
        self.assertEqual(entries[0]["pricing"]["prompt"], 0.4, "the twin was changed")
        bare = models.normalise(made[1])
        self.assertIsNone(bare["prompt_price"])

    def test_a_model_off_the_relays_list_is_known_to_be_retired(self):
        listed = ["private/glm-5-3", "private/gemma4-31b:thinking"]
        self.assertIs(models.offered("private/glm-5-3", listed), True)
        self.assertIs(models.offered("private/glm-5.3", listed), True, "its dotted alias")
        self.assertIs(models.offered("private/glm-5-3-flash", listed), False)
        self.assertIs(models.offered("private/gemma4-31b", listed), False,
                      ":thinking is a model of its own")
        self.assertIsNone(models.offered("private/glm-5-3-flash", []),
                          "a list that could not be read says nothing")
        self.assertEqual(models.private_list_url(API_URL),
                         "https://nano-gpt.com/api/v1/private/tinfoil/models")

    def test_search_can_keep_to_tee_models(self):
        listing = [models.normalise(e) for e in (
            {"id": "TEE/a"}, {"id": "private/b"}, {"id": "openai/c"})]
        found = [m["id"] for m in models.search(listing, "", private_only=True)]
        self.assertEqual(found, ["TEE/a", "private/b"])


# ---- what is shown -------------------------------------------------------------------

class Badges(unittest.TestCase):
    def test_a_badge_is_escaped_and_marks_the_turn(self):
        html = turn_html("assistant", "hi", privacy={"label": "<b>x</b>", "tone": "ok",
                                                     "tip": '"q"'})
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertIn('class="privacy ok"', html)
        self.assertIn("turn assistant private", html)
        self.assertNotIn("<b>x</b>", html)

    def test_receipts_read_as_what_they_prove(self):
        def badge(state):
            return privacy.receipt_badge(privacy.Receipt(TEXT, privacy.TEE, state))
        self.assertIn("signed", badge(privacy.SIGNED)[0])
        self.assertEqual(badge(privacy.MISMATCH)[1], "bad")
        self.assertEqual(badge(privacy.UNSIGNED)[1], "warn")
        self.assertIn("End-to-end", privacy.receipt_badge(
            privacy.Receipt("private/x", privacy.E2EE, privacy.SEALED))[0])
        self.assertIn("Sealed", privacy.sent_badge({privacy.E2EE})[0])
        self.assertIn("TEE", privacy.sent_badge({privacy.E2EE, privacy.TEE})[0])


# ---- the chat window -----------------------------------------------------------------

class RecordingClient:
    """Streams like LLMClient, and fails the test if a private conversation's request
    arrives without its session or for another model."""

    def __init__(self):
        self.calls = []

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None, privacy=None, status=None):
        self.calls.append({"model": model, "web_search": web_search, "privacy": privacy})
        if privacy is not None:
            privacy.note_sent(model)
            privacy.check_reply(model, "chatcmpl-1")
        if on_delta:
            on_delta("ok", "ok")
        return "ok"


class StubApp:
    def __init__(self, config, client):
        self.config = config
        self.client = client

    def notify(self, message, urgency="low"):
        pass


class ChatStaysPrivate(TempConfig):
    @classmethod
    def setUpClass(cls):
        from gi.repository import Gtk
        if not Gtk.init_check(sys.argv)[0]:
            raise unittest.SkipTest("no display")

    def _run_until(self, predicate, timeout=15):
        from gi.repository import GLib, Gtk
        state = {"expired": False}

        def expire():
            state["expired"] = True
            Gtk.main_quit()
            return False

        source = GLib.timeout_add_seconds(timeout, expire)

        def poll():
            if predicate():
                Gtk.main_quit()
                return False
            return True

        GLib.timeout_add(50, poll)
        Gtk.main()
        if not state["expired"]:
            GLib.source_remove(source)
        return not state["expired"]

    def test_switching_privacy_off_does_not_take_a_conversation_out_of_it(self):
        from assistant.ui.chat import ChatWindow
        self.config.privacy_mode = True
        self.config.web_search_enabled = True
        self.config.use_premium = True
        server = FakeNanoGpt()
        session = session_for(server)
        session.ensure(TEXT)                 # so the receipt can be checked offline
        client = RecordingClient()
        window = ChatWindow(StubApp(self.config, client), "Query text",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            autostart=True, web_search=True, privacy=session)
        try:
            self.assertTrue(self._run_until(lambda: window._cancel is None
                                            and len(window.conversation) >= 2))
            # Privacy Mode off, ordinary models chosen, premium and search on: none of
            # it may reach this conversation.
            self.config.privacy_mode = False
            window.entry.get_buffer().set_text("and a follow-up")
            window._send()
            self.assertTrue(self._run_until(lambda: window._cancel is None
                                            and len(window.conversation) >= 4))
            for call in client.calls:
                self.assertEqual(call["model"], TEXT)
                self.assertIs(call["privacy"], session)
                self.assertFalse(call["web_search"])
            self.assertTrue(all(privacy.kind_of(model)
                                for _key, _label, model in window._choices()),
                            "the model menu offers a model outside the enclaves")
            window._chosen.append("anthropic/claude-sonnet-5")
            self.assertFalse(window._choose("model:anthropic/claude-sonnet-5"),
                             "a private conversation took a non-TEE model")
            self.assertEqual(window.current_model, TEXT)
            self.assertIsNone(window.search_toggle.get_parent(),
                              "web search is offered in a private conversation")
            self.assertIn("Privacy Mode", window.header.get_subtitle())
        finally:
            window.destroy()

    def test_every_turn_carries_what_was_proved(self):
        from assistant.ui.chat import ChatWindow
        server = FakeNanoGpt()
        session = session_for(server)
        session.ensure(TEXT)
        window = ChatWindow(StubApp(self.config, RecordingClient()), "Query text",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            autostart=True, privacy=session)
        try:
            self.assertTrue(self._run_until(lambda: window._cancel is None
                                            and len(window.conversation) >= 2))
            question, answer = window.conversation[:2]
            self.assertIn("Sent to attested TEE", question["privacy"]["label"])
            self.assertIn("signed by attested enclave", answer["privacy"]["label"])
            self.assertIn("TEE attested", window._privacy_title.get_text())
            self.assertTrue(window.privacy_banner.get_style_context().has_class("ok"))
        finally:
            window.destroy()

    def test_an_enclave_that_fell_short_turns_the_banner_amber_and_says_how(self):
        """A private/ enclave whose release could not be checked against Tinfoil's
        latest proved less than it could - Strict refuses it - and its banner was green
        all the same, naming nothing: it went by the attestation's level and a newer
        release named, not by what fell short."""
        from unittest import mock
        from assistant.ui.chat import ChatWindow
        model = "private/glm-5-3"

        class Unchecked:
            def check_offered(self, model):
                pass

            def attest(self, model):
                return privacy.Attestation(model=model, kind=privacy.E2EE,
                                           enclave="router-0.tinfoil.sh",
                                           hardware="AMD SEV-SNP", release="v0.0.155",
                                           release_checked=False)

        patch = mock.patch.object(sealed, "available", return_value=(True, ""))
        patch.start()
        self.addCleanup(patch.stop)
        session = PrivacySession(API_URL, "k", model, model, uses=("text",),
                                 sealed_factory=lambda *a: Unchecked())
        session.ensure(model)
        window = ChatWindow(StubApp(self.config, RecordingClient()), "Query text",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            privacy=session)
        try:
            context = window.privacy_banner.get_style_context()
            self.assertTrue(context.has_class("warn"))
            self.assertFalse(context.has_class("ok"))
            self.assertIn("its release not checked against the latest",
                          window._privacy_detail.get_text())
        finally:
            window.destroy()

    def test_a_failure_names_the_model_and_its_part(self):
        from assistant.ui.chat import ChatWindow

        def read(cancel, status, **_request):
            raise LLMError("HTTP 503: model temporarily overloaded", model="ocr/model")

        class Answering(RecordingClient):
            def stream(self, *args, **kwargs):
                raise LLMError("HTTP 503: model temporarily overloaded",
                               model="good/text-model")

        self.config.text_model = "good/text-model"
        self.config.use_premium = False
        for preflight, client, expected in (
                (read, RecordingClient(),
                 "The vision model reading the image (ocr/model) failed: HTTP 503"),
                (None, Answering(),
                 "The text model (good/text-model) failed: HTTP 503")):
            window = ChatWindow(StubApp(self.config, client), "Translate image",
                                [{"role": "user", "content": "p", "display": "Q"}],
                                autostart=True, preflight=preflight)
            try:
                self.assertTrue(self._run_until(lambda: window._cancel is None
                                                and len(window.conversation) >= 2))
                self.assertIn(expected, window.conversation[-1]["content"])
            finally:
                window.destroy()

    def test_a_refused_attestation_turns_the_banner_red_and_sends_nothing(self):
        from assistant.ui.chat import ChatWindow
        server = FakeNanoGpt(nonce_bound=False)
        client = LLMClient(self.config)
        client.session = server
        window = ChatWindow(StubApp(self.config, client), "Query text",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            autostart=True, privacy=session_for(server))
        try:
            self.assertTrue(self._run_until(lambda: window._cancel is None
                                            and len(window.conversation) >= 2))
            self.assertTrue(window.conversation[-1].get("error"))
            self.assertIn("nothing was sent", window.conversation[-1]["content"])
            self.assertTrue(window.privacy_banner.get_style_context().has_class("bad"))
            self.assertEqual(window.conversation[0]["privacy"]["tone"], "warn")
            self.assertEqual(server.chat_posts, [])
        finally:
            window.destroy()


class RetiredClient(RecordingClient):
    """A model nano-gpt has retired fails as LLMClient's would, having told the
    session; any other answers."""

    RETIRED = "private/glm-5-3-flash"
    OFFERED = ["private/glm-5-3", "private/kimi-k3"]

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None, privacy=None, status=None):
        if model == self.RETIRED:
            self.calls.append({"model": model})
            retired = ModelRetired(model, self.OFFERED + [model])
            privacy.note_retired(retired)
            raise LLMError(str(retired), model=model)
        return super().stream(model, messages, image_base64, cancel, on_delta,
                              web_search, system, privacy, status)


class ARetiredModelInAChat(TempConfig):
    setUpClass = ChatStaysPrivate.setUpClass
    _run_until = ChatStaysPrivate._run_until

    def window(self):
        from unittest import mock
        from assistant.ui.chat import ChatWindow
        patch = mock.patch.object(sealed, "available", return_value=(True, ""))
        patch.start()
        self.addCleanup(patch.stop)
        session = PrivacySession(API_URL, "k", RetiredClient.RETIRED, VISION,
                                 uses=("text",))
        self.client = RetiredClient()
        window = ChatWindow(StubApp(self.config, self.client), "Query text",
                            [{"role": "user", "content": "p", "display": "Q"}],
                            privacy=session)
        self.addCleanup(window.destroy)
        return window

    def answer(self, window, response):
        """Start the request; the dialog that follows answers `response` with the
        first model it offers chosen. What it offered, or None if it never showed."""
        from unittest import mock
        from gi.repository import Gtk
        shown = {}

        def run(dialog):
            for child in dialog.get_message_area().get_children():
                if isinstance(child, Gtk.ComboBoxText):
                    shown["picker"] = child
                    shown["offered"] = [row[0] for row in child.get_model()]
                elif isinstance(child, Gtk.Label) and child.get_text():
                    shown.setdefault("text", []).append(child.get_text())
            return response

        with mock.patch.object(Gtk.Dialog, "run", run):
            window._start_stream()
            self.assertTrue(self._run_until(lambda: "text" in shown
                                            or window._cancel is None and len(
                                                window.conversation) >= 2))
            if "text" in shown:
                self.assertTrue(self._run_until(
                    lambda: window._cancel is None
                    and not window.conversation[-1].get("error")
                    or response != Gtk.ResponseType.ACCEPT))
        return shown

    def test_it_is_said_and_another_model_carries_the_conversation_on(self):
        from gi.repository import Gtk
        window = self.window()
        shown = self.answer(window, Gtk.ResponseType.ACCEPT)
        self.assertEqual(shown["offered"], RetiredClient.OFFERED)
        self.assertIn("no longer offered", " ".join(shown["text"]))
        self.assertIn("Settings → Privacy", " ".join(shown["text"]))
        self.assertEqual([c["model"] for c in self.client.calls],
                         [RetiredClient.RETIRED, "private/glm-5-3"])
        self.assertEqual(window.current_model, "private/glm-5-3")
        self.assertEqual(window.conversation[-1]["content"], "ok")
        self.assertTrue(window.privacy.allows("private/glm-5-3"))

    def test_closing_it_leaves_the_model_marked_and_sends_nothing_more(self):
        from gi.repository import Gtk
        window = self.window()
        self.answer(window, Gtk.ResponseType.CLOSE)
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("no longer offered", window._model_label.get_text())
        self.assertIn("nothing was sent", window.conversation[-1]["content"])
        self.assertEqual(window._privacy_title.get_text(),
                         "No longer offered — nothing was sent")
        self.assertIn("nothing was sent", window._privacy_detail.get_text())


class TheClientIsLookedForOffTheMainLoop(TempConfig):
    """Whether Tinfoil's client can be used is found out by importing it: some 1,200
    modules, most of a second, the first time in each run. Settings used to do that as
    it was built, with every window frozen. It is still the real import - a client that
    is installed and will not load is found in Settings, not in the middle of a
    conversation - made on a thread of its own, and Settings filled in when it is done.
    """

    setUpClass = ChatStaysPrivate.setUpClass
    _run_until = ChatStaysPrivate._run_until

    def sdk(self, answer, seconds=0.3):
        """Stand in for the import: `seconds` long, and noting which thread made it."""
        import threading
        import time
        from unittest import mock
        threads = []

        def slow():
            threads.append(threading.current_thread())
            time.sleep(seconds)
            return answer

        for patch in (mock.patch.object(sealed, "_known", None),
                      mock.patch.object(sealed, "_import_sdk", slow)):
            patch.start()
            self.addCleanup(patch.stop)
        return threads

    def settings(self):
        from assistant.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(None, self.config, type("C", (), {"config": self.config}))
        self.addCleanup(dialog.destroy)
        return dialog

    def test_known_never_imports_and_check_does_on_its_own_thread(self):
        import threading
        threads = self.sdk((True, "tinfoil 9"))
        self.assertIsNone(sealed.known())
        self.assertEqual(threads, [])
        heard = []
        done = threading.Event()
        sealed.check(lambda answer: (heard.append(answer), done.set()))
        self.assertIsNone(sealed.known())           # not waited for
        self.assertTrue(done.wait(5))
        self.assertEqual(heard, [(True, "tinfoil 9")])
        self.assertEqual(sealed.known(), (True, "tinfoil 9"))
        self.assertIsNot(threads[0], threading.main_thread())
        self.assertEqual(sealed.available(), (True, "tinfoil 9"))
        self.assertEqual(len(threads), 1)           # worked out once

    def test_settings_opens_without_waiting_and_fills_the_line_in(self):
        import threading
        import time
        threads = self.sdk((True, "tinfoil 9"), seconds=0.5)
        started = time.monotonic()
        dialog = self.settings()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("checking", dialog.e2ee_status.get_text())
        self.assertTrue(self._run_until(
            lambda: "checking" not in dialog.e2ee_status.get_text()))
        self.assertIn("available (tinfoil 9", dialog.e2ee_status.get_text())
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.main_thread())

    def test_a_client_that_will_not_load_is_found_in_settings(self):
        self.config.privacy_mode = True
        self.config.private_text_model = "private/glm-5-3"
        self.sdk((False, sealed.INSTALL_HINT))
        dialog = self.settings()
        # Not known yet: nothing is claimed either way, and Save waits to hear.
        self.assertFalse(dialog.privacy_problems.get_visible())
        self.assertFalse(dialog.save_button.get_sensitive())
        self.assertTrue(self._run_until(
            lambda: "checking" not in dialog.e2ee_status.get_text()))
        self.assertIn("install-private-mode", dialog.e2ee_status.get_text())
        self.assertTrue(dialog.privacy_problems.get_visible())
        self.assertIn("install-private-mode", dialog.privacy_problems.get_text())
        self.assertFalse(dialog.save_button.get_sensitive())

    def test_save_is_free_again_once_the_client_is_known_to_be_there(self):
        self.config.privacy_mode = True
        self.config.private_text_model = "private/glm-5-3"
        self.sdk((True, "tinfoil 9"))
        dialog = self.settings()
        self.assertFalse(dialog.save_button.get_sensitive())
        self.assertTrue(self._run_until(dialog.save_button.get_sensitive))
        self.assertFalse(dialog.privacy_problems.get_visible())

    def test_settings_closed_before_the_answer_is_left_alone(self):
        self.sdk((True, "tinfoil 9"))
        dialog = self.settings()
        dialog.destroy()
        self.assertTrue(self._run_until(lambda: sealed.known() is not None))
        self._run_until(lambda: False, timeout=1)    # the answer arrives at nothing

    def test_problems_can_be_asked_without_waiting(self):
        self.config.private_text_model = "private/glm-5-3"
        threads = self.sdk((False, sealed.INSTALL_HINT))
        self.assertEqual(privacy.problems(self.config, wait=False), [])
        self.assertEqual(threads, [])
        found = privacy.problems(self.config)        # as the tray and --probe ask
        self.assertTrue(any("install-private-mode" in f for f in found))
        self.assertEqual(privacy.problems(self.config, wait=False), found)

    def test_the_model_browser_does_not_wait_either(self):
        import threading
        from assistant.ui.model_browser import ModelBrowser
        from unittest import mock
        threads = self.sdk((False, sealed.INSTALL_HINT))
        with mock.patch.object(ModelBrowser, "_load", lambda *a, **kw: None):   # no network
            browser = ModelBrowser(None, self.config, private_only=True)
        self.addCleanup(browser.destroy)
        shown = []
        browser.detail.set_text = lambda text: shown.append(text)
        browser.models = [{"id": "private/glm-5-3", "description": "A model.",
                           "aliases": []}]
        browser._current_id = lambda: "private/glm-5-3"
        browser._on_selection_changed(None)
        self.assertNotIn("install-private-mode", shown[-1])
        self.assertTrue(self._run_until(
            lambda: "install-private-mode" in shown[-1]))
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.main_thread())


class AppUpgradesButNeverDowngrades(TempConfig):
    def app(self):
        from assistant.app import Assistant

        class Stub:
            _start_privacy = Assistant._start_privacy
            _confirm_privacy = Assistant._confirm_privacy

            def __init__(stub):
                stub.config = self.config
                stub.notices = []

            def notify(stub, message, urgency="low"):
                stub.notices.append(message)
        return Stub()

    def test_a_dialog_opened_in_privacy_mode_sends_privately_after_it_is_switched_off(self):
        app = self.app()
        self.config.privacy_mode = True
        started = app._start_privacy()
        self.config.privacy_mode = False
        self.assertIs(app._confirm_privacy(started), started)

    def test_a_dialog_opened_outside_it_is_upgraded_if_it_is_switched_on(self):
        app = self.app()
        started = app._start_privacy()
        self.assertIsNone(started)
        self.config.privacy_mode = True
        self.assertIsInstance(app._confirm_privacy(started), PrivacySession)

    def test_an_image_operation_is_kept_to_the_vision_model(self):
        app = self.app()
        self.config.privacy_mode = True
        self.assertEqual(app._confirm_privacy(None, ("vision",)).models(), [VISION])
        self.assertEqual(app._start_privacy(("text",)).models(), [TEXT])

    def test_unusable_settings_stop_the_operation_and_say_why(self):
        app = self.app()
        self.config.privacy_mode = True
        self.config.private_vision_model = "qwen/qwen3-vl"
        self.assertIs(app._start_privacy(), False)
        self.assertIn("not a TEE model", app.notices[-1])


# ---- end-to-end encrypted ------------------------------------------------------------

@unittest.skipUnless(sealed.available()[0], "Tinfoil's client is not installed "
                                            "(tools/install-private-mode.sh)")
class Sealed(unittest.TestCase):
    MODEL = "private/glm-5-3"

    def verified(self, key="ab" * 32):
        return sealed.Verified(hpke_public_key=key, enclave="router-0.tinfoil.sh",
                               hardware="AMD SEV-SNP", measurement="m", release_digest="d" * 64,
                               release_tag="v0.0.155", latest_release="", latest_checked=True)

    def make(self, handler, verifier=None):
        import httpx
        calls = {"verify": 0}

        def verify(relay, secret):
            calls["verify"] += 1
            return (verifier or (lambda: self.verified()))()

        enclave = sealed.Enclave(sealed.relay_url(API_URL), verifier=verify)

        def preflight(request):
            return httpx.Response(200, json={"cacheScope": "c" * 64,
                                             "upstreamModel": "glm-5.3"})

        client = sealed.PrivateModeClient(
            API_URL, "k", enclave=enclave,
            transport_factory=lambda key, route: httpx.MockTransport(handler),
            preflight_client=httpx.Client(transport=httpx.MockTransport(preflight)))
        return client, calls

    def stream_body(self):
        return (b'data: {"id":"c1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
                b'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                b'data: [DONE]\n\n')

    def test_the_sealed_request_goes_through_the_proxy_too(self):
        """httpx takes no proxy from the environment for a client given a transport of
        its own, so the sealed request - alone of the app's - went direct, from this
        machine's address, while the preflight beside it went through the proxy."""
        import socket
        import socketserver

        import httpx
        from assistant import proxy
        try:
            import ehbp  # noqa: F401
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        except ImportError:
            self.skipTest("Privacy Mode's client is not installed here")
        seen = []

        class Proxy(socketserver.StreamRequestHandler):
            def handle(self):
                seen.append(self.rfile.readline().decode("latin-1").strip())
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        address = f"http://127.0.0.1:{server.server_address[1]}"
        key = X25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        enclave = sealed.Enclave(sealed.relay_url(API_URL),
                                 verifier=lambda *_a: self.verified(key))
        relay = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"cacheScope": "c" * 64, "upstreamModel": "glm-5.3"}))
        client = sealed.PrivateModeClient(API_URL, "k", enclave=enclave,
                                          preflight_client=httpx.Client(transport=relay))
        environment = {name: "" for name in proxy.VARIABLES + ("NO_PROXY", "no_proxy")}
        environment.update(HTTPS_PROXY=address, https_proxy=address)
        payload = {"model": self.MODEL, "stream": True,
                   "messages": [{"role": "user", "content": "hi"}]}
        direct, resolve = [], socket.getaddrinfo

        def looked_up(host, *args, **kwargs):     # a way round the proxy goes no further
            if host == "nano-gpt.com":
                direct.append(host)
                raise OSError("this test connects to nothing but its own proxy")
            return resolve(host, *args, **kwargs)

        with mock.patch.dict(os.environ, environment), \
                mock.patch("socket.getaddrinfo", looked_up), \
                self.assertRaisesRegex(PrivacyError, "the sealed request failed"):
            with client.request(self.MODEL, payload):
                pass
        self.assertEqual(direct, [])
        self.assertEqual(seen, ["CONNECT nano-gpt.com:443 HTTP/1.1"])

    def test_nothing_ehbp_did_not_seal_is_sent_to_the_enclave(self):
        """EHBP passes a request it finds no body in on as it came, unsealed, and the
        transport under it sent that too. It refuses anything without EHBP's
        encapsulated key now, as DA Vibe Manager's does; what EHBP sealed goes."""
        import httpx
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        key = X25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        sent = []

        def network(_transport, request):
            sent.append((request.headers, request.read()))
            return httpx.Response(500, text="stop here")

        url = sealed.relay_url(API_URL) + "/v1/chat/completions"
        with mock.patch.object(httpx.HTTPTransport, "handle_request", network), \
                httpx.Client(transport=sealed._sealing_transport(key)) as client:
            client.post(url, json={"messages": [{"role": "user", "content": "my secret"}]})
            with self.assertRaisesRegex(PrivacyError, "was not sealed, so it was refused"):
                client.post(url, content=b"")
        self.assertEqual(len(sent), 1, "the unsealed request was sent")
        headers, body = sent[0]
        self.assertIn("Ehbp-Encapsulated-Key", headers)
        self.assertNotIn(b"my secret", body)

    def stream_privately(self, session):
        llm = LLMClient(Config(os.path.join(tempfile.mkdtemp(), "c.json")))
        return llm.stream(self.MODEL, [{"role": "user", "content": "my secret question"}],
                          privacy=session)

    def rotated(self, strict):
        """The relay says the first sealed request was not sealed for the enclave's key,
        and the enclave attested again is an older release than the latest."""
        import dataclasses

        import httpx
        posts = []

        def handler(request):
            posts.append(request)
            if len(posts) == 1:
                return httpx.Response(400, json={"error": "missing_encrypted_body_header"})
            return httpx.Response(200, headers={"x-nanogpt-private-mode": "tinfoil"},
                                  content=self.stream_body())

        proofs = iter([self.verified(),
                       dataclasses.replace(self.verified("cd" * 32),
                                           enclave="router-old.tinfoil.sh",
                                           release_tag="v0.0.100",
                                           latest_release="v0.0.155")])
        client, _calls = self.make(handler, verifier=lambda: next(proofs))
        session = PrivacySession(API_URL, "k", self.MODEL, self.MODEL,
                                 sealed_factory=lambda *a: client, strict=strict)
        events = []
        session.listener = lambda event, model, detail: events.append((event, detail))
        return session, posts, events

    def test_an_enclave_attested_again_mid_request_is_held_to_strict(self):
        """Strict was applied by the session alone: an attestation made inside the
        request, after the relay's key-rotation answer, was used unchecked."""
        session, posts, events = self.rotated(strict=True)
        with self.assertRaisesRegex(LLMError, "attested again, proved less than it could "
                                              ".an older release than v0.0.155.*nothing "
                                              "was sent"):
            self.stream_privately(session)
        self.assertEqual(len(posts), 1)
        self.assertEqual(events[-1][0], "refused")

    def test_without_strict_the_banner_names_the_enclave_sealed_to(self):
        session, posts, events = self.rotated(strict=False)
        self.assertEqual(self.stream_privately(session), "Hi")
        self.assertEqual(len(posts), 2)
        attested = [detail for event, detail in events if event == "attested"]
        self.assertEqual([a.enclave for a in attested],
                         ["router-0.tinfoil.sh", "router-old.tinfoil.sh"])
        self.assertEqual(session.attestations[self.MODEL].latest_release, "v0.0.155")

    def test_a_release_not_checked_against_the_latest_falls_short(self):
        import dataclasses
        unchecked = dataclasses.replace(self.verified(), latest_checked=False)
        self.assertIn("its release not checked against the latest",
                      sealed.attestation_for(self.MODEL, unchecked).fell_short)
        self.assertEqual(sealed.attestation_for(self.MODEL, self.verified()).fell_short, [])
        client, _calls = self.make(lambda request: self.fail("sent"),
                                   verifier=lambda: unchecked)
        session = PrivacySession(API_URL, "k", self.MODEL, self.MODEL,
                                 sealed_factory=lambda *a: client, strict=True)
        with self.assertRaisesRegex(PrivacyError, "its release not checked"):
            session.ensure(self.MODEL)

    def test_a_proof_strict_refused_is_not_sealed_to_by_a_request_made_meanwhile(self):
        """attest() took a proof as the one to seal to before the session had held it
        to Strict. A request already past its own check - on another thread, or for
        another private/ model of the conversation - then went out sealed to an enclave
        that Strict refused a moment later."""
        import dataclasses

        import httpx
        clock = [0.0]
        unchecked = dataclasses.replace(self.verified("cd" * 32), latest_checked=False)
        proofs = iter([self.verified(), unchecked])
        posts = []

        def handler(request):
            posts.append(request)
            return httpx.Response(200, headers={"x-nanogpt-private-mode": "tinfoil"},
                                  content=self.stream_body())

        relay = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"cacheScope": "c" * 64, "upstreamModel": "glm-5.3"}))
        client = sealed.PrivateModeClient(
            API_URL, "k",
            enclave=sealed.Enclave(sealed.relay_url(API_URL),
                                   verifier=lambda relay_url, secret: next(proofs),
                                   clock=lambda: clock[0]),
            transport_factory=lambda key, route: httpx.MockTransport(handler),
            preflight_client=httpx.Client(transport=relay))
        session = PrivacySession(API_URL, "k", self.MODEL, self.MODEL,
                                 sealed_factory=lambda *a: client, strict=True)
        session.ensure(self.MODEL)                  # one request's check, passed
        clock[0] = sealed.MAX_AGE_SECONDS + 1       # its proof ages meanwhile
        with self.assertRaisesRegex(PrivacyError, "its release not checked"):
            session.ensure(self.MODEL)              # another's: attested again, refused
        with self.assertRaisesRegex(PrivacyError, "its release not checked.*nothing "
                                                  "was sent"):
            with client.request(self.MODEL, {"messages": []}):     # the first one's
                pass
        self.assertEqual(posts, [])

    def test_a_good_attestation_after_a_refusal_clears_the_red_banner(self):
        """The banner turns green only on "attested", which came only when the proof
        said something new: one refusal left it red over messages that went."""
        import httpx
        clock = [0.0]
        outcomes = iter([self.verified(), PrivacyError("TUF refused"), self.verified()])

        def verify(relay, secret):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        relay = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"cacheScope": "c" * 64, "upstreamModel": "glm-5.3"}))
        client = sealed.PrivateModeClient(
            API_URL, "k", enclave=sealed.Enclave(sealed.relay_url(API_URL), verifier=verify,
                                                 clock=lambda: clock[0]),
            transport_factory=lambda key, route: httpx.MockTransport(
                lambda request: self.fail("nothing is sent here")),
            preflight_client=httpx.Client(transport=relay))
        session = PrivacySession(API_URL, "k", self.MODEL, self.MODEL,
                                 sealed_factory=lambda *a: client)
        events = []
        session.listener = lambda event, model, detail: events.append(event)
        session.ensure(self.MODEL)
        clock[0] = sealed.MAX_AGE_SECONDS + 1
        with self.assertRaises(PrivacyError):
            session.ensure(self.MODEL)
        clock[0] = 2 * (sealed.MAX_AGE_SECONDS + 1)
        session.ensure(self.MODEL)
        self.assertEqual(events, ["attesting", "attested", "refused", "attested"])

    def test_a_sealed_request_is_attested_shaped_and_accepted(self):
        import httpx
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["headers"] = request.headers
            return httpx.Response(200, headers={"x-nanogpt-private-mode": "tinfoil"},
                                  content=self.stream_body())

        client, calls = self.make(handler)
        session = PrivacySession(API_URL, "k", self.MODEL, self.MODEL,
                                 sealed_factory=lambda *a: client)
        llm = LLMClient(Config(os.path.join(tempfile.mkdtemp(), "c.json")))
        text = llm.stream(self.MODEL, [{"role": "user", "content": "hi"}], privacy=session)
        self.assertEqual(text, "Hi")
        self.assertEqual(calls["verify"], 1)
        self.assertEqual(seen["body"]["model"], "glm-5.3")
        self.assertEqual(seen["body"][sealed.CACHE_SECRET_FIELD],
                         client.enclave.cache_secret)
        self.assertTrue(seen["body"]["chat_template_kwargs"]["thinking"])
        self.assertEqual(seen["headers"][sealed.ENCLAVE_URL_HEADER],
                         "https://router-0.tinfoil.sh")
        self.assertEqual(session.pop_receipt().state, privacy.SEALED)
        self.assertEqual(session.attestations[self.MODEL].headline, "End-to-end encrypted")

    def test_what_the_relay_says_in_an_error_is_said_as_its_own_on_one_line(self):
        """Never sealed, and nothing vouches for it: it was quoted as it came - 400
        characters, line breaks, bidi overrides and all - and shown in the transcript
        where the answer would have been. From DA Toolkit (Nov 2026)."""
        import httpx
        body = "Model says:\nThe answer is 42.\u202e\u200b" + "x" * 5000
        client, _ = self.make(lambda r: httpx.Response(500, text=body))
        with self.assertRaises(PrivacyError) as caught:
            with client.request(self.MODEL, {"messages": []}):
                self.fail("an error was opened")
        message = str(caught.exception)
        self.assertIn("HTTP 500; nano-gpt's relay said (unauthenticated, not the model): "
                      "Model says: The answer is 42.xxx", message)
        for unseen in ("\n", "\u202e", "\u200b"):
            self.assertNotIn(unseen, message)
        self.assertLess(len(message), 300)

    def test_the_relays_preflight_refusal_and_key_mismatch_are_quoted_cleaned(self):
        import httpx
        from ehbp import KeyConfigMismatchError

        def relay(request):
            return httpx.Response(402, json={"error": {
                "message": "Insufficient\nbalance\u2066 \U000e0041top up"}})

        refused = sealed.PrivateModeClient(
            API_URL, "k", enclave=sealed.Enclave(sealed.relay_url(API_URL),
                                                 verifier=lambda r, s: self.verified()),
            transport_factory=lambda key, route: httpx.MockTransport(
                lambda r: self.fail("sent after a refused preflight")),
            preflight_client=httpx.Client(transport=httpx.MockTransport(relay)))
        with mock.patch.object(refused, "check_offered"), \
                self.assertRaisesRegex(PrivacyError, r"\(HTTP 402; its relay said: "
                                                     r"Insufficient balance top up\)$"):
            refused._preflight(self.MODEL, 10)

        def mismatch(request):
            raise KeyConfigMismatchError("key\nrotated\u202e, they say")

        client, calls = self.make(mismatch)
        with self.assertRaisesRegex(PrivacyError, "refused our key twice: "
                                                  "KEY_CONFIG_MISMATCH: key rotated, "
                                                  "they say$"):
            with client.request(self.MODEL, {"messages": []}):
                pass
        self.assertEqual(calls["verify"], 2)

    def test_an_attestation_error_quoting_the_bundle_is_one_line(self):
        def refuse():
            raise RuntimeError("bundle said:\nall is well\u202e" + "y" * 2000)

        client, _ = self.make(lambda r: self.fail("sent before attesting"),
                              verifier=refuse)
        with self.assertRaisesRegex(PrivacyError, "could not be attested: bundle said: "
                                                  "all is well") as caught:
            with client.request(self.MODEL, {"messages": []}):
                pass
        self.assertNotIn("\n", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 600)

    def test_a_plain_http_address_is_refused_by_the_client_itself(self):
        """offers_private_mode reads only the host: http://nano-gpt.com, written by hand
        or brought back by a restore, passed it, and the preflight, which httpx sends,
        would have carried the key in the clear. The session refuses it when it starts;
        so does the client now. From DA Vibe Manager (Nov 2026)."""
        plain = "http://nano-gpt.com/api/v1/chat/completions"
        with self.assertRaisesRegex(PrivacyError, "is not https"):
            sealed.PrivateModeClient(plain, "k", enclave=sealed.Enclave(
                sealed.relay_url(plain), verifier=lambda r, s: self.fail("attested")))

    def test_a_reply_not_marked_private_is_refused(self):
        import httpx
        client, _ = self.make(lambda r: httpx.Response(200, content=self.stream_body()))
        with self.assertRaisesRegex(PrivacyError, "did not come back through Private"):
            with client.request(self.MODEL, {"messages": []}):
                pass

    def test_a_reply_that_is_not_2xx_is_refused_and_nothing_of_it_shown(self):
        """EHBP opens only a 2xx reply, and hands on any other without a response nonce
        just as it came. A 3xx marked as Private Mode, carrying a plaintext stream, was
        read as a sealed reply and shown as end-to-end encrypted."""
        import httpx
        for status in (301, 302, 303, 307, 308):
            client, _ = self.make(lambda r, status=status: httpx.Response(
                status, headers={"x-nanogpt-private-mode": "tinfoil",
                                 "location": "https://nano-gpt.com/elsewhere"},
                content=self.stream_body()))
            with self.assertRaisesRegex(PrivacyError, f"answered HTTP {status} instead "
                                                      f"of a sealed reply"):
                with client.request(self.MODEL, {"messages": []}):
                    self.fail(f"an HTTP {status} reply was opened")

        session = PrivacySession(API_URL, "k", self.MODEL, self.MODEL,
                                 sealed_factory=lambda *a: client)
        shown = []
        with tempfile.TemporaryDirectory() as directory:
            llm = LLMClient(Config(os.path.join(directory, "c.json")))
            with self.assertRaisesRegex(LLMError, "HTTP 308 instead of a sealed reply"):
                llm.stream(self.MODEL, [{"role": "user", "content": "hi"}],
                           on_delta=lambda piece, text: shown.append(text),
                           privacy=session)
        self.assertEqual(shown, [], "the plaintext stream was shown")
        self.assertIsNone(session.pop_receipt())

    def test_a_stream_cut_short_is_refused(self):
        import httpx
        body = b'data: {"id":"c1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
        client, _ = self.make(lambda r: httpx.Response(
            200, headers={"x-nanogpt-private-mode": "tinfoil"}, content=body))
        session = PrivacySession(API_URL, "k", self.MODEL, self.MODEL,
                                 sealed_factory=lambda *a: client)
        llm = LLMClient(Config(os.path.join(tempfile.mkdtemp(), "c.json")))
        with self.assertRaisesRegex(LLMError, "ended before it was complete"):
            llm.stream(self.MODEL, [{"role": "user", "content": "hi"}], privacy=session)

    def test_an_unsealed_refusal_is_retried_once_after_attesting_again(self):
        import httpx
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(400, json={"error": "missing_encrypted_body_header"})
            return httpx.Response(200, headers={"x-nanogpt-private-mode": "tinfoil"},
                                  content=self.stream_body())

        client, calls = self.make(handler)
        with client.request(self.MODEL, {"messages": []}) as response:
            response.read()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(calls["verify"], 2)

    def test_a_failed_attestation_sends_nothing(self):
        def refuse():
            raise RuntimeError("bad measurement")

        client, _ = self.make(lambda r: self.fail("sent before attesting"),
                              verifier=refuse)
        with self.assertRaisesRegex(PrivacyError, "could not be attested"):
            with client.request(self.MODEL, {"messages": []}):
                pass

    def relay(self, listed, preflight_status=400):
        """A client whose relay lists `listed` and whose preflight answers
        `preflight_status`; [] for a list that cannot be read. Attesting fails the
        test: a retired model is never attested."""
        import httpx
        seen = []

        def relay(request):
            seen.append(f"{request.method} {request.url.path}")
            if request.url.path.endswith("/models"):
                if not listed:
                    return httpx.Response(503)
                return httpx.Response(200, json={"data": [{"id": i} for i in listed]})
            if preflight_status >= 400:
                return httpx.Response(preflight_status, json={"error": "nope"})
            return httpx.Response(200, json={"cacheScope": "c" * 64,
                                             "upstreamModel": "glm-5.3"})

        def verify(relay_url, secret):
            seen.append("attest")
            return self.verified()

        client = sealed.PrivateModeClient(
            API_URL, "k", enclave=sealed.Enclave(sealed.relay_url(API_URL), verifier=verify),
            transport_factory=lambda key, route: httpx.MockTransport(
                lambda r: self.fail("sent to a retired model")),
            preflight_client=httpx.Client(transport=httpx.MockTransport(relay)))
        return client, seen

    def test_a_model_off_the_list_is_retired_and_named_with_what_is_offered(self):
        client, seen = self.relay(["private/glm-5-3", "private/kimi-k3"])
        with self.assertRaisesRegex(ModelRetired, "no longer offered.*nothing was sent.*"
                                                  "choose another private model") as caught:
            client.check_offered("private/glm-5-3-flash")
        self.assertEqual(caught.exception.model, "private/glm-5-3-flash")
        self.assertEqual(caught.exception.offered, ["private/glm-5-3", "private/kimi-k3"])
        client.check_offered(self.MODEL)
        self.assertEqual(seen, ["GET /api/v1/private/tinfoil/models"] * 2,
                         "the public list, on the relay, and nothing attested")
        unread, _ = self.relay([])
        unread.check_offered("private/glm-5-3-flash")      # nothing known, nothing said

    def test_a_preflight_refusal_for_a_retired_model_says_so(self):
        client, _ = self.relay([self.MODEL])
        with self.assertRaises(ModelRetired):
            client._preflight("private/glm-5-3-flash", 10)
        # Still listed: the refusal is nano-gpt's own, said as before.
        with self.assertRaisesRegex(PrivacyError, r"refused the Private Mode request "
                                                  r"\(HTTP 400") as plain:
            client._preflight(self.MODEL, 10)
        self.assertNotIsInstance(plain.exception, ModelRetired)
        unread, _ = self.relay([])
        with self.assertRaises(PrivacyError) as unknown:
            unread._preflight("private/glm-5-3-flash", 10)
        self.assertNotIsInstance(unknown.exception, ModelRetired)

    def test_a_conversation_checks_the_list_before_attesting_once_per_model(self):
        client, seen = self.relay([self.MODEL], preflight_status=200)
        retired = "private/glm-5-3-flash"
        session = PrivacySession(API_URL, "k", retired, self.MODEL,
                                 sealed_factory=lambda *a: client)
        events = []
        session.listener = lambda event, model, detail: events.append((event, model))
        with self.assertRaises(ModelRetired):
            session.ensure(retired)
        self.assertEqual(events, [("attesting", retired), ("refused", retired),
                                  ("retired", retired)])
        self.assertNotIn("attest", seen)
        self.assertNotIn(retired, session.attestations)
        session.ensure(self.MODEL)
        session.ensure(self.MODEL)
        self.assertEqual(seen.count("GET /api/v1/private/tinfoil/models"), 2,
                         "listed once for each model, not before every request")

    def test_a_retired_models_request_sends_nothing_and_says_why(self):
        client, _ = self.relay([self.MODEL])
        retired = "private/glm-5-3-flash"
        session = PrivacySession(API_URL, "k", retired, retired,
                                 sealed_factory=lambda *a: client)
        llm = LLMClient(Config(os.path.join(tempfile.mkdtemp(), "c.json")))
        with self.assertRaisesRegex(LLMError, "no longer offered"):
            llm.stream(retired, [{"role": "user", "content": "secret"}], privacy=session)

    def test_one_retired_since_it_was_attested_is_said_at_the_preflight(self):
        listed = [self.MODEL]
        client, _ = self.relay(listed)
        session = PrivacySession(API_URL, "k", self.MODEL, self.MODEL,
                                 sealed_factory=lambda *a: client)
        session.ensure(self.MODEL)
        listed.clear()
        listed.append("private/kimi-k3")                 # gone from the list since
        events = []
        session.listener = lambda event, model, detail: events.append((event, detail))
        llm = LLMClient(Config(os.path.join(tempfile.mkdtemp(), "c.json")))
        with self.assertRaisesRegex(LLMError, "no longer offered"):
            llm.stream(self.MODEL, [{"role": "user", "content": "hi"}], privacy=session)
        retired = [d for e, d in events if e == "retired"]
        self.assertEqual(retired[0].offered, ["private/kimi-k3"])

    def test_bodies_are_shaped_per_family(self):
        body = sealed.shape_body({"model": "x", "messages": [], "temperature": 1,
                                  "extra": 1}, "private/kimi-k3", "kimi-k3")
        self.assertNotIn("temperature", body)
        self.assertNotIn("extra", body)
        gemma = sealed.shape_body({"messages": []}, "private/gemma4-31b:thinking",
                                  "gemma-4-31b")
        self.assertTrue(gemma["chat_template_kwargs"]["enable_thinking"])


# ---- RedPill's gateway, nano-gpt's words, and private/ models only sealed ------------

# What RedPill's gateway adds to an attestation (live, Nov 2026), its keys good to 2100.
GATEWAY = {"api_version": "aci/1", "service_capabilities": {"serving": "aggregator"},
           "attestation": {"workload_keyset": {"not_after": 4102444800}}}


class GatewayAttestation(unittest.TestCase):
    """What nano-gpt relays for its TEE/ models is RedPill's gateway, which passes each
    request on to the model's host: its quote is the gateway's, and the model's own
    enclave is not shown. It counted as a full attestation. From DA Vibe Manager."""

    def attest(self, extra=GATEWAY):
        return FakeNanoGpt(attestation_extra=extra).tee_client(API_URL, "k", TEXT).attest()

    def test_the_gateways_attestation_is_partial_and_says_why(self):
        attestation = self.attest()
        self.assertEqual(attestation.level, "partial")
        self.assertIn(privacy.GATEWAY_SHORTFALL, attestation.headline)
        self.assertEqual(attestation.fell_short, [privacy.GATEWAY_SHORTFALL])
        self.assertIn("not that the model itself runs in an enclave", attestation.detail)
        self.assertEqual((attestation.gpus, attestation.tcb_status), (8, "UpToDate"),
                         "every other check stands")

    def test_either_of_its_marks_is_the_gateway(self):
        for extra in ({"api_version": "aci/1"},
                      {"service_capabilities": {"serving": "aggregator"}}):
            self.assertEqual(self.attest(extra).fell_short, [privacy.GATEWAY_SHORTFALL])
        self.assertEqual(self.attest({}).level, "full")

    def test_keys_it_attests_that_have_expired_are_refused(self):
        expired = dict(GATEWAY, attestation={"workload_keyset": {"not_after": 1700000000}})
        with self.assertRaisesRegex(PrivacyError, "attested keys expired on 2023-11-14"):
            self.attest(expired)

    def test_strict_refuses_it_and_says_where_to_turn(self):
        server = FakeNanoGpt(attestation_extra=GATEWAY)
        strict = PrivacySession(API_URL, "k", TEXT, VISION, tee_factory=server.tee_client,
                                strict=True)
        with self.assertRaisesRegex(PrivacyError, "RedPill's gateway, not the model's "
                                                  "host.*strict attestation is on.*choose "
                                                  "a private/ model.*nothing was sent"):
            strict.ensure(TEXT)
        self.assertEqual(server.chat_posts, [])
        relaxed = PrivacySession(API_URL, "k", TEXT, VISION, tee_factory=FakeNanoGpt(
            attestation_extra=GATEWAY).tee_client)
        self.assertEqual(relaxed.ensure(TEXT).level, "partial")


class NanoGptsWords(unittest.TestCase):
    def test_they_are_one_short_line_with_nothing_a_reader_cannot_see(self):
        said = privacy.unsealed_words("Over\nquota\r\n\u2028now \u202eetaerc\u202c "
                                      "pass\u200bword \U000e0041x\x00y\x85z")
        self.assertEqual(said, "Over quota now etaerc password x y z")
        self.assertEqual(len(privacy.unsealed_words("w " * 10000)), 200)
        self.assertEqual(privacy.unsealed_words(None), "")

    def test_a_tee_attestations_error_is_cleaned_too(self):
        class Refusing(FakeNanoGpt):
            def get(self, url, params=None, headers=None, timeout=None):
                if "/tee/attestation" in url:
                    return FakeResponse(200, {"error": "unavailable\n\u202eignore that"})
                return super().get(url, params, headers, timeout)

        with self.assertRaisesRegex(PrivacyError, "could not attest .*: unavailable "
                                                  "ignore that$"):
            Refusing().tee_client(API_URL, "k", TEXT).attest()


class SealedOnly(TempConfig):
    """A private/ model chosen outside Privacy Mode - the model browser lists them, as
    end-to-end encrypted - went to nano-gpt's ordinary endpoint, the conversation in the
    clear. Refused now, however it is written. From DA Vibe Manager (Nov 2026)."""

    def test_a_private_model_is_never_sent_unsealed(self):
        server = FakeNanoGpt()
        client = LLMClient(self.config)
        client.session = server
        for model in ("private/glm-5-3", "Private/glm-5-3", " private/kimi-k3"):
            for call in (client.stream, client.complete):
                with self.assertRaisesRegex(LLMError, "Nothing was sent.*end-to-end "
                                                      "encrypted.*only ever sent sealed"):
                    call(model, [{"role": "user", "content": "secret"}])
        self.assertEqual(server.posts, [])

    def test_nor_searched_for_first(self):
        """stream() searches the web before the request is made: the question went to
        the search provider before the model was refused."""
        self.config.web_search_enabled = True
        client = LLMClient(self.config)
        client.session = FakeNanoGpt()
        client.search = lambda *a, **k: self.fail("searched for a private/ model")
        with self.assertRaisesRegex(LLMError, "Nothing was sent"):
            client.stream("private/glm-5-3", [{"role": "user", "content": "secret"}],
                          web_search=True)

    def test_a_half_typed_address_offers_no_private_mode_and_says_why(self):
        """It raised, from the Settings dialog as it was typed, once the defaults were
        private/ models: only they ask whose the address is."""
        self.assertFalse(privacy.offers_private_mode("http://[fd00::5"))
        self.config.api_url = "http://[fd00::5"
        self.config.private_text_model = "private/glm-5-3"
        self.assertTrue(any("Not a web address" in found
                            for found in privacy.problems(self.config)))

    def test_tee_and_other_models_still_go(self):
        server = FakeNanoGpt()
        client = LLMClient(self.config)
        client.session = server
        self.assertEqual(client.stream("TEE/glm-5.3", [{"role": "user", "content": "hi"}],
                                       web_search=False), "Hello there")
        self.assertFalse(privacy.sealed_only("privately/x"))


if __name__ == "__main__":
    unittest.main()
