"""The providers a model can be asked of.

Six by name, whose addresses and habits the app knows, and one typed in by hand:

    NanoGPT      the default. Its TEE/ and private/ models are Privacy Mode's, and it
                 is the only one that runs the web search.
    OpenRouter   https://openrouter.ai, with a key of its own.
    Anthropic    Anthropic's own Messages API (anthropic.py), with its own key.
    Chutes       https://llm.chutes.ai/v1 (or its proxy of Anthropic's API,
                 claude.chutes.ai): every model Chutes lists, its -TEE ones always sent
                 end-to-end encrypted to an attested instance (chutes.py), the others in
                 the clear. Preliminary.
    Tinfoil      Tinfoil's own API, every model end-to-end encrypted to its attested
                 router (tinfoil_direct.py). Preliminary.
    Ollama       a server on this machine or the local network; no key.
    Other        any OpenAI-compatible endpoint, by its address.

What an endpoint is, and so how a request to it is made, is read from its address
(services.py), not from the name it was set up under: Tinfoil's address typed in as
Other, or an older config's, is Tinfoil's all the same - sealed, its key bound to its
origin, its models marked as the highest privacy - and Chutes' or Anthropic's is theirs
(Provider.protocol, Provider.service).

Each of the three model settings - text, premium, vision - names its provider beside its
model, so OCR can run on the machine next door while the answers come from elsewhere.

What differs between them, and is decided here rather than wherever a request is made:
where the chat endpoint and the model list are, whether a key is sent, whether a request
body may be gzipped, and what a model has to be told to stop.
"""

from urllib.parse import urlparse as _urlparse, urlunparse

from . import netguard, services


def urlparse(url):
    """urllib's parse of `url`, or of nothing when it cannot be read at all: a half-typed
    "http://[fd00::5" raises, and what to say about it is netguard's to decide."""
    try:
        return _urlparse(url)
    except ValueError:
        return _urlparse("")

NANOGPT, OPENROUTER, OLLAMA, CUSTOM = "nanogpt", "openrouter", "ollama", "custom"
ANTHROPIC, CHUTES, TINFOIL = "anthropic", "chutes", "tinfoil"

NANOGPT_URL = "https://nano-gpt.com/api/v1/chat/completions"
NANOGPT_HOSTS = frozenset({"nano-gpt.com"})
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_DEFAULT_URL = "http://localhost:11434"
OLLAMA_PORT = 11434

CHAT_SUFFIXES = ("/chat/completions", "/completions", "/responses")

# role -> (the setting holding its model, the setting holding its provider)
ROLES = {"text": ("text_model", "text_provider"),
         "premium": ("premium_text_model", "premium_text_provider"),
         "vision": ("vision_model", "vision_provider")}

# Ollama does not stop glm-ocr when it has finished (0.30 to 0.40.2 at least; the model's
# end-of-turn token, <|user|>, is not one Ollama counts as an end). It transcribes the
# image and carries on to the token limit - minutes of it, from `ollama run` as much as
# through the API. That token is a control token and never reaches the text, so it
# cannot be the stop itself; what follows it can, when it is one of these. Measured on
# 0.40.2, glm-ocr and glm-ocr:q8_0, with this app's prompts - after the transcription
# comes, from one run to the next:
#
#     prose      "```markdown" and the prose again, then fences; or the prose again and
#                again with nothing between, or "---"
#     code       a second "```" under the code block's closing fence, then fences
#     a table    the same table again as "<table ...>", over and over
#
# Each stop below is the start of one of those, and none is the start of a reply: "```"
# alone would be, for a screenshot of code, and returned nothing at all. A stop is not
# part of the reply, so the code block's own closing fence goes with it: llm.tidy_end
# puts it back. The prose that simply starts again has nothing to stop at, and nor has
# one line said over and over ("NO TEXT FOUND", 660 times) or a few said round and
# round, so a model listed here is also watched as it streams and cut off when it does
# any of those (llm.runaway_start).
#
# A workaround, and only for what was measured. The cure is the model's: an Ollama
# that stops it, or a GGUF whose metadata names <|user|> as its end-of-turn token.
OLLAMA_RUNAWAYS = (("glm-ocr", ["```markdown", "```\n```", "\n<table"]),)


class Kind:
    """One of the providers above, before any settings are applied to it."""

    def __init__(self, id, name, url_key=None, key_key=None, fixed_url=None,
                 needs_key=False, gzip=False, listing="openai", note="", short=None,
                 placeholder="", protocol="auto", default_url=""):
        self.id = id
        self.name = name
        self.short = short or name      # for a drop-down beside a model
        self.placeholder = placeholder  # what an address for it looks like
        self.url_key = url_key          # the setting holding its address, if it has one
        self.key_key = key_key          # the setting holding its API key, if it takes one
        self.fixed_url = fixed_url
        self.needs_key = needs_key
        # Request bodies gzipped once they are large (llm.GZIP_THRESHOLD_BYTES). Only
        # where it is known to be read: Ollama answers a gzipped body with HTTP 400,
        # "invalid character '\x1f' looking for beginning of value".
        self.gzip = gzip
        self.listing = listing          # how its model list is read: see models.fetch
        self.note = note
        # The protocol it speaks unless its address says otherwise (services.py).
        self.protocol = protocol
        # The address filled in for it, which may be changed.
        self.default_url = default_url


KINDS = {kind.id: kind for kind in (
    Kind(NANOGPT, "NanoGPT", url_key="api_url", key_key="api_key", needs_key=True,
         gzip=True, listing="nanogpt",
         note="Also runs Privacy Mode and the web search."),
    Kind(OPENROUTER, "OpenRouter", key_key="openrouter_api_key",
         fixed_url=OPENROUTER_URL, needs_key=True, listing="openrouter"),
    Kind(ANTHROPIC, "Anthropic", url_key="anthropic_url", key_key="anthropic_api_key",
         needs_key=True, listing="anthropic", protocol="anthropic",
         default_url=services.ANTHROPIC_URL, placeholder=services.ANTHROPIC_URL,
         note="Anthropic's own Messages API. Not private: Anthropic reads your text."),
    Kind(CHUTES, "Chutes", url_key="chutes_url", key_key="chutes_api_key",
         needs_key=True, listing="chutes", default_url=services.CHUTES_BASE_URL,
         placeholder=services.CHUTES_BASE_URL,
         note="Preliminary. Its -TEE models are always end-to-end encrypted to an "
              "attested instance (partial privacy); its other models are not private. "
              "https://claude.chutes.ai speaks Anthropic's API instead."),
    Kind(TINFOIL, "Tinfoil", url_key="tinfoil_url", key_key="tinfoil_api_key",
         needs_key=True, listing="tinfoil", protocol="tinfoil",
         default_url=services.TINFOIL_BASE_URL, placeholder=services.TINFOIL_BASE_URL,
         note="Preliminary. Every model end-to-end encrypted to Tinfoil's attested "
              "router (the highest privacy). Usable in Privacy Mode too."),
    Kind(OLLAMA, "Ollama", url_key="ollama_url", listing="ollama",
         placeholder="http://localhost:11434",
         note="A server on this machine or your network. No key."),
    Kind(CUSTOM, "Other OpenAI-compatible endpoint", url_key="custom_url",
         key_key="custom_api_key", short="Other endpoint",
         placeholder="https://api.example.com/v1",
         note="Any server that speaks /v1/chat/completions. The key is optional."),
)}
ORDER = (NANOGPT, OPENROUTER, ANTHROPIC, CHUTES, TINFOIL, OLLAMA, CUSTOM)


class Model(str):
    """A model id that carries the provider it is asked of.

    A str, so everything that labels, compares or stores a model goes on working; the
    provider rides along to where the request is made. A plain str is looked up among
    the three model settings instead: see provider_id.
    """

    def __new__(cls, model_id, provider):
        self = super().__new__(cls, model_id)
        self.provider = provider if provider in KINDS else NANOGPT
        return self


def models_url(api_url):
    """Derive the models endpoint from a chat endpoint."""
    parsed = urlparse(api_url)
    path = parsed.path
    for suffix in CHAT_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    else:
        path = path.rsplit("/", 1)[0] if "/" in path else path
    return urlunparse(parsed._replace(path=path.rstrip("/") + "/models", query="",
                                      fragment=""))


def _root(url):
    """scheme://host:port of `url`, whatever path it was typed with."""
    parsed = urlparse((url or "").strip())
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def chat_url(kind, url):
    """The chat-completions endpoint for an address as typed.

    An Ollama address is its server, and any path typed after it - /v1, /api/chat - is
    dropped. Another endpoint's is taken as the API's base unless it already names the
    endpoint: https://host/v1 and https://host/v1/chat/completions are the same place.
    Nothing is done to the scheme or the host: netguard judges those as written.
    """
    text = (url or "").strip()
    if kind.fixed_url:
        return kind.fixed_url
    if not text or kind.id == NANOGPT:
        return text
    # Read from the address, whatever the provider was set up as: Anthropic's API and
    # Chutes' proxy of it take /v1/messages, Tinfoil's /v1/chat/completions under its
    # /v1.
    protocol = services.chat_protocol(text, kind.protocol)
    if protocol == "anthropic" and urlparse(text).netloc:
        from . import anthropic
        return anthropic.messages_url(text)
    if protocol == "tinfoil" and urlparse(text).netloc:
        from . import tinfoil_direct
        return tinfoil_direct.base_of(text) + "/chat/completions"
    if kind.id == OLLAMA:
        root = _root(text)
        return root + "/v1/chat/completions" if urlparse(root).netloc else text
    parsed = urlparse(text)
    if not parsed.netloc:
        return text
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1/chat/completions"
    elif not path.endswith(CHAT_SUFFIXES):
        path += "/chat/completions"
    return urlunparse(parsed._replace(path=path, query="", fragment=""))


class Provider:
    """A provider as these settings have it: where it is, and with what key."""

    def __init__(self, kind, url, api_key, proxy=None, withheld=None):
        self.kind = kind
        self.id, self.name, self.gzip = kind.id, kind.name, kind.gzip
        self.address = (url or "").strip()          # as typed
        self.chat_url = chat_url(kind, url)
        # What its address says it is (services.py), however it was set up: Tinfoil's,
        # Chutes', Anthropic's, or "" for another; and the protocol it is spoken in.
        self.service = services.service_of(self.chat_url)
        self.protocol = services.chat_protocol(self.chat_url, kind.protocol) \
            if kind.id != NANOGPT or self.service else "openai"
        self.listing = {services.TINFOIL: "tinfoil", services.CHUTES: "chutes"}.get(
            self.service) or ("anthropic" if self.protocol == "anthropic" else kind.listing)
        if self.service and self.gzip:
            self.gzip = False       # NanoGPT's habit, not theirs
        # Why the key kept for it is not sent to this address (Config.key_withheld): it
        # was saved for another. Then there is no key here, and nothing is sent at all,
        # rather than sent without it.
        self.withheld = withheld
        self.api_key = "" if withheld else (api_key or "").strip()
        # The proxy these settings name, which an onion address is judged by: one typed
        # in Settings beside it, not yet in force, counts.
        self.proxy = (proxy or "").strip()

    @property
    def models_url(self):
        """Where its model list is read from."""
        if self.id == OLLAMA:
            return _root(self.chat_url) + "/api/tags"
        if self.listing == "chutes":
            return "https://llm.chutes.ai/v1/models"     # Chutes' public list
        if self.listing == "tinfoil":
            from . import tinfoil_direct
            return tinfoil_direct.base_of(self.chat_url) + "/models"
        if self.listing == "anthropic":
            from . import anthropic
            return anthropic.origin(self.chat_url) + "/v1/models"
        return models_url(self.chat_url)

    @property
    def sealed(self):
        """Whether every request to it must be sealed end to end (Tinfoil's): never
        sent by the plain client."""
        return self.protocol == "tinfoil"

    def sealed_model(self, model):
        """Whether `model`, asked of this provider, is only ever sent sealed: every
        model of Tinfoil's, a Chutes -TEE model on Chutes' hosts, a private/ one."""
        from . import levels
        return levels.privacy_at(model, self.chat_url, self.protocol).sealed

    def url_problem(self):
        """Why its address may not be used, or None: not set, or not https (netguard),
        or a Tor onion service with no way to reach one."""
        if not self.address and not self.kind.fixed_url:
            return f"No address is set for {self.name}. Add it in Settings > Providers."
        found = netguard.problem(self.chat_url)
        if found is None and self.service == services.TINFOIL:
            # Its key goes to Tinfoil's API alone, and the attestation names that host.
            found = services.tinfoil_key_refusal(self.chat_url)
        if found is None and netguard.is_onion(urlparse(self.chat_url).hostname or ""):
            from . import proxy
            if not proxy.reaches_onion(self.chat_url, self.proxy):
                return (f"{netguard.shown(self.address)} is a Tor onion service, "
                        f"reached only through Tor: set Settings > Advanced > Proxy to "
                        f"its SOCKS port, socks5h://127.0.0.1:9050 as a rule.")
        return found

    def key_problem(self):
        if self.withheld:
            return self.withheld
        if self.kind.needs_key and not self.api_key:
            return f"No API key is set for {self.name}. Add it in Settings > Providers."
        return netguard.key_problem(self.api_key, self.name)

    def problem(self):
        """Why nothing can be sent to it, or None."""
        return self.url_problem() or self.key_problem()

    def headers(self, cache_ttl=None):
        if self.protocol == "anthropic":
            from . import anthropic
            return anthropic.headers(self.chat_url, self.api_key, cache_ttl)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def stops(self, model):
        """What `model` has to be told to stop at here, or None for a model that stops
        by itself - all but the few in OLLAMA_RUNAWAYS."""
        if self.id != OLLAMA:
            return None
        name = (model or "").lower().rsplit("/", 1)[-1]
        for family, stops in OLLAMA_RUNAWAYS:
            if name == family or name.startswith(family + ":"):
                return list(stops)
        return None


def get(config, provider):
    """The Provider for a provider id, from these settings. One it does not know is
    NanoGPT: a config.json edited by hand does not get to send anything anywhere new.
    Nor does an address: the key goes only where it was saved for (key_withheld)."""
    kind = KINDS.get(provider) or KINDS[NANOGPT]
    reasons = []
    for check, name in (("key_withheld", kind.key_key), ("key_in_clear_withheld", kind.key_key),
                        ("address_withheld", kind.url_key)):
        found = getattr(config, check, None)
        if name and callable(found):
            reasons.append(found(name))
    # And a key, or a password in its address, that would go over plain http to another
    # machine before it is confirmed for it (Config.key_in_clear): nothing is sent.
    return Provider(kind,
                    (getattr(config, kind.url_key, "") or kind.default_url) if kind.url_key
                    else "",
                    getattr(config, kind.key_key, "") if kind.key_key else "",
                    getattr(config, "proxy", ""),
                    withheld=next((reason for reason in reasons if reason), None))


def role_provider(config, role):
    """The provider id chosen for a role: "text", "premium" or "vision"."""
    chosen = getattr(config, ROLES[role][1], NANOGPT)
    return chosen if chosen in KINDS else NANOGPT


def role_model(config, role):
    """The role's model, with its provider."""
    return Model((getattr(config, ROLES[role][0], "") or "").strip(),
                 role_provider(config, role))


def provider_id(config, model):
    """Which provider `model` is asked of.

    The one it carries, when it is a Model. A plain id is one of the three model
    settings' or it is NanoGPT's: a TEE/ or private/ model, or one named in passing.
    """
    carried = getattr(model, "provider", None)
    if carried in KINDS:
        return carried
    for role in ROLES:
        if model and getattr(config, ROLES[role][0], None) == model:
            return role_provider(config, role)
    return NANOGPT


def of(config, model):
    """The Provider `model` is asked of."""
    return get(config, provider_id(config, model))


def where(config, model):
    """' · Ollama' for a model asked of a provider other than NanoGPT, else '': how a
    model is named wherever its provider is worth saying."""
    provider = provider_id(config, model)
    return "" if provider == NANOGPT else f" · {KINDS[provider].short}"


def in_use(config):
    """The providers the three model settings name, each once, in ORDER. With Privacy
    Mode on, those its private models are of: NanoGPT's TEE/ and private/ ones, and
    Tinfoil's or Chutes' by their references ("Tinfoil::glm-5-3")."""
    used = {role_provider(config, role) for role in ROLES}
    if getattr(config, "privacy_mode", False):
        from . import privacy
        for key in ("private_text_model", "private_premium_model", "private_vision_model"):
            service = privacy.service_of(getattr(config, key, "") or "")
            used.add(service if service in KINDS else NANOGPT)
    return [get(config, provider) for provider in ORDER if provider in used]


def unready(config):
    """Why a request cannot be made with these settings, or None: the first provider in
    use that has no address, an address that may not be used, or no key."""
    for provider in in_use(config):
        found = provider.problem()
        if found:
            return found
    return None


def refused(config):
    """Why these settings may not be saved, or None: an address in use that is missing,
    or that would send a key and a screenful of text over plain http. An address typed
    for a provider nothing uses is judged too, once there is one - it is one click from
    being used."""
    for provider in in_use(config):
        found = provider.url_problem() or netguard.key_problem(provider.api_key,
                                                                provider.name)
        if found:
            return found
    for kind_id in ORDER:
        provider = get(config, kind_id)
        if provider.address and netguard.problem(provider.chat_url):
            return netguard.problem(provider.chat_url)
    return None


def legacy(api_url):
    """Where a config written before providers had names sent everything: (provider id,
    the address to keep for it). Its one API URL was NanoGPT's unless it was changed."""
    parsed = urlparse((api_url or "").strip())
    try:
        host, port = (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        host, port = "", None
    if not host or host in NANOGPT_HOSTS or host.endswith(".nano-gpt.com"):
        return NANOGPT, None
    if host == "openrouter.ai":
        return OPENROUTER, None
    # Anthropic's, Chutes' and Tinfoil's, read from the address (services.service_of):
    # their own providers, the address kept as it was.
    found = {services.ANTHROPIC: ANTHROPIC, services.CHUTES: CHUTES,
             services.TINFOIL: TINFOIL}.get(services.service_of(api_url))
    if found:
        return found, api_url.strip()
    if port == OLLAMA_PORT:
        return OLLAMA, _root(api_url)
    return CUSTOM, api_url.strip()
