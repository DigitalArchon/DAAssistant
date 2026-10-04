"""Privacy Mode: conversations kept to models that run in an attested enclave.

nano-gpt offers two kinds, and this module is the one place that tells them apart:

  TEE/*      Run inside a trusted execution environment. Before anything is sent, the
             enclave is attested (below), and every reply is checked to be signed by
             the key that attestation vouched for. The text still passes nano-gpt's
             gateway in the clear: attestation proves where the model runs, not that
             nano-gpt cannot read it. Their own docs say so.
  private/*  nano-gpt's Private Mode: end-to-end encrypted. The body is sealed on this
             machine to a key only the attested enclave holds, and the reply comes back
             sealed the same way; nano-gpt relays ciphertext. See sealed.py.

The code is ported from StoryLoom (providers/tee.py, private_mode.py), which talks to
the same models and whose docs/design/private.md records what was measured live. The
parts that matter here:

- `GET {base}/tee/attestation?model=&nonce=` returns the enclave's signing address, an
  Intel TDX quote whose report data is that address, zero padding, then our nonce - so
  the hardware quote itself binds the key that signs replies to *this* request - and
  NVIDIA GPU evidence, made with the same nonce. The quote is verified up to Intel's
  root, for revocation and for its TCB status (dcap.py); the GPU evidence goes to
  NVIDIA's attestation service, whose signed verdict is checked in turn (nras.py).
- `GET {base}/tee/signature/{completion id}` returns a text and an EIP-191 signature;
  the signer recovered from them (ethsig.py) must be the attested address. What that
  proves is that the attested key signed a record for this reply's id. It does not tie
  the reply's *content* to the signature: the record hashes the bodies as nano-gpt's
  gateway saw them, and those hashes match nothing this app sends or receives (checked
  live, Sept 2026). So a reply is "signed", never "verified".

A conversation's privacy is decided once, when it starts (`PrivacySession.start`), and
never relaxed: its models are copied then, so switching Privacy Mode off, or choosing
other models in Settings, leaves every open private conversation exactly as it was.
"""

import base64
import binascii
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import requests

from . import dcap, nras
from .ethsig import recover_personal

NRAS_URL = "https://nras.attestation.nvidia.com/v3/attest/gpu"
TIMEOUT_SECONDS = 30
PRIVATE_MODE_HOSTS = frozenset({"nano-gpt.com"})

TEE = "tee"
E2EE = "e2ee"

# What one reply's check found. See Receipt.
SIGNED = "signed"            # TEE: the attested key signed this reply's record
MISMATCH = "mismatch"        # TEE: a different key did
UNSIGNED = "unsigned"        # TEE: no signature was kept, or it could not be fetched
SEALED = "sealed"            # E2EE: it came back sealed by the attested enclave

TEE_NOTE = ("TEE: the model runs in an attested enclave and its replies are signed, but "
            "the text passes nano-gpt's gateway in the clear.")
E2EE_NOTE = ("End-to-end encrypted: every message is sealed on this machine to a key only "
             "the model's attested enclave holds. nano-gpt relays ciphertext; it sees your "
             "account, the model, timing, sizes and cost, never the text.")


class PrivacyError(Exception):
    """A private request was refused before anything was sent."""


# ---- which models qualify -------------------------------------------------------------

def is_tee(model):
    return bool(model) and model.startswith("TEE/")


def is_private_mode(model):
    return bool(model) and model.startswith("private/")


def kind_of(model):
    """TEE, E2EE, or None for a model that runs nowhere in particular."""
    if is_private_mode(model):
        return E2EE
    if is_tee(model):
        return TEE
    return None


def offers_private_mode(api_url):
    return (urlparse(api_url or "").hostname or "").lower() in PRIVATE_MODE_HOSTS


def describe(model):
    """(short label, explanation) for a model's privacy, or ("", "")."""
    kind = kind_of(model)
    if kind == E2EE:
        return "end-to-end encrypted", E2EE_NOTE
    if kind == TEE:
        return "TEE", TEE_NOTE
    return "", ""


def problems(config):
    """Why Privacy Mode cannot run with these settings, as a list of sentences."""
    found = []
    for key, what in (("private_text_model", "text"), ("private_premium_model", "premium"),
                      ("private_vision_model", "vision")):
        model = (getattr(config, key, "") or "").strip()
        if not model:
            found.append(f"No private {what} model is set.")
        elif kind_of(model) is None:
            found.append(f"The private {what} model, {model}, is not a TEE model.")
        elif is_private_mode(model) and not offers_private_mode(config.api_url):
            found.append(f"{model} is end-to-end encrypted through nano-gpt, and the API "
                         f"URL is not nano-gpt's.")
        elif is_private_mode(model):
            from . import sealed
            if not sealed.available()[0]:
                found.append(f"{model} needs Tinfoil's client, which is not installed: "
                             f"run tools/install-private-mode.sh, then restart the app.")
    return found


# ---- attestation ----------------------------------------------------------------------

@dataclass
class Attestation:
    """What an enclave proved before anything was sent to it."""

    model: str
    kind: str
    # full: everything this app can check was checked and held.
    # partial: nothing failed, but something could not be checked or is not current;
    # `shortfalls` names each in a few words, for the headline.
    level: str = "full"
    detail: str = ""
    summary: str = ""               # one line of `detail`, for the chat window's banner
    checked_at: float = field(default_factory=time.time)
    shortfalls: list = field(default_factory=list)
    # TEE
    signing_address: str = ""
    gpu_ok: object = None           # True once NVIDIA's signed verdict held, else None
    gpus: int = 0
    tcb_status: str = ""            # Intel's, when its collateral could be checked
    # E2EE
    enclave: str = ""
    hardware: str = ""
    release: str = ""
    latest_release: str = ""        # set when the enclave runs an older release

    @property
    def headline(self):
        if self.kind == E2EE:
            return "End-to-end encrypted"
        if not self.shortfalls:
            return "TEE attested"
        return f"TEE attested ({', '.join(self.shortfalls)})"


@dataclass
class Receipt:
    """What was checked about one reply."""

    model: str
    kind: str
    state: str
    detail: str = ""


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _jwt_claims(value):
    """Every JWT's payload found in a JSON value, in order."""
    found = []
    if isinstance(value, str) and value.count(".") == 2:
        part = value.split(".")[1]
        try:
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (ValueError, binascii.Error):
            return found
        if isinstance(claims, dict):
            found.append(claims)
    elif isinstance(value, list):
        for item in value:
            found.extend(_jwt_claims(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_jwt_claims(item))
    return found


def api_base(api_url):
    """The API root the chat endpoint hangs from: .../api/v1 for .../api/v1/chat/completions."""
    parsed = urlparse(api_url)
    path = parsed.path
    for suffix in ("/chat/completions", "/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return f"{parsed.scheme}://{parsed.netloc}{path.rstrip('/')}"


def _bases(api_url):
    """Where the TEE endpoints may live: nano-gpt's docs give the path three ways."""
    parsed = urlparse(api_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return list(dict.fromkeys([api_base(api_url), f"{root}/api/v1", f"{root}/v1"]))


def _error_text(response):
    """The provider's message from an error response, or ""."""
    try:
        body = response.json()
    except ValueError:
        return (getattr(response, "text", "") or "").strip()[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        return str(error or body.get("message") or "")[:200]
    return ""


def _instance_verifier():
    """verify(certificate DER, body, signature) -> sha256 of the certificate's public
    key when its signature over body holds, else None. Raises ImportError without the
    cryptography package, before any evidence is looked at."""
    import hashlib

    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    def verify(certificate, body, signature):
        try:
            key = x509.load_der_x509_certificate(certificate).public_key()
            if isinstance(key, rsa.RSAPublicKey):
                key.verify(signature, body, padding.PKCS1v15(), hashes.SHA256())
            elif isinstance(key, ec.EllipticCurvePublicKey):
                key.verify(signature, body, ec.ECDSA(hashes.SHA256()))
            else:
                return None
        except (InvalidSignature, ValueError):
            return None
        return hashlib.sha256(key.public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).digest()

    return verify


class _Checks:
    """What one attestation's checks found, gathered across quotes and instances."""

    def __init__(self, *notes):
        self.notes = list(notes)
        self.shortfalls = []
        self.status = ""
        self.quotes = 0              # quotes Intel's checks were run on
        self.gpus = 0
        self.summary_gpu = set()

    def note(self, *notes):
        for note in notes:
            if note not in self.notes:
                self.notes.append(note)

    def fall_short(self, what):
        if what not in self.shortfalls:
            self.shortfalls.append(what)

    def unchecked(self):
        self.fall_short("signatures unchecked")
        self.note("Intel's and NVIDIA's signatures could not be checked: the "
                  "cryptography package is not installed")

    def tcb(self, status):
        """Keep the worst Intel TCB status seen."""
        def rank(value):
            return dcap.STATUSES.index(value) if value in dcap.STATUSES else len(dcap.STATUSES)
        if status and (not self.status or rank(status) > rank(self.status)):
            self.status = status

    def merge(self, other):
        self.note(*other.notes)
        for what in other.shortfalls:
            self.fall_short(what)
        self.tcb(other.status)
        self.quotes += other.quotes
        self.gpus += other.gpus
        self.summary_gpu |= other.summary_gpu

    def attestation(self, model, bound, signing_address=""):
        if self.quotes:
            summary = [f"Intel quote{'s' if self.quotes != 1 else ''} verified, "
                       f"TCB {self.status or 'unchecked'}"]
        else:
            summary = ["Intel quote unchecked"]
        if self.gpus:
            summary.append(f"NVIDIA verified {self.gpus} GPU{'s' if self.gpus != 1 else ''}")
        else:
            summary += sorted(self.summary_gpu)[:1]
        return Attestation(model=model, kind=TEE,
                           level="partial" if self.shortfalls else "full",
                           detail="; ".join(self.notes), shortfalls=self.shortfalls,
                           summary="; ".join(summary + [bound]),
                           signing_address=signing_address,
                           gpu_ok=True if self.gpus else None, gpus=self.gpus,
                           tcb_status=self.status)


class TeeClient:
    """Attestation and reply signatures for one TEE/ model."""

    def __init__(self, api_url, api_key, model, session=None, intel=None, nvidia=None):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.session = session or requests.Session()
        self.signing_address = None
        self._base = None
        # The two verifiers, replaceable so tests can stand in for Intel and NVIDIA.
        self._intel = intel or dcap.verify
        self._nvidia = nvidia or nras.verify

    def _get(self, path, params):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last = ""
        for base in [self._base] if self._base else _bases(self.api_url):
            try:
                response = self.session.get(f"{base}{path}", params=params,
                                            headers=headers, timeout=TIMEOUT_SECONDS)
            except requests.exceptions.RequestException as e:
                last = str(e)
                continue
            if response.status_code == 404:
                last = f"{base}{path}: 404"
                continue
            if response.status_code >= 400:
                # nano-gpt's own words, when it gives any: "Unable to reach RedPill
                # attestation service" says far more than a status code.
                reason = _error_text(response)
                error = PrivacyError(f"nano-gpt answered HTTP {response.status_code}"
                                     + (f": {reason}" if reason else ""))
                error.status_code = response.status_code
                error.reason = reason
                raise error
            try:
                body = response.json()
            except ValueError:
                raise PrivacyError(f"{base}{path} returned a non-JSON body")
            if not isinstance(body, dict):
                raise PrivacyError(f"{base}{path} returned {type(body).__name__}")
            self._base = base
            return body
        raise PrivacyError(f"no TEE endpoint answered ({last})")

    def attest(self):
        """Check the enclave now, with a fresh nonce.

        nano-gpt's TEE/ models come from more than one provider, and they attest in
        two shapes (both measured live, Sept 2026):

          aci/1          one enclave, whose TDX quote binds its reply-signing key and
                         our nonce, with GPU evidence made with our nonce;
          per instance   a list of instances (Chutes: TEE/kimi-k3, TEE/qwen3.5-27b,
                         ...), each quote binding that instance's own key, which signs
                         a body carrying our nonce, the quote and the GPU evidence.

        Refuses - PrivacyError, and nothing is sent - when an enclave cannot be
        trusted: no attestation, a quote that does not bind our nonce (directly, or by
        a key the quote binds), a quote Intel's chain does not vouch for (forged,
        revoked, a debug TD, a TCB Intel has revoked or never listed), GPU evidence
        whose NVIDIA verdict is unsigned, stale, for another nonce or negative, or - in
        the per-instance shape - any instance that failed to attest, since a request
        could be routed to it.

        What merely could not be checked - Intel's collateral or NVIDIA's service out of
        reach, no GPU evidence offered, no `cryptography` package - or is not current
        (any Intel TCB status but UpToDate) makes the attestation partial, and the
        headline names it.

        Stricter than StoryLoom, which reads these verdicts without checking their
        signatures: a nonce or a verdict that merely comes back in JSON proves nothing,
        since anything can echo a string.
        """
        nonce = secrets.token_hex(32)
        try:
            body = self._get("/tee/attestation", {"model": self.model, "nonce": nonce})
        except PrivacyError as e:
            status = getattr(e, "status_code", None)
            if status == 400:
                # Live (Sept 2026): the Tinfoil-hosted TEE/ models offer no attestation
                # here - nano-gpt says fresh attestation for them only comes through
                # Tinfoil's own flow, which is the one their private/ twins use.
                reason = getattr(e, "reason", "")
                hint = ("; its end-to-end encrypted private/ version, where nano-gpt "
                        "offers one, is attested that way" if "tinfoil" in reason.lower()
                        else "")
                raise PrivacyError(f"{self.model} offers no attestation"
                                   + (f" (nano-gpt: {reason})" if reason else "") + hint)
            # 429 too: live, nano-gpt answers "Unable to reach Chutes TEE evidence
            # service" with it, and the same request passed a minute later.
            if status is not None and (status >= 500 or status == 429):
                raise PrivacyError(f"nano-gpt could not fetch {self.model}'s attestation "
                                   f"just now ({e}); this is on the provider's side, and "
                                   f"may pass if tried again")
            raise
        if isinstance(body.get("evidence"), list):
            return self._attest_instances(body, nonce)
        if isinstance(body.get("all_attestations"), list) and "signing_address" not in body:
            # RedPill relaying Chutes (live, Sept 2026: TEE/glm-5.1, TEE/qwen3.5-27b,
            # TEE/qwen3.5-397b-a17b). The quotes are made for a nonce the relay chose -
            # not ours, and not derived from it - and nothing signed ties them to ours,
            # so they would read the same if replayed from any earlier day.
            relayed = body.get("nonce")
            why = ("made for a nonce the relay chose, not ours" if relayed != nonce
                   else "carries no binding of our nonce this app can check")
            raise PrivacyError(f"{self.model}'s attestation, relayed by its provider "
                               f"({body.get('attestation_type') or 'unnamed'}), was {why}, "
                               f"so it cannot show the enclave is live for this request")
        if body.get("signing_address") or body.get("intel_quote"):
            return self._attest_signing_key(body, nonce)
        if body.get("error"):
            raise PrivacyError(f"nano-gpt could not attest {self.model}: {body['error']}")
        raise PrivacyError(f"{self.model}'s attestation is in a shape this app does not "
                           f"recognise (it holds {', '.join(sorted(body)) or 'nothing'})")

    def _attest_signing_key(self, body, nonce):
        """aci/1: one enclave, its reply-signing key and our nonce in its quote."""
        address = body.get("signing_address")
        address = address if isinstance(address, str) and address.startswith("0x") else None
        if address is None:
            raise PrivacyError(f"{self.model}'s attestation named no signing key")
        try:
            raw = bytes.fromhex(str(body.get("intel_quote") or ""))
            quote = dcap.parse_quote(raw)
        except (ValueError, dcap.DcapError) as e:
            raise PrivacyError(f"{self.model}'s Intel TDX quote could not be read ({e})")
        # Read from the quote's own bytes - the part Intel's chain signs - rather than
        # from nano-gpt's JSON copy of it: the signing address, zero padding, our nonce.
        report_data = quote.report_data
        if report_data[:20] != bytes.fromhex(address[2:]) or any(report_data[20:32]) \
                or report_data[32:] != bytes.fromhex(nonce):
            raise PrivacyError(f"{self.model}'s attestation did not bind its signing key "
                               f"to our nonce")

        check = _Checks("the Intel TDX quote binds the signing key to our nonce")
        self._check_quote(raw, check)
        payload = _json(body.get("nvidia_payload"))
        self._check_gpus(payload if isinstance(payload, dict) else None, nonce, check,
                         "this request's nonce")
        self.signing_address = address
        return check.attestation(self.model, "signing key bound to our nonce",
                                 signing_address=address)

    def _attest_instances(self, body, nonce):
        """Per instance: each quote binds an instance key, which signed our nonce.

        For each instance, all of these before it counts:
          - its certificate's public key hashes to the second half of its quote's
            report data, so the key belongs to that TD (the certificate's own issuer,
            the provider's private CA, is not a trust anchor and is not relied on);
          - that key's signature over `attested_body` verifies, and the body carries
            our nonce and exactly this quote - so an instance whose key the hardware
            vouches for answered this request;
          - the quote passes Intel's checks, like any other;
          - the GPU evidence - the signed copy - passes NVIDIA's checks. It is made with
            the first half of the report data as its nonce (measured live: NVIDIA's
            verdict matches that and nothing else), so it is bound to the quote, and
            through the signature to this request.
        The instances are checked in parallel; the attestation takes the worst of them.
        """
        failed = [str(i) for i in body.get("failed_instance_ids") or []]
        instances = body["evidence"]
        if failed:
            raise PrivacyError(f"{len(failed)} of {len(failed) + len(instances)} of "
                               f"{self.model}'s instances could not be attested, and a "
                               f"request could be routed to one of them")
        if not instances:
            raise PrivacyError(f"{self.model}'s attestation listed no instances")
        try:
            _instance_key = _instance_verifier()
        except ImportError:
            raise PrivacyError(f"{self.model}'s instances can only be tied to our nonce by "
                               f"checking their signatures, which needs the cryptography "
                               f"package (python3-cryptography)")

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(instances))) as pool:
            results = list(pool.map(
                lambda item: self._check_instance(item, nonce, _instance_key), instances))
        check = _Checks(f"each of {len(instances)} instances' keys, bound into its TDX "
                        f"quote, signed our nonce")
        for result in results:
            check.merge(result)
        summary = (f"{len(instances)} instance{'s' if len(instances) != 1 else ''}, each "
                   f"key bound to our nonce")
        attestation = check.attestation(self.model, summary)
        attestation.detail += ("; this provider signs no replies, so answers show as "
                               "unsigned")
        return attestation

    def _check_instance(self, item, nonce, verify_signature):
        name = str(item.get("instance_id") or "an instance")[:8]
        try:
            raw = base64.b64decode(item["quote"])
            quote = dcap.parse_quote(raw)
            signed = base64.b64decode(item["attested_body"])
            certificate = base64.b64decode(item["certificate"])
            signature = base64.b64decode(item["signature"])
        except (KeyError, TypeError, ValueError, binascii.Error, dcap.DcapError) as e:
            raise PrivacyError(f"{self.model}'s instance {name} sent unreadable evidence "
                               f"({e})")
        key_hash = verify_signature(certificate, signed, signature)
        if key_hash is None:
            raise PrivacyError(f"{self.model}'s instance {name} did not sign its evidence "
                               f"with the key it presented")
        if quote.report_data[32:] != key_hash:
            raise PrivacyError(f"{self.model}'s instance {name} signed with a key its TDX "
                               f"quote does not bind")
        try:
            attested = json.loads(signed)
            evidence = attested["evidence"]
        except (ValueError, KeyError, TypeError):
            raise PrivacyError(f"{self.model}'s instance {name} signed unreadable evidence")
        if attested.get("nonce") != nonce:
            raise PrivacyError(f"{self.model}'s instance {name} did not sign our nonce")
        if base64.b64decode(evidence.get("tdx_quote") or "") != raw:
            raise PrivacyError(f"{self.model}'s instance {name} signed a different quote")

        check = _Checks()
        self._check_quote(raw, check)
        gpu = _json(evidence.get("nvtrust_evidence"))
        payload = None
        if isinstance(gpu, list) and gpu:
            payload = {"nonce": quote.report_data[:32].hex(),
                       "arch": gpu[0].get("arch", "HOPPER"), "evidence_list": gpu}
        self._check_gpus(payload, quote.report_data[:32].hex(), check,
                         "the nonce its quote binds")
        return check

    def _check_quote(self, raw, check):
        """Intel's verification of one quote, into `check`. Refuses a bad quote."""
        try:
            intel = self._intel(raw, self.session)
        except ImportError:
            check.unchecked()
        except dcap.DcapError as e:
            raise PrivacyError(f"{self.model}'s Intel TDX quote failed verification: {e}")
        else:
            check.note(*intel.notes)
            check.tcb(intel.status)
            check.quotes += 1
            if not intel.collateral:
                check.fall_short("Intel TCB unchecked")
            elif intel.status != "UpToDate":
                check.fall_short(f"Intel TCB {intel.status}")

    def _check_gpus(self, payload, nonce, check, bound_to):
        """NVIDIA's verdict on one set of GPU evidence, into `check`."""
        if not (isinstance(payload, dict) and payload.get("evidence_list")):
            # Live, several TEE/ models send an empty evidence list: not a rejection,
            # but nothing for NVIDIA to vouch for either.
            check.fall_short("no GPU evidence")
            check.note("no GPU evidence was offered")
            check.summary_gpu.add("no GPU evidence")
            return
        answer = self._nras_answer(payload)
        if answer is None:
            check.fall_short("GPUs unverified")
            check.note("NVIDIA's attestation service could not be reached")
            check.summary_gpu.add("GPUs unverified")
            return
        try:
            gpus = self._nvidia(answer, nonce, self.session)
        except (ImportError, nras.KeysUnavailable) as e:
            # Unchecked, but a verdict that says no is still believed.
            if not any(c.get("x-nvidia-overall-att-result") is True
                       for c in _jwt_claims(answer)):
                raise PrivacyError(f"NVIDIA rejected {self.model}'s GPU evidence")
            check.summary_gpu.add("NVIDIA's verdict unchecked")
            if isinstance(e, ImportError):
                check.unchecked()
            else:
                check.fall_short("NVIDIA signature unchecked")
                check.note(f"{e}, so NVIDIA's verdict is unchecked")
        except nras.NrasError as e:
            raise PrivacyError(f"{self.model}'s GPU attestation failed: {e}")
        else:
            check.gpus += gpus
            check.note(f"NVIDIA's signed verdict vouches for {gpus} GPU"
                       f"{'s' if gpus != 1 else ''}, for {bound_to}")

    def _nras_answer(self, payload):
        """NRAS's answer to the GPU evidence, or None if the service could not be
        reached. An answer of HTTP 4xx is NVIDIA refusing the evidence."""
        try:
            response = self.session.post(NRAS_URL, json=payload, timeout=TIMEOUT_SECONDS)
        except requests.exceptions.RequestException:
            return None
        if response.status_code >= 500:
            return None
        if response.status_code >= 400:
            raise PrivacyError(f"NVIDIA rejected {self.model}'s GPU evidence "
                               f"(HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError:
            return None

    def check_reply(self, response_id):
        """SIGNED, MISMATCH or UNSIGNED, with a sentence saying why."""
        if not response_id:
            return UNSIGNED, "the reply carried no id to check"
        if self.signing_address is None:
            # The per-instance providers (Chutes) sign no replies: nano-gpt answers
            # "TEE signature is not available for this model". Their instances were each
            # attested instead.
            return UNSIGNED, ("this model's provider signs no replies; every instance "
                              "that could answer was attested before sending")
        try:
            body = self._get(f"/tee/signature/{quote(response_id, safe='')}",
                             {"model": self.model, "signing_algo": "ecdsa"})
        except PrivacyError:
            # Live: some TEE/ models' providers keep no signature for a reply.
            return UNSIGNED, "the provider kept no signature for this reply"
        text, signature = body.get("text"), body.get("signature")
        if not isinstance(text, str) or not isinstance(signature, str):
            return UNSIGNED, "the signature record held no text or signature"
        try:
            signer = recover_personal(text, signature)
        except ValueError:
            return MISMATCH, "the signature is not a valid signature"
        if signer.lower() != self.signing_address.lower():
            return MISMATCH, f"signed by {signer}, not the attested {self.signing_address}"
        return SIGNED, f"signed by the attested key {self.signing_address}"


# ---- one conversation ---------------------------------------------------------------

class PrivacySession:
    """One conversation's Privacy Mode: its models, and what their enclaves proved.

    Built once, when an operation starts, from the settings at that moment, and kept by
    that conversation to its end. Nothing reads config.privacy_mode or the private model
    settings again for it, which is what makes "you cannot leave Privacy Mode
    mid-conversation" true by construction rather than by a check somewhere.

    `uses` is which of the private models the conversation plans to use, by role
    ("text", "premium", "vision"): OCR the vision model and the answering model its
    follow-ups go to, the clipboard operations only the answering model, translating or
    querying an image both. A model the user picks later, from the chat window's model
    menu, joins with enlist(): any TEE/ or private/ model, attested before first use
    like the rest. Nothing else can join, which is what keeps the conversation private
    whatever is chosen in it.

    Thread-safe: the chat window's worker thread does the attesting.
    """

    def __init__(self, api_url, api_key, text_model, vision_model, request_timeout=180,
                 tee_factory=TeeClient, sealed_factory=None, uses=("text", "vision"),
                 premium_model=None):
        self.api_url = api_url
        self.api_key = api_key
        self.text_model = text_model
        self.vision_model = vision_model
        self.premium_model = premium_model or text_model
        self._models = list(dict.fromkeys(self.model_for(role) for role in uses))
        self.request_timeout = request_timeout
        self._tee_factory = tee_factory
        self._sealed_factory = sealed_factory
        self._lock = threading.RLock()
        self._tee = {}
        self._sealed = None
        self.attestations = {}          # model -> Attestation, newest check
        self._receipts = []
        # Called with (event, model, detail) from whichever thread is working:
        # "attesting", "attested" (detail: Attestation), "refused" (detail: message),
        # "sent", "receipt" (detail: Receipt). The chat window marshals to GTK.
        self.listener = None

    @classmethod
    def start(cls, config, uses=("text", "vision")):
        """The session for a new conversation, or None when Privacy Mode is off.

        Raises PrivacyError when it is on and cannot be honoured: an operation must not
        quietly fall back to the ordinary models.
        """
        if not getattr(config, "privacy_mode", False):
            return None
        found = problems(config)
        if found:
            raise PrivacyError("Privacy Mode is on, but " + " ".join(
                f[0].lower() + f[1:] for f in found) + " Nothing was sent.")
        return cls(config.api_url, config.api_key,
                   config.private_text_model.strip(), config.private_vision_model.strip(),
                   request_timeout=config.request_timeout, uses=uses,
                   premium_model=config.private_premium_model.strip())

    # ---- models ---------------------------------------------------------------------

    def model_for(self, role):
        return {"text": self.text_model, "premium": self.premium_model,
                "vision": self.vision_model}[role]

    def models(self):
        """The models this conversation can send to, in the order they joined."""
        return list(self._models)

    def role(self, model):
        """What the model is to this conversation: "text", "premium and vision",
        "chosen" for one picked from the model menu..."""
        roles = [role for role in ("text", "premium", "vision")
                 if self.model_for(role) == model]
        return " and ".join(roles) or "chosen"

    def enlist(self, model):
        """Let the conversation send to `model` from now on. Raises PrivacyError for a
        model that runs in no enclave, or one that could not be used privately here."""
        with self._lock:
            if model in self._models:
                return
        kind = kind_of(model)
        if kind is None:
            raise PrivacyError(f"{model} is not a TEE model, and this conversation is "
                               f"private; choose a TEE/ or private/ model")
        if kind == E2EE:
            if not offers_private_mode(self.api_url):
                raise PrivacyError(f"{model} is end-to-end encrypted through nano-gpt, "
                                   f"and the API URL is not nano-gpt's")
            from . import sealed
            ok, message = sealed.available()
            if not ok:
                raise PrivacyError(message)
        with self._lock:
            if model not in self._models:
                self._models.append(model)

    def allows(self, model):
        return model in self._models

    @property
    def all_sealed(self):
        return all(is_private_mode(m) for m in self.models())

    # ---- events ---------------------------------------------------------------------

    def _emit(self, event, model, detail=None):
        listener = self.listener
        if listener is not None:
            listener(event, model, detail)

    def note_sent(self, model):
        self._emit("sent", model)

    # ---- attestation ----------------------------------------------------------------

    def sealed_client(self):
        """The end-to-end encrypted client every private/ model here is sent through.
        One per conversation, so its enclave is attested once and its cache secret -
        ours, random, never stored - lasts exactly as long as the conversation."""
        with self._lock:
            if self._sealed is None:
                factory = self._sealed_factory
                if factory is None:
                    from . import sealed
                    factory = sealed.PrivateModeClient
                self._sealed = factory(self.api_url, self.api_key, self.request_timeout)
            return self._sealed

    def ensure(self, model):
        """Attest `model`'s enclave if it has not been, and return what it proved.

        TEE enclaves are attested once per conversation: every reply's signature is
        then checked against that key. The end-to-end encrypted enclave is re-attested
        by sealed.py whenever its proof is five minutes old, before anything is sealed.
        Raises PrivacyError, having sent nothing, when the enclave cannot be trusted.
        """
        if not self.allows(model):
            raise PrivacyError(f"{model} is not one of this private conversation's models; "
                               f"nothing was sent")
        kind = kind_of(model)
        if kind is None:
            raise PrivacyError(f"{model} is not a TEE model; nothing was sent")
        with self._lock:
            known = self.attestations.get(model)
            if kind == TEE and known is not None:
                return known
            if known is None:
                self._emit("attesting", model)
            try:
                if kind == E2EE:
                    attestation = self.sealed_client().attest(model)
                else:
                    client = self._tee.get(model)
                    if client is None:
                        client = self._tee[model] = self._tee_factory(
                            self.api_url, self.api_key, model)
                    attestation = client.attest()
            except PrivacyError as e:
                self._emit("refused", model, str(e))
                raise PrivacyError(f"{e}; nothing was sent") from e
            except Exception as e:  # the SDK's own errors, network failures
                message = f"{model}'s enclave could not be attested: {e}"
                self._emit("refused", model, message)
                raise PrivacyError(f"{message}; nothing was sent") from e
            changed = known is None or known.detail != attestation.detail
            self.attestations[model] = attestation
        if changed:
            self._emit("attested", model, attestation)
        return attestation

    def check_reply(self, model, response_id):
        """Record and return what can be said about one reply."""
        if kind_of(model) == E2EE:
            receipt = Receipt(model, E2EE, SEALED,
                              "decrypted here; only the attested enclave could have "
                              "sealed it")
        else:
            client = self._tee.get(model)
            if client is None:
                receipt = Receipt(model, TEE, UNSIGNED, "the enclave had not been attested")
            else:
                state, detail = client.check_reply(response_id)
                receipt = Receipt(model, TEE, state, detail)
        with self._lock:
            self._receipts.append(receipt)
        self._emit("receipt", model, receipt)
        return receipt

    def pop_receipt(self):
        """The newest reply's receipt, taken so it is not read twice. None if none."""
        with self._lock:
            return self._receipts.pop() if self._receipts else None

    # ---- what to show ---------------------------------------------------------------

    def caveats(self):
        """What was not checked, or stays visible, for the kinds of model this
        conversation uses - never a TEE/ caveat in a conversation with none."""
        kinds = {kind_of(m) for m in self.models()}
        lines = []
        if E2EE in kinds:
            lines.append(
                "End-to-end encrypted models: before anything is sealed to it, the "
                "enclave's hardware report and its software release, against Tinfoil's "
                "Sigstore record, are checked, and every reply must come back sealed by "
                "it. nano-gpt relays only ciphertext, but still sees your account, the "
                "model, when you send, how much, and the cost.")
        if TEE in kinds:
            lines.append(
                "TEE models: nano-gpt's gateway sees the text in the clear. Not checked "
                "by this app: whether a reply's content matches its signed record (the "
                "record hashes the bodies as the gateway saw them), so that your request "
                "was the one the attested enclave handled rests on nano-gpt forwarding "
                "it honestly; and which software the enclave runs - its measurements are "
                "proved genuine by Intel's chain, but not compared with a known build.")
        return lines

    def details(self):
        """Every model and what its enclave proved, for the details dialog."""
        lines = []
        for model in self.models():
            role = self.role(model)
            attestation = self.attestations.get(model)
            lines.append(f"{model}  ({role})")
            if attestation is None:
                lines.append("    not used yet: it will be attested before anything is "
                             "sent to it")
            else:
                when = time.strftime("%H:%M:%S", time.localtime(attestation.checked_at))
                lines.append(f"    {attestation.headline}, checked at {when}")
                lines.append(f"    {attestation.detail}")
                if attestation.signing_address:
                    lines.append(f"    signing key {attestation.signing_address}")
            lines.append(f"    {describe(model)[1]}")
            lines.append("")
        return "\n".join(lines).rstrip()


# ---- turn badges ------------------------------------------------------------------------

def receipt_badge(receipt):
    """(label, tone, tooltip) for an answer, from its receipt."""
    if receipt is None:
        return None
    if receipt.state == SEALED:
        return ("🔐 End-to-end encrypted", "ok", receipt.detail)
    if receipt.state == SIGNED:
        return ("🔒 TEE · signed by attested enclave", "ok",
                receipt.detail + ". The signature covers the reply's record; its "
                "content is not compared with it.")
    if receipt.state == MISMATCH:
        return ("⚠ TEE · signature mismatch", "bad", receipt.detail)
    return ("🔒 TEE · signature unavailable", "warn", receipt.detail)


def sent_badge(kinds):
    """(label, tone, tooltip) for a question, from the kinds of model it went to."""
    if kinds and all(kind == E2EE for kind in kinds):
        return ("🔐 Sealed end-to-end", "ok",
                "Encrypted on this machine to the attested enclave's key before sending.")
    return ("🔒 Sent to attested TEE", "ok",
            "Sent only after the enclave was attested. TEE text passes nano-gpt's "
            "gateway in the clear.")


def receiving_badge(kind):
    if kind == E2EE:
        return ("🔐 Receiving sealed reply…", "pending",
                "Each chunk is decrypted here as it arrives")
    return ("🔒 Receiving from attested TEE…", "pending",
            "Its signature is checked once it is complete")


def pending_badge(session):
    if session.all_sealed:
        return ("🔐 Private", "pending", "Will be sealed to an attested enclave")
    return ("🔒 Private", "pending", "Will be sent only once the enclave is attested")
