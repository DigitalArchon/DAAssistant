"""Privacy Mode: attestation, reply signatures, the sealed client, and the rule that a
private conversation never leaves its attested models.

The TEE checks run against a fake nano-gpt that attests, signs and streams like the
real one (the report data layout, NRAS's JWT list and EIP-191 signatures as measured
live, Sept 2026). Intel's and NVIDIA's verification is checked against a real
attestation captured with its collateral (tests/data/tee_attestation.json), at the
moment it was captured, and against forgeries of it. The end-to-end encrypted client
runs only where Tinfoil's SDK is installed (tools/install-private-mode.sh), against a
mocked transport.

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
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401  (pins GI versions)
from assistant import dcap, models, nras, privacy, sealed
from assistant.config import Config
from assistant.ethsig import (address_of, keccak256, personal_hash, recover,
                              recover_personal, sign)
from assistant.llm import LLMClient, LLMError
from assistant.privacy import PrivacyError, PrivacySession, TeeClient
from assistant.ui.markdown_html import turn_html

KEY = 0xB25C7DB31FEED9122727BF0939DC769A96564B2DE4C4726D035B36ECF1E5B364
ADDRESS = "0x5ce9454909639D2D17A3F753ce7d93fa0b9aB12E"
API_URL = "https://nano-gpt.com/api/v1/chat/completions"
TEXT = "TEE/glm-5.3-flash"
VISION = "TEE/qwen3.6-27b"


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
                 intel=UP_TO_DATE, nvidia=8, nras_status=200):
        self.nonce_bound = nonce_bound
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
            return FakeResponse(self.nras_status,
                                [["JWT", jwt({"x-nvidia-overall-att-result": self.gpu})],
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

    def test_without_cryptography_it_says_the_signatures_were_not_checked(self):
        attestation = self.client(intel=ImportError("cryptography"),
                                  nvidia=ImportError("cryptography")).attest()
        self.assertEqual(attestation.shortfalls, ["signatures unchecked"])
        with self.assertRaisesRegex(PrivacyError, "NVIDIA rejected"):
            self.client(intel=ImportError(), nvidia=ImportError(), gpu=False).attest()

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


class FixtureSession:
    """Serves one real attestation, captured live from TEE/glm-5.3-flash with Intel's
    collateral and NVIDIA's answer and key, exactly as they came (tests/data)."""

    def __init__(self, fixture, intel_status=200, jwks=None):
        self.fixture = fixture
        self.intel_status = intel_status
        self.jwks = fixture["jwks"] if jwks is None else jwks

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
        impostor = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(1)
                    .not_valid_before(datetime(2018, 1, 1, tzinfo=timezone.utc))
                    .not_valid_after(datetime(2049, 1, 1, tzinfo=timezone.utc))
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

    def test_collateral_out_of_reach_is_reported_not_passed(self):
        result = self.intel(session=FixtureSession(self.fixture, intel_status=503))
        self.assertFalse(result.collateral)
        self.assertEqual(result.status, "")
        self.assertIn("could not be fetched", result.notes[-1])

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


# ---- the session ---------------------------------------------------------------------

class InstanceEvidence:
    """Chutes' per-instance attestation, built with a real key: each instance's
    certificate key is bound into its quote's report data, and signs a body carrying the
    nonce, the quote and the GPU evidence - the shape TEE/kimi-k3 answers with."""

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
             failed=()):
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
            attested = json.dumps({"nonce": signed_nonce or nonce, "evidence": {
                "tdx_quote": base64.b64encode(signed_quote).decode(),
                "nvtrust_evidence": json.dumps(gpu)}}).encode()
            signer = sign_with or key
            signature = signer.sign(attested, padding.PKCS1v15(), hashes.SHA256())
            evidence.append({
                "instance_id": f"inst-{i}", "quote": base64.b64encode(quote).decode(),
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
    """TEE/kimi-k3 and the other Chutes models (live, Sept 2026)."""

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

    def test_nothing_is_sent_when_the_enclave_is_not_attested(self):
        server = FakeNanoGpt(nonce_bound=False)
        with self.assertRaisesRegex(LLMError, "nothing was sent"):
            self.client(server).stream(TEXT, [{"role": "user", "content": "secret"}],
                                       privacy=session_for(server))
        self.assertEqual(server.chat_posts, [])

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

    def test_a_failure_names_the_model_and_its_part(self):
        from assistant.ui.chat import ChatWindow

        def read(cancel, status):
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
            transport_factory=lambda key: httpx.MockTransport(handler),
            preflight_client=httpx.Client(transport=httpx.MockTransport(preflight)))
        return client, calls

    def stream_body(self):
        return (b'data: {"id":"c1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
                b'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                b'data: [DONE]\n\n')

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

    def test_a_reply_not_marked_private_is_refused(self):
        import httpx
        client, _ = self.make(lambda r: httpx.Response(200, content=self.stream_body()))
        with self.assertRaisesRegex(PrivacyError, "did not come back through Private"):
            with client.request(self.MODEL, {"messages": []}):
                pass

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

    def test_bodies_are_shaped_per_family(self):
        body = sealed.shape_body({"model": "x", "messages": [], "temperature": 1,
                                  "extra": 1}, "private/kimi-k3", "kimi-k3")
        self.assertNotIn("temperature", body)
        self.assertNotIn("extra", body)
        gemma = sealed.shape_body({"messages": []}, "private/gemma4-31b:thinking",
                                  "gemma-4-31b")
        self.assertTrue(gemma["chat_template_kwargs"]["enable_thinking"])


if __name__ == "__main__":
    unittest.main()
