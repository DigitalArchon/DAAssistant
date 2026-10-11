"""nano-gpt Private Mode: end-to-end encrypted chat with an attested enclave.

nano-gpt's `private/*` models run in Tinfoil's enclaves. Unlike the `TEE/*` models,
whose prompts pass nano-gpt's gateway in the clear, a Private Mode request body is
sealed on this machine to a key only the attested enclave holds, and the reply comes
back sealed the same way. nano-gpt relays ciphertext: it sees the account, the model,
timing, sizes and cost.

nano-gpt ships this as a Node proxy (`@nanogpt/private-mode`), run with
`npx ...@latest`: the code that holds the plaintext would come from, and be updated by,
the party the encryption is meant to keep out. So, like StoryLoom (whose
providers/private_mode.py this is ported from), this client does what the proxy does
with Tinfoil's own Python SDK, pinned by hash in packaging/private-mode.lock, and runs
no code from nano-gpt:

1. Attest (`Enclave.current`). Tinfoil's SecureClient.verify fetches the attestation
   bundle through nano-gpt's relay and checks it against the hardware vendor's roots
   (AMD SEV-SNP or Intel TDX) and Sigstore's record of the router release
   (tinfoilsh/confidential-model-router). The report binds the enclave's HPKE key.
   Through a relay the SDK accepts any genuine release, so the latest is looked up from
   Tinfoil directly and an older one is reported, not refused: rollouts lag.
2. Preflight. nano-gpt checks the balance, reserves a charge and names the upstream
   model. It issues a cache scope; we send its sha256 as the header it wants, but the
   sealed body carries our own random secret, so nobody else - nano-gpt included - can
   time our prompt cache.
3. Seal the body with EHBP (HPKE X25519, AES-256-GCM) to the attested key, and open the
   sealed reply. A 2xx reply that is not sealed is refused by the transport, so is one
   nano-gpt does not mark as Private Mode, and so is a stream that ends without its
   final chunk. Nothing is ever sent unsealed, and nothing falls back to the plain
   endpoint: llm.py refuses a private/ model everywhere but here.

The attestation is redone before a request once it is five minutes old (the proxy's
figure: laptop sleep, a key rotated underneath us), and once more when the enclave says
the key has changed.
"""

import functools
import hashlib
import json
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

from . import models
from .privacy import E2EE, Attestation, ModelRetired, PrivacyError, offers_private_mode

RELAY_PATH = "/api/v1/private/tinfoil"
ROUTER_REPO = "tinfoilsh/confidential-model-router"
MAX_AGE_SECONDS = 5 * 60
PREFLIGHT_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT = 10
ENCLAVE_URL_HEADER = "X-Tinfoil-Enclave-Url"
REQUIRED_STEPS = ("verifyCode", "verifyEnclave", "compareMeasurements")
INSTALL_HINT = ("End-to-end encrypted models need Tinfoil's client, which is not "
                "installed: run tools/install-private-mode.sh, then restart the app.")

# The request fields Tinfoil's router takes (the proxy's
# PRIVATE_TINFOIL_CHAT_COMPLETION_BODY_FIELDS, less tools, which this app never sends).
# Anything else is dropped before sealing.
ALLOWED_FIELDS = frozenset({
    "chat_template_kwargs", "frequency_penalty", "logit_bias", "max_tokens", "messages",
    "model", "presence_penalty", "reasoning_effort", "response_format", "seed", "stop",
    "stream", "stream_options", "temperature", "top_p", "user",
})
# The router-only field that scopes the enclave's prompt cache: sealed with the body,
# never seen by nano-gpt.
CACHE_SECRET_FIELD = "user_cache_secret"


@functools.lru_cache(maxsize=None)
def available():
    """(True, version) when Tinfoil's SDK can be imported, else (False, what to do).
    Cached: it cannot change without a restart, since vendored.py sets sys.path once."""
    try:
        import ehbp  # noqa: F401
        import tinfoil  # noqa: F401
    except Exception:  # an ImportError, or a dependency built for another Python
        return False, INSTALL_HINT
    try:
        from importlib.metadata import version
        return True, f"tinfoil {version('tinfoil')}"
    except Exception:
        return True, "tinfoil"


def relay_url(api_url):
    parsed = urlparse(api_url)
    return f"{parsed.scheme}://{parsed.netloc}{RELAY_PATH}"


@dataclass(frozen=True)
class Verified:
    """What an attestation proved: the key to seal to, and the record."""

    hpke_public_key: str
    enclave: str
    hardware: str
    measurement: str
    release_digest: str
    release_tag: str
    latest_release: str
    latest_checked: bool


def _hardware(document):
    kind = str(((document.get("enclaveMeasurement") or {}).get("measurement") or {})
               .get("type"))
    if "sev-snp" in kind:
        return "AMD SEV-SNP"
    if "tdx" in kind:
        return "Intel TDX"
    return kind


def tinfoil_verify(relay, cache_secret):
    """Attest Tinfoil's router through nano-gpt's relay (network; worker thread)."""
    # Imported here: the SDK pulls in sigstore and the OpenAI client, which nothing else
    # needs, and the app should not pay for them at startup.
    from tinfoil import SecureClient
    from tinfoil.github import fetch_latest_release

    # The secret is passed explicitly, or the SDK would create one in ~/.tinfoil; this
    # client only verifies, it never sends a request.
    client = SecureClient(base_url=relay + "/", attestation_bundle_url=relay,
                          transport="ehbp", user_cache_secret=cache_secret)
    truth = client.verify()
    document = client.get_verification_document().to_dict()
    steps = document.get("steps") or {}
    incomplete = [step for step in REQUIRED_STEPS
                  if (steps.get(step) or {}).get("status") != "success"]
    if document.get("securityVerified") is not True or incomplete \
            or not truth.hpke_public_key:
        raise PrivacyError("the enclave's attestation was incomplete"
                           + (f" ({', '.join(incomplete)} did not succeed)"
                              if incomplete else ""))
    digest = document.get("releaseDigest") or truth.digest
    tag, latest, checked = str(document.get("releaseTag") or ""), "", False
    try:
        release = fetch_latest_release(ROUTER_REPO)
        checked = True
        if release.digest == digest:
            tag = tag or release.tag
        else:
            latest = release.tag
    except Exception:  # reported, never fatal
        pass
    return Verified(hpke_public_key=truth.hpke_public_key, enclave=client.enclave,
                    hardware=_hardware(document),
                    measurement=str(document.get("enclaveFingerprint") or ""),
                    release_digest=str(digest or ""), release_tag=tag,
                    latest_release=latest, latest_checked=checked)


def attestation_for(model, verified):
    release = verified.release_tag or f"release {verified.release_digest[:12]}"
    if verified.latest_release:
        currency = f"an older release than the latest, {verified.latest_release}"
    elif verified.latest_checked:
        currency = "the latest release"
    else:
        currency = "the latest release could not be looked up"
    measured = (f"measured by {verified.hardware}" if verified.hardware
                else "measured by its hardware")
    detail = (f"{verified.enclave} runs {ROUTER_REPO} {release} ({currency}), {measured}; "
              f"its HPKE key sha256 "
              f"{hashlib.sha256(verified.hpke_public_key.encode()).hexdigest()[:16]}…")
    return Attestation(model=model, kind=E2EE, level="full", detail=detail,
                       enclave=verified.enclave, hardware=verified.hardware,
                       release=release, latest_release=verified.latest_release)


class Enclave:
    """The attested enclave one conversation seals to. Thread-safe."""

    def __init__(self, relay, verifier=tinfoil_verify, clock=time.monotonic):
        self.relay = relay
        # Ours, not nano-gpt's: see the module docstring. One per conversation, never
        # written anywhere.
        self.cache_secret = secrets.token_hex(32)
        self._verifier = verifier
        self._clock = clock
        self._lock = threading.Lock()
        self._verified = None
        self._at = 0.0

    def current(self):
        """The attested key, attesting first if there is none or it has aged."""
        with self._lock:
            fresh = (self._verified is not None
                     and 0 <= self._clock() - self._at < MAX_AGE_SECONDS)
            if not fresh:
                self._verified = None
                try:
                    self._verified = self._verifier(self.relay, self.cache_secret)
                except PrivacyError:
                    raise
                except Exception as e:  # the SDK's own errors
                    raise PrivacyError(
                        f"the private model's enclave could not be attested: {e}") from e
                self._at = self._clock()
            return self._verified

    def invalidate(self, verified=None):
        """Forget the key (it was rotated): the next request attests again."""
        with self._lock:
            if verified is None or self._verified is verified:
                self._verified = None


def _sealing_transport(public_key):
    import httpx
    from ehbp import EHBPTransport

    inner = httpx.HTTPTransport(limits=httpx.Limits(max_keepalive_connections=0))
    return EHBPTransport.from_public_key_hex(public_key, inner=inner)


class PrivateModeClient:
    """Sends private/ models' requests sealed, through nano-gpt's relay."""

    def __init__(self, api_url, api_key, request_timeout=180, enclave=None,
                 transport_factory=_sealing_transport, preflight_client=None):
        if not offers_private_mode(api_url):
            raise PrivacyError("end-to-end encrypted models are nano-gpt's Private Mode, "
                               "and the API URL is not nano-gpt's")
        ok, message = available() if enclave is None else (True, "")
        if not ok:
            raise PrivacyError(message)
        self.api_url = api_url
        self.api_key = api_key
        self.request_timeout = request_timeout
        self.enclave = enclave or Enclave(relay_url(api_url))
        self._transport_factory = transport_factory
        self._preflight_client = preflight_client
        self._client = None
        self._client_key = None

    @property
    def endpoint(self):
        return self.enclave.relay + "/v1/chat/completions"

    def attest(self, model):
        return attestation_for(model, self.enclave.current())

    def private_ids(self):
        """nano-gpt's list of Private Mode models - public, no key is sent - read on the
        preflight's client. [] when it cannot be read."""
        import httpx
        client = self._preflight_client or httpx.Client(follow_redirects=False)
        try:
            response = client.get(models.private_list_url(self.api_url),
                                  timeout=PREFLIGHT_TIMEOUT_SECONDS)
            return models.private_ids_in(response.json()
                                         if response.status_code < 400 else {})
        except (httpx.HTTPError, ValueError):
            return []
        finally:
            if self._preflight_client is None:
                client.close()

    def check_offered(self, model):
        """Raise ModelRetired, with what is offered instead, if nano-gpt has retired
        `model`. A list that cannot be read claims nothing: the preflight still refuses
        a model that is gone."""
        ids = self.private_ids()
        if models.offered(model, ids) is False:
            raise ModelRetired(model, ids)

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _sealed_client(self, verified):
        import httpx
        if self._client is None or self._client_key != verified.hpke_public_key:
            if self._client is not None:
                self._client.close()
            self._client = httpx.Client(
                transport=self._transport_factory(verified.hpke_public_key),
                timeout=httpx.Timeout(self.request_timeout, connect=CONNECT_TIMEOUT),
                # A redirect would carry the sealed body somewhere else; it comes back
                # as a 3xx instead, without the Private Mode header, and is refused.
                follow_redirects=False)
            self._client_key = verified.hpke_public_key
        return self._client

    def _preflight(self, model, body_bytes):
        """nano-gpt's check before a sealed request: (cache scope, upstream model)."""
        import httpx
        client = self._preflight_client or httpx.Client(follow_redirects=False)
        try:
            response = client.post(self.enclave.relay + "/preflight",
                                   json={"model": model, "requestBodyBytes": body_bytes},
                                   headers=self._headers(),
                                   timeout=PREFLIGHT_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            raise PrivacyError(f"nano-gpt's Private Mode check failed: {e}") from e
        finally:
            if self._preflight_client is None:
                client.close()
        if response.status_code >= 400:
            # Every path's backstop - a model retired mid-conversation, the connection
            # test: say why, not just that it was refused.
            self.check_offered(model)
            raise PrivacyError(f"nano-gpt refused the Private Mode request "
                               f"(HTTP {response.status_code}: {_error_text(response)})")
        try:
            data = response.json()
        except ValueError as e:
            raise PrivacyError("nano-gpt's Private Mode check was not JSON") from e
        scope = str(data.get("cacheScope") or "").strip()
        upstream = str(data.get("upstreamModel") or "").strip()
        if len(scope) != 64 or not upstream:
            raise PrivacyError("nano-gpt's Private Mode check did not say where to send it")
        return scope, upstream

    @contextmanager
    def request(self, model, payload):
        """Send one sealed, streamed request; yields the open response.

        Attested first: nothing, not even nano-gpt's charge, before the enclave has
        proved what it is. Retried once, after attesting again, if the enclave says the
        key has rotated or nano-gpt says the body was not sealed - the proxy's one safe
        retry. Anything else is an error, and nothing falls back.
        """
        import httpx
        from ehbp import EHBPError, KeyConfigMismatchError

        if not model.startswith("private/"):
            raise PrivacyError(f"{model} is not a Private Mode model; nothing was sent")
        for attempt in (0, 1):
            verified = self.enclave.current()
            client = self._sealed_client(verified)
            size = len(json.dumps(payload).encode("utf-8"))
            scope, upstream = self._preflight(model, size)
            body = shape_body(payload, model, upstream)
            body[CACHE_SECRET_FIELD] = self.enclave.cache_secret
            headers = {
                **self._headers(),
                "Accept": "text/event-stream",
                "x-nanogpt-private-model": model,
                "x-nanogpt-private-stream": "true",
                "x-nanogpt-private-cache-scope": hashlib.sha256(scope.encode()).hexdigest(),
                "x-query-source": "api",
                ENCLAVE_URL_HEADER: f"https://{verified.enclave}",
            }
            try:
                response = client.send(
                    client.build_request("POST", self.endpoint, json=body,
                                         headers=headers),
                    stream=True)
            except KeyConfigMismatchError as e:
                self.enclave.invalidate(verified)
                if attempt:
                    raise PrivacyError(f"the enclave refused our key twice: {e}") from e
                continue
            except EHBPError as e:
                raise PrivacyError(f"the encrypted reply could not be opened: {e}") from e
            except httpx.TimeoutException as e:
                raise PrivacyError(f"no response within {self.request_timeout} "
                                   f"seconds") from e
            except httpx.HTTPError as e:
                raise PrivacyError(f"the sealed request failed: {e}") from e

            if response.status_code >= 400:
                response.read()
                text = _error_text(response)
                response.close()
                if attempt == 0 and response.status_code == 400 \
                        and "missing_encrypted_body_header" in text:
                    self.enclave.invalidate(verified)
                    continue
                raise PrivacyError(f"HTTP {response.status_code}: {text}")
            if response.headers.get("x-nanogpt-private-mode") != "tinfoil":
                response.close()
                raise PrivacyError("the reply did not come back through Private Mode; "
                                   "refused")
            try:
                yield response
            finally:
                response.close()
            return
        raise PrivacyError("the enclave's key kept changing; nothing was sent")

    @staticmethod
    def lines(response):
        """The decrypted lines of a sealed stream."""
        import httpx
        from ehbp import EHBPError
        try:
            yield from response.iter_lines()
        except EHBPError as e:
            raise PrivacyError(f"the encrypted reply could not be opened: {e}") from e
        except httpx.HTTPError as e:
            raise PrivacyError(f"the sealed reply was cut off: {e}") from e

    @staticmethod
    def finish(finish_reason, saw_done):
        if not (finish_reason and saw_done):
            # The proxy's private_stream_incomplete: whatever came may be partial.
            raise PrivacyError("the encrypted reply ended before it was complete")


def _error_text(response):
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:400] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        if body.get("message"):
            return str(body["message"])
    return json.dumps(body)[:400]


# How each family's router spells reasoning effort (the proxy's requestTransforms.js).
# GLM and Kimi take low, high or max; medium has no spelling of its own there.
_EFFORT = {"low": "low", "minimal": "low", "medium": "high", "high": "high", "max": "max"}


def shape_body(payload, model, upstream):
    """The body the router takes, as nano-gpt's proxy shapes it for each model family
    (its requestTransforms.js), for the fields this app sends.

    `payload["reasoning"]`, when llm.py sets it, is {"effort": ...}: how hard a model
    that reasons should think. It never switches thinking on for a model that can do
    without it (deepseek-v4, Gemma): only the :thinking variants think there.
    """
    body = dict(payload)
    body["model"] = upstream
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    reasoning = body.pop("reasoning", None)
    effort = str(reasoning.get("effort") or "").lower() if isinstance(reasoning, dict) \
        else ""
    thinking_suffix = model.endswith(":thinking")
    kwargs = dict(body.get("chat_template_kwargs") or {})
    if upstream.startswith("glm-5"):
        # Always reasons, and at length unless told otherwise: live (Sept 2026), a
        # 5,000-character translation spent a 20,480-token limit reasoning and never
        # began its answer. The effort goes at the top level.
        kwargs["thinking"] = True
        if effort:
            body["reasoning_effort"] = _EFFORT.get(effort, "low")
    elif upstream.startswith("deepseek-v4"):
        kwargs["thinking"] = thinking_suffix
        if thinking_suffix and effort:
            kwargs["reasoning_effort"] = ("low" if effort in ("low", "minimal")
                                         else "high" if effort == "medium" else "xhigh")
    elif upstream.startswith("gemma"):
        kwargs["enable_thinking"] = thinking_suffix
    elif upstream.startswith("kimi"):
        # Kimi K3's template fixes its own sampling and always reasons.
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            body.pop(key, None)
        if effort:
            body["reasoning_effort"] = _EFFORT.get(effort, "low")
    if kwargs:
        body["chat_template_kwargs"] = kwargs
    return {key: value for key, value in body.items() if key in ALLOWED_FIELDS}
