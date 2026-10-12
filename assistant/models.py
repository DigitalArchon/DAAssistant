"""Model catalogue: fetch, cache and search the provider's model list.

The provider exposes the plain OpenAI-shaped list at /v1/models, but that carries only
id/object/created/owned_by. Pricing, context length and capability flags come from
`?detailed=true`, so that is what we ask for, falling back to the plain list if the
detailed variant is not supported by whatever endpoint the user has configured.

Results are cached on disk because the detailed list is ~650 KB and rarely changes.
"""

import copy
import json
import os
import time
from urllib.parse import urlparse, urlunparse

import requests

from . import netguard, privacy

CACHE_DIR = os.path.expanduser("~/.cache/da-assistant")
CACHE_FILE = os.path.join(CACHE_DIR, "models.json")
CACHE_TTL = 6 * 3600
# Bumped when what is cached changes shape or scope: 2 added the private/ models, which
# an older cache would otherwise hide for up to CACHE_TTL; 3 the reasoning efforts.
CACHE_VERSION = 3

# nano-gpt's end-to-end encrypted models are listed apart from the rest, by the Private
# Mode relay, and by id alone: no prices, context lengths or capabilities.
RELAY_MODELS_PATH = "/api/v1/private/tinfoil/models"
# Each is the same model nano-gpt sells as TEE/..., which is also what it bills as, plus
# the published Private Mode markup (the proxy's README, Sept 2026).
PRIVATE_MODE_MARKUP = 1.05
_PRICE_FIELDS = ("prompt", "completion", "cacheReadInputPer1kTokens", "input_cache_read",
                 "input_cache_write")


def models_url(api_url):
    """Derive the models endpoint from the configured chat endpoint."""
    parsed = urlparse(api_url)
    path = parsed.path
    for suffix in ("/chat/completions", "/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    else:
        path = path.rsplit("/", 1)[0] if "/" in path else path
    return urlunparse(parsed._replace(path=path.rstrip("/") + "/models", query="",
                                      fragment=""))


def _price(entry, key):
    pricing = entry.get("pricing") or {}
    value = pricing.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise(entry):
    """Flatten one API entry into the shape the UI uses."""
    caps = entry.get("capabilities") or {}
    arch = entry.get("architecture") or {}
    inputs = arch.get("input_modalities") or []
    pricing = entry.get("pricing") or {}
    return {
        "id": entry.get("id") or "",
        "name": entry.get("name") or entry.get("id") or "",
        "description": entry.get("description") or "",
        "owned_by": entry.get("owned_by") or "",
        "category": entry.get("category") or "",
        "context_length": entry.get("context_length"),
        "max_output_tokens": entry.get("max_output_tokens"),
        "vision": bool(caps.get("vision") or "image" in inputs),
        "reasoning": bool(caps.get("reasoning")),
        "tool_calling": bool(caps.get("tool_calling")),
        # The reasoning levels the model accepts, as the provider lists them: e.g.
        # ["low", "high", "max"] for GLM 5.3. "none" among them marks a model that
        # thinks only when asked - see effort_for.
        "efforts": [str(e).lower() for e in entry.get("reasoning_efforts") or []],
        "prompt_price": _price(entry, "prompt"),
        "completion_price": _price(entry, "completion"),
        "price_unit": pricing.get("unit") or "",
        "currency": pricing.get("currency") or "USD",
        "aliases": list(entry.get("aliases") or []),
        "subscription": bool((entry.get("subscription") or {}).get("included")),
    }


def format_price(model):
    """'$0.08 / $0.50' per million tokens, or '' when the provider gave no pricing."""
    prompt, completion = model.get("prompt_price"), model.get("completion_price")
    if prompt is None and completion is None:
        return ""
    symbol = "$" if (model.get("currency") or "USD").upper() == "USD" else ""

    def fmt(value):
        if value is None:
            return "?"
        if value == 0:
            return "free"
        if value < 0.01:
            return f"{symbol}{value:.4f}".rstrip("0").rstrip(".")
        if value < 1:
            return f"{symbol}{value:.3f}".rstrip("0").rstrip(".")
        return f"{symbol}{value:,.2f}"

    return f"{fmt(prompt)} in / {fmt(completion)} out"


def format_context(model):
    n = model.get("context_length")
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M ctx".replace(".0M", "M")
    if n >= 1000:
        return f"{n // 1000}K ctx"
    return f"{n} ctx"


def _twin_key(model_id):
    """What a TEE/ id and its private/ twin share: TEE/glm-5.3 <-> private/glm-5-3."""
    return model_id.split("/", 1)[-1].lower().replace(".", "-")


def private_entries(private_ids, entries):
    """A raw entry for each private/ model, made from its TEE/ twin's when the listing
    has one - its capabilities and context, its prices times the markup - or else bare:
    listed, unpriced, capabilities unknown. From StoryLoom's private_catalog.py."""
    twins = {_twin_key(e["id"]): e for e in entries
             if isinstance(e.get("id"), str) and e["id"].startswith("TEE/")}
    made = []
    for model_id in private_ids:
        source = twins.get(_twin_key(model_id))
        entry = copy.deepcopy(source) if source is not None else {}
        entry["id"] = model_id
        entry["aliases"] = []
        name = entry.get("name") or model_id.split("/", 1)[-1]
        entry["name"] = f"{name} (end-to-end encrypted)"
        entry["description"] = privacy.E2EE_NOTE + (
            f" The same model as {source['id']}." if source is not None else "")
        pricing = entry.get("pricing")
        if isinstance(pricing, dict):
            for key in _PRICE_FIELDS:
                try:
                    number = float(pricing.get(key))
                except (TypeError, ValueError):
                    continue
                pricing[key] = number * PRIVATE_MODE_MARKUP
        made.append(entry)
    return made


def private_list_url(api_url):
    """Where the Private Mode relay lists its models: public, no key is sent."""
    parsed = urlparse(api_url)
    return f"{parsed.scheme}://{parsed.netloc}{RELAY_MODELS_PATH}"


def private_ids_in(body):
    """The private/ model ids in the relay's list, as JSON."""
    items = body.get("data") if isinstance(body, dict) else None
    return [item["id"] for item in items or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and item["id"].startswith("private/")]


def fetch_private_ids(api_url, timeout=30):
    """The Private Mode relay's model ids (a public list; no key is sent). [] on failure:
    the rest of the catalogue is still worth showing."""
    if netguard.problem(api_url):
        return []
    try:
        response = requests.get(private_list_url(api_url), timeout=timeout)
        body = response.json() if response.status_code < 400 else {}
    except (requests.exceptions.RequestException, ValueError):
        return []
    return private_ids_in(body)


def offered(model_id, private_ids):
    """Whether the relay's list still has `model_id`: nano-gpt retires Private Mode
    models (private/glm-5-3-flash, Oct 2026). Its dotted alias is the same model, a
    :thinking one is not. None when the list is empty - one that could not be read -
    which says nothing either way. From StoryLoom's private_catalog.py."""
    private_ids = list(private_ids)
    if not private_ids:
        return None
    key = _twin_key(model_id)
    return any(_twin_key(i) == key for i in private_ids)


def load_cache(max_age=CACHE_TTL, url=None):
    """The cached list, or None. `url` is the endpoint it has to have come from."""
    try:
        stat = os.stat(CACHE_FILE)
    except OSError:
        return None
    if max_age is not None and time.time() - stat.st_mtime > max_age:
        return None
    try:
        with open(CACHE_FILE) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    # One provider's catalogue is no use for choosing a model from another's.
    if url is not None and payload.get("url") != url:
        return None
    if payload.get("version") != CACHE_VERSION:
        return None
    return payload.get("models") or None


def save_cache(models, url):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": CACHE_VERSION, "url": url, "fetched": time.time(),
                       "models": models}, f)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass        # a cache failure must never break the browser


def fetch(config, force=False, timeout=60):
    """Return (models, from_cache). Raises RuntimeError with a readable message."""
    try:
        netguard.check(config.api_url)
    except netguard.InsecureURL as e:
        raise RuntimeError(str(e))
    url = models_url(config.api_url)
    if not force:
        cached = load_cache(url=url)
        if cached:
            return cached, True

    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}

    last_error = None
    # Detailed first - it is the only variant carrying pricing and capabilities.
    for params in ({"detailed": "true"}, None):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            continue
        if response.status_code >= 400:
            last_error = f"HTTP {response.status_code}"
            continue
        try:
            body = response.json()
        except ValueError:
            last_error = "provider returned a non-JSON model list"
            continue

        entries = body.get("data") if isinstance(body, dict) else body
        if not isinstance(entries, list):
            last_error = "unexpected model list shape"
            continue

        entries = [e for e in entries if isinstance(e, dict) and e.get("id")]
        if not entries:
            last_error = "provider returned an empty model list"
            continue
        if privacy.offers_private_mode(config.api_url):
            entries += private_entries(fetch_private_ids(config.api_url), entries)
        models = [normalise(e) for e in entries]

        models.sort(key=lambda m: m["id"].lower())
        save_cache(models, url)
        return models, False

    raise RuntimeError(f"Could not fetch models from {url}: {last_error}")


def search(models, query, vision_only=False, free_only=False, private_only=False):
    """Filter by capability, then by all whitespace-separated terms."""
    results = models
    if private_only:
        results = [m for m in results if privacy.kind_of(m["id"])]
    if vision_only:
        results = [m for m in results if m["vision"]]
    if free_only:
        results = [m for m in results if (m.get("prompt_price") or 0) == 0]

    terms = (query or "").lower().split()
    if not terms:
        return results

    def matches(model):
        haystack = " ".join([
            model["id"], model["name"], model["owned_by"],
            model["category"], model["description"], " ".join(model["aliases"]),
        ]).lower()
        return all(term in haystack for term in terms)

    return [m for m in results if matches(m)]


def accepts_images(config, model_id):
    """Whether `model_id` accepts images, from the cached catalogue: True, False, or None
    when the catalogue has not been fetched or does not list it.

    Any age of cache will do - a model does not gain or lose vision often - so this never
    waits on the network; the app refreshes the catalogue in the background at startup.
    """
    cached = load_cache(max_age=None, url=models_url(config.api_url))
    if not cached:
        return None
    model = resolve(cached, model_id)
    return None if model is None else bool(model.get("vision"))


EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max"]


def effort_for(config, model_id, wanted=None):
    """The reasoning effort to ask `model_id` for, or None to ask for nothing.

    A level is only ever sent to a model that lists its reasoning levels:
      - "none" (the setting None) goes to a model that lists "none" - one whose
        thinking can be switched off. A model that always thinks, GLM 5.3 for one, is
        asked for its lowest level instead: nano-gpt refuses "none" for it outright
        ("GLM 5.3 always thinks and does not support disabling reasoning").
      - any other level goes only to a model that always reasons, one that does not
        list "none". Measured live (Sept 2026), asking a model that thinks only when
        asked - deepseek/deepseek-v4-pro, listed as ["none", "high", "max"] - for any
        effort switched its thinking on: 35 tokens and 2s became 1,143 and 15s.
    A model the catalogue says nothing about is left alone.

    `wanted` (the setting, "low" by default) becomes the nearest level the model
    offers, rounding up: GLM 5.3 offers low, high and max, so "medium" is "high".
    """
    wanted = (wanted or getattr(config, "reasoning_effort", "low") or "low").lower()
    cached = load_cache(max_age=None, url=models_url(config.api_url))
    model = resolve(cached or [], model_id)
    levels = (model or {}).get("efforts") or []
    if not levels:
        return None
    if wanted == "none":
        if "none" in levels:
            return "none"
        return next((level for level in EFFORT_ORDER if level in levels), None)
    if "none" in levels:
        return None
    if wanted in levels:
        return wanted
    at = EFFORT_ORDER.index(wanted) if wanted in EFFORT_ORDER else 1
    for level in EFFORT_ORDER[at:] + list(reversed(EFFORT_ORDER[:at])):
        if level in levels:
            return level
    return None


def resolve(models, name):
    """Find a model by exact id, then by alias, then case-insensitively."""
    if not name:
        return None
    for model in models:
        if model["id"] == name:
            return model
    for model in models:
        if name in model["aliases"]:
            return model
    lowered = name.lower()
    for model in models:
        if model["id"].lower() == lowered:
            return model
        if any(a.lower() == lowered for a in model["aliases"]):
            return model
    return None
