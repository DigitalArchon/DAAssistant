"""The providers a model can be asked of.

Three by name, whose addresses and habits the app knows, and one typed in by hand:

    NanoGPT      the default. The only one with TEE/ and private/ models, so Privacy Mode
                 is its alone, and the only one that runs the web search.
    OpenRouter   https://openrouter.ai, with a key of its own.
    Ollama       a server on this machine or the local network; no key.
    Other        any OpenAI-compatible endpoint, by its address.

Each of the three model settings - text, premium, vision - names its provider beside its
model, so OCR can run on the machine next door while the answers come from elsewhere.

What differs between them, and is decided here rather than wherever a request is made:
where the chat endpoint and the model list are, whether a key is sent, whether a request
body may be gzipped, and what a model has to be told to stop.
"""

from urllib.parse import urlparse, urlunparse

from . import netguard

NANOGPT, OPENROUTER, OLLAMA, CUSTOM = "nanogpt", "openrouter", "ollama", "custom"

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
                 placeholder=""):
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


KINDS = {kind.id: kind for kind in (
    Kind(NANOGPT, "NanoGPT", url_key="api_url", key_key="api_key", needs_key=True,
         gzip=True, listing="nanogpt",
         note="Also runs Privacy Mode and the web search."),
    Kind(OPENROUTER, "OpenRouter", key_key="openrouter_api_key",
         fixed_url=OPENROUTER_URL, needs_key=True, listing="openrouter"),
    Kind(OLLAMA, "Ollama", url_key="ollama_url", listing="ollama",
         placeholder="http://localhost:11434",
         note="A server on this machine or your network. No key."),
    Kind(CUSTOM, "Other OpenAI-compatible endpoint", url_key="custom_url",
         key_key="custom_api_key", short="Other endpoint",
         placeholder="https://api.example.com/v1",
         note="Any server that speaks /v1/chat/completions. The key is optional."),
)}
ORDER = (NANOGPT, OPENROUTER, OLLAMA, CUSTOM)


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

    def __init__(self, kind, url, api_key):
        self.kind = kind
        self.id, self.name, self.gzip, self.listing = (kind.id, kind.name, kind.gzip,
                                                       kind.listing)
        self.address = (url or "").strip()          # as typed
        self.chat_url = chat_url(kind, url)
        self.api_key = (api_key or "").strip()

    @property
    def models_url(self):
        """Where its model list is read from."""
        if self.id == OLLAMA:
            return _root(self.chat_url) + "/api/tags"
        return models_url(self.chat_url)

    def url_problem(self):
        """Why its address may not be used, or None: not set, or not https (netguard)."""
        if not self.address and not self.kind.fixed_url:
            return f"No address is set for {self.name}. Add it in Settings > Providers."
        return netguard.problem(self.chat_url)

    def key_problem(self):
        if self.kind.needs_key and not self.api_key:
            return f"No API key is set for {self.name}. Add it in Settings > Providers."
        return None

    def problem(self):
        """Why nothing can be sent to it, or None."""
        return self.url_problem() or self.key_problem()

    def headers(self):
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
    NanoGPT: a config.json edited by hand does not get to send anything anywhere new."""
    kind = KINDS.get(provider) or KINDS[NANOGPT]
    return Provider(kind,
                    getattr(config, kind.url_key, "") if kind.url_key else "",
                    getattr(config, kind.key_key, "") if kind.key_key else "")


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
    """The providers the three model settings name, each once, in ORDER. NanoGPT is
    among them when Privacy Mode is on, whatever the settings say."""
    used = {role_provider(config, role) for role in ROLES}
    if getattr(config, "privacy_mode", False):
        used.add(NANOGPT)
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
        found = provider.url_problem()
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
    if port == OLLAMA_PORT:
        return OLLAMA, _root(api_url)
    return CUSTOM, api_url.strip()
