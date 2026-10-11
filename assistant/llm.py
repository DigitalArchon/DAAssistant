"""OpenAI-compatible chat client.

Details that matter:
  - attach_image() re-attaches the image on every request, to the user turn it arrived
    on, so follow-ups keep it and it stays put.
  - Streaming carries images too.
  - The provider's JSON error body is parsed and shown, so a bad key or an unknown model
    is not an opaque HTTP status.
  - max_tokens is always sent, or long OCR transcriptions hit provider defaults and are
    truncated mid-text.
  - Cancellation is a threading.Event, not a plain bool read across threads.
"""

import gzip
import json
import threading
import time

import requests

from . import models as catalogue, privacy as privacy_module, prompts, websearch
from .privacy import ModelRetired, PrivacyError

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 3
# Reaching the provider takes a second or it is not going to happen. request_timeout is
# for the wait that follows, which for a long answer is legitimately minutes.
CONNECT_TIMEOUT = 10

# Provider web-search options. Values are exactly what the API accepts; the UI builds
# its dropdowns from these lists so the two can never drift apart.
WEB_SEARCH_MODES = ["off", "standard", "deep"]
# Ranked and pruned in websearch.py, which is where the measurements are recorded.
WEB_SEARCH_PROVIDERS = websearch.PROVIDERS
WEB_SEARCH_CONTEXT_SIZES = ["low", "medium", "high"]
WEB_SEARCH_VALYU_TYPES = ["all", "web"]


# Web search is run by assistant/websearch.py against POST /api/web, not by putting
# `:online` on the model name. The suffix hands the search to the chat endpoint, which
# builds the query from the latest user message and never returns the sources; doing it
# ourselves means we choose the query and get titles and URLs back to cite.
SEARCH_CONTEXT_HEADER = "Use these web search results where they are relevant."

# Compress request bodies from here up. Small bodies gain nothing, and a gzip header on
# a two-kilobyte request is just ceremony. Level 6 is where the curve flattens.
# A search query, not an essay: enough room for a quoted fragment and no more.
QUERY_TOKENS = 120

# Room to think, on top of max_tokens, for a model asked for a reasoning effort. The
# limit is a ceiling, not a charge: only what is used is billed. Without it, a model
# that thinks first could spend the answer's whole allowance before starting it -
# private/glm-5-3 did exactly that, and the window said only "empty response".
REASONING_ROOM = {"none": 0, "minimal": 2048, "low": 4096, "medium": 16384,
                  "high": 16384, "xhigh": 32768, "max": 32768}
CUT_OFF_NOTE = "\n\n_(Cut off: the reply reached its token limit.)_"

GZIP_THRESHOLD_BYTES = 16 * 1024
GZIP_LEVEL = 6

# Explicit prompt caching is deliberately not sent. It reads well - Claude caches
# nothing unless asked - but measured against this API it is a straight surcharge.
#
# The provider chooses the cache breakpoints, and it puts a single one at the end of the
# whole prompt. So the write is only ever read back by a byte-identical repeat request,
# which a conversation never produces: every turn adds a message, and a searched turn
# brings new results as well. Three follow-ups in a row wrote 1,244-1,248 tokens each
# and read nothing. A marked-up `cache_control` breakpoint at the end of the stable
# prefix was ignored too; only an exact whole-prompt match ever hit, at 11,916 tokens
# read on the second and third of three identical sends.
#
# The cost of asking anyway, same request, same 4,216 input tokens:
#     promptCaching off   $0.008682
#     promptCaching on    $0.010779     (cacheCost 0.010535, cacheReadInputTokens 0)
# a 24% premium for a cache nothing reads. Worth revisiting if the API ever exposes
# breakpoint placement; until then the stable-prefix work below stands on its own.
# https://docs.nano-gpt.com/api-reference/miscellaneous/prompt-caching


def _balance_quotes(query):
    """Tidy the model's quotation marks.

    Three cases. Quotes around the whole answer are the model presenting its query, and
    would otherwise demand an exact match on every word - dropped. A balanced pair
    inside is a phrase the model means to match together, which is worth keeping. An odd
    quote is debris: `0x80070643" KB5034441` searches for something nobody wrote.
    """
    if len(query) > 1 and query[0] == '"' and query[-1] == '"' \
            and query.count('"') == 2:
        query = query[1:-1].strip()
    if query.count('"') % 2:
        query = query.replace('"', "").strip()
    return query


def web_search_active(config, enabled=None):
    """Whether search should run for this request."""
    active = config.web_search_enabled if enabled is None else enabled
    if not active:
        return False
    return (getattr(config, "web_search_mode", "") or "standard").lower() != "off"


# Web search results come back as structured data after all, so nothing has to be
# reconstructed from the prose:
#
#   x_nanogpt_search_results      titles, URLs and snippets
#   x_nanogpt_search_annotations  character ranges tying the answer to its sources
#   choices[0].message.citations  on search-native models
#
# The app used to ask the model to copy `URL:` lines out of its own context instead,
# which both trusted the model to transcribe and, worse, put a paragraph about web
# search results into the user's turn - where the search query is built from. A question
# about a screenshot of song lyrics duly came back with results about how search engines
# work. Reading the real fields costs nothing and says nothing to the model.
SEARCH_RESULT_KEYS = ("x_nanogpt_search_results", "x_nanogpt_search_annotations",
                      "citations", "annotations", "web_search_results", "sources")


def system_message(instructions):
    """How to answer, kept out of the user's turn.

    The search query is derived from the latest user message, so everything in it is
    searched for. Instructions belong where instructions go.
    """
    if not instructions:
        return None
    return {"role": "system", "content": instructions}


class LLMError(Exception):
    """A request failed. `model` is the model it was for, when known, so the window can
    say which of a conversation's models failed - the OCR model reading the image, or
    the one answering - rather than leave the reader to guess."""

    def __init__(self, message, model=None):
        super().__init__(message)
        self.model = model


def attach_image(messages, image_base64, mime="image/png"):
    """Return a copy of `messages` with the image on the user turn it arrived on.

    That is the first user turn: a conversation is opened around one capture, and the
    capture is part of the question that was asked about it. On the *last* user turn
    it would stay visible too, but move forward on every follow-up. A prefix that
    reshuffles is a prefix that
    cannot be cached, and the capture is the expensive part of the request, so each
    follow-up paid full price for it again.

    OpenAI-compatible content-parts form. If a message already carries parts we append
    to them rather than clobbering, so an image survives repeated turns.
    """
    if not image_base64:
        return [dict(m) for m in messages]

    out = [dict(m) for m in messages]
    idx = next((i for i, m in enumerate(out) if m.get("role") == "user"), None)
    if idx is None:
        return out

    content = out[idx].get("content", "")
    parts = list(content) if isinstance(content, list) else [{"type": "text", "text": content}]
    if not any(p.get("type") == "image_url" for p in parts):
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{image_base64}"},
        })
    out[idx] = dict(out[idx], content=parts)
    return out


def _extract_citations(blob):
    """Pull web-search sources out of a response, a chunk or a message.

    Providers differ on where they put these, and NanoGPT adds its own two, so accept
    any of SEARCH_RESULT_KEYS and ignore anything unrecognised.
    """
    if not isinstance(blob, dict):
        return []
    found = []
    for key in SEARCH_RESULT_KEYS:
        entries = blob.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                found.append((entry, entry))
            elif isinstance(entry, dict):
                inner = entry.get("url_citation") if isinstance(
                    entry.get("url_citation"), dict) else entry
                url = inner.get("url") or inner.get("link") or inner.get("uri")
                if url:
                    found.append((inner.get("title") or url, url))
    return found


def format_citations(citations):
    """Render collected sources as a Markdown footer."""
    if not citations:
        return ""
    lines = ["", "", "---", "**Sources**", ""]
    seen = set()
    for title, url in citations:
        if url in seen:
            continue
        seen.add(url)
        lines.append(f"- [{title}]({url})")
    return "\n".join(lines)


def with_search_context(messages, search_context):
    """Append the search results as the final turn, after everything else.

    Position is the whole point. A conversation with web search on runs a fresh search
    every turn, so these bytes differ each time and nothing after them can be cached.
    They started in the system message, at the very front, which meant every follow-up
    changed byte one of the request and invalidated the cached prefix for the whole
    conversation - the capture included. That capture is the expensive part of an image
    query, and it was being re-sent and re-billed on every turn.

    Last is the only position that keeps the image inside the shared prefix. Before the
    final user turn is not good enough: on turn one the block would sit ahead of the
    capture and on turn two behind it, so the two requests diverge at the capture again.
    Put it at the end and every turn agrees on everything up to it.

    Kept as its own system turn rather than folded into the user's message: the results
    are material to answer from, not something the user said. Measured, that distinction
    changes answers - asked about a fact only the results carried, Sonnet used it from a
    system turn and argued with it when the same text was appended to the user's.
    """
    if not search_context:
        return messages
    return messages + [{"role": "system",
                        "content": f"{SEARCH_CONTEXT_HEADER}\n\n{search_context}"}]


def strip_internal_keys(messages):
    """Drop our own bookkeeping keys (is_premium, image) before sending upstream."""
    allowed = ("role", "content", "name")
    return [{k: v for k, v in m.items() if k in allowed} for m in messages]


class LLMClient:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }

    @staticmethod
    def _error_text(response):
        """Pull the provider's actual message out of an error response."""
        try:
            body = response.json()
        except ValueError:
            snippet = (response.text or "").strip()
            return snippet[:400] or f"HTTP {response.status_code}"
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str):
                return err
            if body.get("message"):
                return str(body["message"])
        return json.dumps(body)[:400]

    def _encode(self, payload):
        """Serialise the body, gzipped once it is big enough to be worth it.

        Every turn resends the whole conversation, and a capture is a base64 PNG, so
        bodies here run to hundreds of kilobytes. The body must finish uploading before
        the model is dispatched, so compressing it comes straight off time-to-first-token
        - JSON like this compresses about 5:1. Below the threshold it would be ceremony.

        https://docs.nano-gpt.com/api-reference/miscellaneous/request-compression
        """
        raw = json.dumps(payload).encode("utf-8")
        headers = self._headers()
        if len(raw) < GZIP_THRESHOLD_BYTES:
            return raw, headers
        headers["Content-Encoding"] = "gzip"
        return gzip.compress(raw, GZIP_LEVEL), headers

    def _post(self, payload, stream, cancel=None):
        last_error = None
        for attempt in range(MAX_RETRIES):
            if cancel is not None and cancel.is_set():
                return None
            try:
                body, headers = self._encode(payload)
                response = self.session.post(
                    self.config.api_url,
                    headers=headers,
                    data=body,
                    stream=stream,
                    timeout=(CONNECT_TIMEOUT, self.config.request_timeout),
                )
            except requests.exceptions.ConnectTimeout:
                last_error = "Connection timed out"
            except requests.exceptions.Timeout:
                # The request arrived and the answer did not. Unlike a refused
                # connection or a 503 that is not evidence it was never run, so trying
                # again can bill the same completion twice - and at the default timeout
                # three attempts is nine minutes of "Thinking…".
                raise LLMError(
                    f"No response within {self.config.request_timeout} seconds",
                    model=payload.get("model"))
            except requests.exceptions.RequestException as e:
                last_error = str(e)
            else:
                if response.status_code < 400:
                    return response
                detail = self._error_text(response)
                last_error = f"HTTP {response.status_code}: {detail}"
                response.close()
                if response.status_code not in RETRY_STATUS:
                    # 401/404 etc: retrying cannot help
                    raise LLMError(last_error, model=payload.get("model"))

            if attempt < MAX_RETRIES - 1:
                # Exponential backoff, interruptible so Cancel stays responsive.
                delay = 1.5 * (2 ** attempt)
                if cancel is not None:
                    if cancel.wait(delay):
                        return None
                else:
                    time.sleep(delay)

        raise LLMError(last_error or "Request failed", model=payload.get("model"))

    def _payload(self, model, messages, image_base64=None, stream=False,
                 web_search=None, system=None, search_context=None, max_tokens=None):
        msgs = attach_image(strip_internal_keys(messages), image_base64)
        head = system_message(system)
        if head:
            msgs = [head] + msgs
        msgs = with_search_context(msgs, search_context)
        payload = {
            "model": model,
            "messages": msgs,
            "stream": stream,
        }
        limit = max_tokens or self.config.max_tokens
        # How hard to think, for a model that always reasons: see models.effort_for.
        # Spelled {"reasoning": {"effort": ...}}, which nano-gpt's endpoint honours
        # (measured: TEE/glm-5.3's reasoning went from 548 tokens to 1 at "low") and
        # sealed.shape_body turns into each private/ family's own field.
        effort = catalogue.effort_for(self.config, model)
        if effort:
            payload["reasoning"] = {"effort": effort}
            if limit:
                limit += REASONING_ROOM.get(effort, 4096)
        if limit:
            payload["max_tokens"] = limit
        return payload

    @staticmethod
    def _ending(text, finish_reason, model, limit):
        """The reply as it should be kept, given how it ended.

        A reply that stopped at the token limit says so, rather than passing for a
        whole answer. One that stopped there before saying anything was all thinking,
        and is an error with the way out named, not "an empty response".
        """
        if finish_reason != "length":
            return text
        if not (text or "").strip():
            raise LLMError(
                f"{model} spent its whole allowance of {limit} tokens thinking and never "
                f"began its answer. Lower Reasoning effort, or raise Max response tokens, "
                f"in Settings > Advanced.", model=model)
        return text + CUT_OFF_NOTE

    def craft_query(self, material, cancel=None):
        """Turn the question and its material into something worth searching for.

        Pasting the raw turn into a search box is how a stack trace returns nothing: the
        local file paths and process ids are noise, and "what is going wrong here" is not
        a subject. Measured, that exact query failed outright while the exception name
        and the identifiers found the answer. A cheap text model does this well.

        Returns None if it cannot, and the caller keeps the mechanical query.
        """
        material = (material or "").strip()
        if not material:
            return None
        try:
            answer = self.complete(
                self.config.text_model,
                [{"role": "user", "content": prompts.SEARCH_QUERY + material}],
                cancel=cancel, max_tokens=QUERY_TOKENS)
        except LLMError as e:
            print(f"[search] query not crafted: {e}")
            return None

        # One line, and nothing long enough to be an explanation.
        query = " ".join((answer or "").strip().splitlines()[:1]).strip()
        query = _balance_quotes(query.strip("'"))
        if not query or len(query) > websearch.MAX_QUERY_CHARS:
            return None
        return query

    def search(self, messages, web_search=None, cancel=None):
        """Run the web search for this turn. Returns (context, [(title, url)]).

        Failure is not fatal: a search that cannot run leaves the model answering from
        what it already has, which is what it would have done anyway.
        """
        if not web_search_active(self.config, web_search):
            return "", []
        if cancel is not None and cancel.is_set():
            return "", []

        query = websearch.query_from(messages)
        if query and getattr(self.config, "web_search_craft_query", True):
            # Crafted from the whole turn, not from the trimmed query: the material is
            # what carries the exception name or the distinctive phrase.
            query = self.craft_query(websearch.material_from(messages), cancel) or query
        if cancel is not None and cancel.is_set():
            return "", []
        try:
            results = websearch.run(query, self.config, session=self.session)
        except websearch.SearchError as e:
            print(f"[search] {e}")
            return "", []
        return (websearch.as_context(query, results),
                [(result["title"], result["url"]) for result in results])

    def complete(self, model, messages, image_base64=None, cancel=None,
                 web_search=None, system=None, max_tokens=None, privacy=None, status=None):
        """Non-streaming completion. Returns the text, or raises LLMError.

        With `privacy` (a PrivacySession), the request goes only to that conversation's
        attested models, by way of _private_call.
        """
        if privacy is not None:
            return self._private_call(model, messages, image_base64, cancel, None, system,
                                      privacy, status, max_tokens)
        payload = self._payload(model, messages, image_base64, False, web_search, system,
                                max_tokens=max_tokens)
        response = self._post(payload, False, cancel)
        if response is None:
            return None
        try:
            body = response.json()
        except ValueError:
            raise LLMError("Provider returned a non-JSON response", model=model)
        finally:
            response.close()
        try:
            choice = body["choices"][0]
            return self._ending(choice["message"]["content"] or "",
                                choice.get("finish_reason"), model,
                                payload.get("max_tokens"))
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Unexpected response shape: {json.dumps(body)[:300]}",
                           model=model)

    @staticmethod
    def _consume(lines, cancel, on_delta, citations):
        """Read an SSE stream. Returns {text, id, finish_reason, done}, or None if it
        was cancelled part way.

        `lines` may be bytes (requests) or str (httpx, for the sealed stream). The
        completion's id is kept because a TEE reply's signature is filed under it.
        """
        text, response_id, finish_reason, done = "", None, None, False
        for raw in lines:
            if cancel is not None and cancel.is_set():
                return None
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            line = line[5:].strip()
            if line == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            if response_id is None and isinstance(chunk.get("id"), str):
                response_id = chunk["id"]
            # Search metadata arrives in a frame of its own, where `choices` is
            # empty - skipping those was throwing the sources away unread.
            for source in _extract_citations(chunk):
                if source not in citations:
                    citations.append(source)
            choices = chunk.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                text += piece
                if on_delta:
                    on_delta(piece, text)
            for source in (_extract_citations(delta)
                           + _extract_citations(choices[0])
                           + _extract_citations(choices[0].get("message"))):
                if source not in citations:
                    citations.append(source)
        return {"text": text, "id": response_id, "finish_reason": finish_reason,
                "done": done}

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None, privacy=None, status=None):
        """Streaming completion.

        on_delta(chunk_text, full_text_so_far) is called for every token chunk, so the UI
        can render tokens live.

        With `privacy` (a PrivacySession) the request goes to that conversation's
        attested models only, and never searches the web: see _private_call.
        """
        if privacy is not None:
            return self._private_call(model, messages, image_base64, cancel, on_delta,
                                      system, privacy, status)

        # Ours to run, before the model is called, so the query is the user's question
        # rather than whatever the chat endpoint would have derived from the request.
        search_context, citations = self.search(messages, web_search, cancel)
        if cancel is not None and cancel.is_set():
            return None

        payload = self._payload(model, messages, image_base64, True, web_search, system,
                                search_context)
        response = self._post(payload, True, cancel)
        if response is None:
            return None

        try:
            result = self._consume(response.iter_lines(), cancel, on_delta, citations)
        finally:
            response.close()
        if result is None:
            return None

        text = self._ending(result["text"].strip(), result["finish_reason"], model,
                            payload.get("max_tokens"))
        # The sources are the ones we searched for, so the footer is built from what we
        # know rather than from what the model remembers reading. Any the response
        # happens to carry as well are folded in above.
        if (text and citations and "## Sources" not in text
                and getattr(self.config, "web_search_citations", True)):
            text += format_citations(citations)
        return text

    def _private_call(self, model, messages, image_base64, cancel, on_delta, system,
                      privacy, status=None, max_tokens=None):
        """One request in a private conversation. Returns the text, or None if cancelled.

        In order, and each step before anything reaches the model:
          - the model must be one of the conversation's own - a TEE/ or private/ model
            fixed when it started - or nothing is sent;
          - its enclave is attested (privacy.PrivacySession.ensure), or nothing is sent;
          - a private/ model's body is sealed to that enclave's key (sealed.py) and
            never goes anywhere unsealed; a TEE/ model's goes to the ordinary endpoint.
        Afterwards the reply is checked - a TEE reply's signature against the attested
        key - and the receipt left on the session for the window to show.

        The web is never searched: the query, crafted by a model or not, is made from
        the conversation and would leave the enclave in the clear, bound for a search
        provider this app cannot attest.
        """
        say = status or (lambda _message: None)
        kind = privacy_module.kind_of(model)
        if kind is None or not privacy.allows(model):
            raise LLMError(f"{model} is not one of this private conversation's TEE "
                           f"models; nothing was sent", model=model)
        try:
            attestation = privacy.ensure(model)
        except PrivacyError as e:
            raise LLMError(str(e), model=model)
        if cancel is not None and cancel.is_set():
            return None
        say(f"{attestation.headline} ✓ — sending to {model}…")

        payload = self._payload(model, messages, image_base64, True, system=system,
                                max_tokens=max_tokens)
        if kind == privacy_module.E2EE:
            try:
                client = privacy.sealed_client()
                with client.request(model, payload) as response:
                    privacy.note_sent(model)
                    say("Receiving the sealed reply…")
                    result = self._consume(client.lines(response), cancel, on_delta, [])
                if result is None:
                    return None
                client.finish(result["finish_reason"], result["done"])
            except ModelRetired as e:
                # Gone since it was attested: the window offers what is listed instead.
                privacy.note_retired(e)
                raise LLMError(str(e), model=model)
            except PrivacyError as e:
                raise LLMError(f"{e}. Private Mode: nothing was sent in the clear",
                               model=model)
        else:
            response = self._post(payload, True, cancel)
            if response is None:
                return None
            privacy.note_sent(model)
            say("Receiving from the attested enclave…")
            try:
                result = self._consume(response.iter_lines(), cancel, on_delta, [])
            finally:
                response.close()
            if result is None:
                return None
            say("Checking the reply's signature…")
        privacy.check_reply(model, result["id"])
        return self._ending(result["text"].strip(), result["finish_reason"], model,
                            payload.get("max_tokens"))

    def test_connection(self):
        """Used by the Settings 'Test connection' button. Returns (ok, message).

        With Privacy Mode on, it is the private text model that is tried, attested and
        checked exactly as a conversation would be: that is the model every request
        will go to.
        """
        privacy = None
        try:
            privacy = privacy_module.PrivacySession.start(self.config)
            model = privacy.text_model if privacy else self.config.active_text_model()
            result = self.complete(
                model,
                [{"role": "user", "content": "Reply with the single word: ok"}],
                privacy=privacy,
            )
        except LLMError as e:
            return False, f"{e.model}: {e}" if e.model else str(e)
        except PrivacyError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        reply = f"Model replied: {(result or '').strip()[:80]}"
        if privacy is None:
            return True, reply
        attestation = privacy.attestations.get(model)
        receipt = privacy.pop_receipt()
        badge = privacy_module.receipt_badge(receipt)
        return True, (f"{reply} — {attestation.headline if attestation else ''}"
                      f"{', ' + badge[0] if badge else ''}")
