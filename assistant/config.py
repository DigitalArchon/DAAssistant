"""Configuration storage.

What matters here:
  - API keys are kept in the desktop's keyring (see keychain.py), never in
    config.json. A key left in the file by an older version is moved there on start.
  - So is a password written into an address - a proxy's, an endpoint's (ADDRESSES):
    the file has the address without it, and the keyring the address as typed.
  - A config from before providers had names, whose one API URL was not NanoGPT's, is
    given the provider that URL was: see providers.legacy.
  - config.json is written 0600 all the same: it says which models and services are used.
  - Writes are atomic (temp + rename) so a crash mid-save cannot truncate the file.
  - Corrupt JSON is reported instead of being swallowed by a bare `except:`.
"""

import json
import math
import os
from urllib.parse import urlparse

from . import atomic, netguard, paths, providers, safejson
from .keychain import KeychainError, MemoryKeychain, SystemKeychain

DEFAULTS = {
    # NanoGPT's endpoint and key. The address is not in Settings: providers are chosen
    # by name there (see assistant/providers.py), and this one's is known.
    "api_url": providers.NANOGPT_URL,
    "api_key": "",
    # The other providers: OpenRouter's key, the Ollama server's address, and an
    # OpenAI-compatible endpoint of the user's own with the key it wants, if any.
    "openrouter_api_key": "",
    "ollama_url": providers.OLLAMA_DEFAULT_URL,
    "custom_url": "",
    "custom_api_key": "",
    # Which provider each of the three models below is asked of.
    "text_provider": providers.NANOGPT,
    "premium_text_provider": providers.NANOGPT,
    "vision_provider": providers.NANOGPT,
    "default_language": "English",
    "text_model": "deepseek-ai/DeepSeek-V3.1-Terminus",
    "premium_text_model": "anthropic/claude-sonnet-5.5",
    # Reads images: OCR, stage one of Translate image selection, and - when the text
    # model answering a question about an image cannot see images - the transcription
    # and description it answers from; for one that can, only the text a web search on
    # the question needs. A cheap, literal reader.
    "vision_model": "Qwen/Qwen3-VL-235B-A22B-Instruct",
    "use_premium": False,
    # Privacy Mode: every operation, and every follow-up, goes only to these models,
    # each attested before anything is sent to it (see assistant/privacy.py). TEE/ models
    # are attested, most as RedPill's gateway, and their replies checked where their
    # provider signs them; private/ models are end-to-end encrypted as well. A
    # conversation keeps the privacy it started with, whatever happens here.
    # The defaults are private/ ones, sealed end to end to an enclave attested itself,
    # after 4.0.0 (Nov 2026): every TEE/ model nano-gpt offered by then was attested as
    # RedPill's gateway, which does not show that the model's own host is an enclave,
    # so Strict refuses them (privacy.GATEWAY_SHORTFALL). Before, TEE/glm-5.3-flash,
    # TEE/glm-5.3 and TEE/qwen3.8-27b. Tinfoil's client, which these need, is in the
    # AppImage; from source, tools/install-private-mode.sh. GLM 5.3 Flash has no
    # private twin any more (retired Oct 2026): DeepSeek V4.1 Flash, fast, cheap and
    # able to see, answers and reads images in its place.
    "privacy_mode": False,
    "private_text_model": "private/deepseek-v4-1-flash",
    # Answers instead of private_text_model when the premium option is on, as
    # premium_text_model does outside Privacy Mode.
    "private_premium_model": "private/glm-5-3",
    "private_vision_model": "private/deepseek-v4-1-flash",
    # Refuse an enclave that proved less than it could - no GPU evidence, an Intel TCB
    # that is not UpToDate, Intel's or NVIDIA's services out of reach, an older release
    # - rather than sending with an amber banner; and after a reply left unsigned or not
    # compared by a model that signs them, send nothing more until the enclave has been
    # attested again (as every conversation does after a reply whose check failed).
    "privacy_strict": False,
    # After a TEE/ reply whose receipt holds, name the provider that ran the model, from
    # RedPill's public record of its gateway's session (privacy.TeeClient.served_by). Off
    # by default: it connects to api.redpill.ai directly, not through nano-gpt, so
    # RedPill sees this computer's address (SealedLore's author, Nov 11 2026: a clear
    # choice in Settings). Read at each reply, as SealedLore reads it: off, nothing more
    # is looked up, in conversations already open too.
    "tee_name_upstreams": False,
    # Global shortcuts, set in Settings > Shortcuts: {action: "<Control><Shift>1"}. None
    # are bound on a new install. See assistant/hotkeys.py.
    "hotkeys": {},
    # How they are bound: auto picks for the session (X11 grabs, the GlobalShortcuts
    # portal, or the desktop's own keybindings); x11, portal and desktop force one;
    # off binds none.
    "hotkey_mode": "auto",
    # What the app last wrote into the desktop's own keybindings, so that it can be
    # rewritten when it changes and taken out when something else takes over.
    "hotkeys_installed": {},
    # Image pipeline. See assistant/imaging.py for what these do.
    "upscale_small_captures": True,
    "capture_target_long_edge": 1600,
    # Measured on a 3200x1100 console capture, three runs at each size: at 2000 the
    # transcription lost a tracking number (JD014600006281726431) every time, at 2600
    # and above it lost nothing. Downscaling a crisp screenshot also *costs* bytes -
    # resampling noise compresses worse - so the capped PNG was 81 KB against 48 KB
    # for the original. Long identifiers are the whole point of reading an error.
    "capture_max_long_edge": 3000,
    # Web search. web_search_enabled is the stored default; individual conversations
    # can override it from the chat window without changing this.
    "web_search_enabled": False,
    "web_search_mode": "standard",          # off | standard | deep
    # Measured against the alternatives over three queries: the most primary sources
    # (10 of 16, against 4 for tavily and 2 for linkup) and about 1,300 characters of
    # clean prose per result. See websearch.PROVIDERS for the full table. linkup keeps
    # nothing (zero data retention): chosen, it is the only provider ever asked.
    "web_search_provider": "perplexity",
    "web_search_context_size": "medium",    # low | medium | high
    "web_search_valyu_search_type": "all",  # valyu only
    # Have the text model turn the question and the material into a search query before
    # searching. Pasting a stack trace into a search box finds nothing; the terms that
    # find something are the exception name and the identifiers, without the local file
    # paths. Costs one cheap call per searched request.
    "web_search_craft_query": True,
    # Ask the model to list the sources it used. The provider returns no structured
    # citations, but the search results in its context carry titles and URLs.
    "web_search_citations": True,
    # How hard a model that always reasons thinks before it answers: low, medium or
    # high. Low by default: live (Sept 2026), private/glm-5-3 at its own default
    # reasoned through a 20,480-token limit on a 5,000-character translation and never
    # began it; at low it translated in 10s, reasoning 0 tokens. Models that think only
    # when asked are never sent it (models.effort_for).
    "reasoning_effort": "low",
    # The answer's length. A model asked for reasoning gets room to think on top of
    # this (llm.REASONING_ROOM), so thinking cannot use up the answer's share.
    "max_tokens": 4096,
    # Ask Claude models to cache the conversation so far, for "5m" or "1h"; "off". A
    # cache write costs more than the input it caches (1.25x, or 2x for an hour), and a
    # read far less (0.1x, Sonnet 5.5 0.05x). See llm.CACHE_TTLS.
    "prompt_caching": "off",
    "request_timeout": 180,
    # Every request of the app's own through this proxy (proxy.py): socks5h://host:port
    # or http://host:port. Empty: the environment's proxy settings, if any.
    "proxy": "",
    # Debugging: write every image sent to the model into ~/.cache/da-assistant/captures.
    "debug_save_captures": False,
    # Look up word: the folder holding the user's DSL and StarDict dictionaries, read in
    # place (see assistant/reference), and the paths of any in it not to search.
    "dictionaries_dir": "",
    "dictionaries_disabled": [],
    # The order lookups list dictionaries in, as paths; any not listed come after, by
    # name. Set by moving them in Settings > Reference.
    "dictionaries_order": [],
    # Search Encyclopedia and Ask Encyclopedia, for those with the DVD. Off, neither is
    # in the tray menu and their hotkeys are left free for other programs.
    "encyclopedia_enabled": False,
    # The encyclopedia DVD's data folder (the one holding ARTICLE_DATA.zip). Its
    # articles are imported once into ~/.local/share/da-assistant (see
    # assistant/reference/encyclopedia.py); pictures and films go on being read from here.
    "encyclopedia_dir": "",
    # Ask Encyclopedia's defaults; each window can change them for itself. Strict: the
    # answer is the encyclopedia's alone. Audience: adult, student or young - which
    # encyclopedia is preferred, and who the answer is written for.
    "encyclopedia_strict": True,
    "encyclopedia_audience": "adult",
    # How tall the chat window's message box was last dragged, in pixels; 0 until it has
    # been. See ChatWindow._build_splitter.
    "chat_prompt_height": 0,
    # The size of a conversation's text, as Ctrl+plus, Ctrl+minus and Ctrl+0 last left
    # it: 1.0 is the theme's own. Kept for the next window.
    "chat_zoom": 1.0,
    # A conversation window's size as it was last left, in pixels; 0 until it has been
    # resized. See ChatWindow._initial_size.
    "chat_width": 0,
    "chat_height": 0,
    # The user's own actions, Settings > Actions: [{"slot": 1-6, "name", "prompt",
    # "source": "clipboard" | "selection" | "region"}]. See hotkeys.CUSTOM_ACTIONS.
    "custom_actions": [],
    # Where each API key was saved for: {"custom_api_key": "https://llm.example:443"},
    # the origin (scheme, host and port) of its provider's address when it was saved -
    # not the key, which is in the keyring. A key goes there and nowhere else
    # (Config.key_withheld): an address changed since, in Settings or here by hand, is
    # sent no key until Settings is saved and told to use it there, or a key is entered
    # for it. A key kept before this was recorded takes its address's origin at the
    # first start that reads it, with no question asked.
    "key_origins": {},
    # The address each key's place was said No to, when Settings asked whether to send
    # the saved key there: {"custom_api_key": "https://llm.elsewhere.example:443"}. It is
    # not asked about again at later saves - a question repeated at every Save is soon
    # answered Yes without reading - and Settings > Providers says the key is held back
    # there, with Ask again.... A key entered for that address, or the address put back
    # where the key was saved for, lets the No go. No key in it.
    "key_declined": {},
    # The address each secret was confirmed for going over plain http to another
    # machine on the local network (netguard.in_the_clear), where anyone on it can read
    # it: {"custom_api_key": "http://192.168.1.50:8000"}, and the same for a password
    # written into one of ADDRESSES ({"proxy": ...}). Until then it is not sent there
    # (key_in_clear_withheld, address_withheld): nothing is taken as confirmed, one
    # going there before this was asked included (the author, Nov 14 2026). Settings
    # asks (No the default) and lists one held back with Confirm.... No secret in it.
    "key_in_clear": {},
}

# Settings kept in the keychain under their own names, and never written to config.json.
SECRETS = ("api_key", "openrouter_api_key", "custom_api_key")
# The provider each key is for: where its address is (providers.Kind.url_key, or its
# fixed address), and what it is called.
KEYED = {kind.key_key: kind for kind in providers.KINDS.values() if kind.key_key}

# Settings that are addresses, which may have a user name and password written into
# them: a proxy that asks for them, an endpoint or a model server behind a password.
# config.json has the address without them (netguard.without_password), and the keychain
# the address as typed, under ADDRESS_SECRET's name - put back together when read, and
# only while the file's address is that one less its password, so that a password goes
# to no address but the one it was typed for, whatever the file is edited to. Up to
# 4.1.0 the whole address went into the file, password and all.
ADDRESSES = ("proxy", "custom_url", "ollama_url")
ADDRESS_SECRET = "{}_with_password"
# Each of ADDRESSES, as the question before a password in it goes unencrypted names it
# (Config.keys_in_the_clear).
ADDRESS_SAID = {"proxy": "The proxy's user name and password",
                "custom_url": "The password in the other endpoint's address",
                "ollama_url": "The password in Ollama's address"}

# Defaults that were, and that their provider no longer lists or this app can no longer
# use: a file still holding one (save() writes every setting) is moved to today's
# default. Keyed by the setting and the setting naming its provider, which must be the
# default provider for the move; None for a private model, which is always NanoGPT's.
# NanoGPT's list had no gpt-5-chat-latest by Oct 2026, under any alias; TEE/qwen3.6-27b
# was refused from Nov 2026 (see private_vision_model).
RETIRED_DEFAULTS = {("premium_text_model", "premium_text_provider"): {"gpt-5-chat-latest"},
                    ("private_vision_model", None): {"TEE/qwen3.6-27b"}}


class ConfigError(Exception):
    pass


# Settings that keep something from being sent, read as on when the file cannot say.
# A value of the wrong type falls back to the default, and a file that cannot be read
# at all to the defaults - which for these is off: a stray comma or "privacy_mode":
# "enabled" sent the next capture to the ordinary models. Read as on, the worst is a
# request held back, and a notice that says why. null is of the wrong type for these
# too, though it passes elsewhere as "undecided" (_coerce): undecided was off.
FAIL_SAFE = {"privacy_mode": True, "privacy_strict": True}
_SAYS_ON = r'"%s"\s*:\s*(?:true|1|"\s*(?:true|yes|on|1)\s*")'


_KINDS = {bool: "true or false", int: "a whole number", float: "a number", str: "text",
          list: "a list", dict: "an object"}

# What a number edited into config.json by hand may be, from and to - None for no
# bound. One outside is left out and its default used, as one of the wrong kind is:
# checked for its kind alone, a capture_max_long_edge of -3000 sent every capture as
# 1x1 and said nothing, a max_tokens of 0 sent none at all - which is how long
# transcriptions come back cut short - and a chat_zoom of NaN, which Python's JSON
# reads, sized the text NaN.
RANGES = {
    "capture_target_long_edge": (64, None),
    "capture_max_long_edge": (64, None),
    "max_tokens": (1, None),
    "request_timeout": (1, None),
    "chat_prompt_height": (0, None),            # 0: never dragged
    "chat_zoom": (0.5, 3.0),                    # ui/chat.py's ZOOM_STEPS, end to end
    "chat_width": (0, None),                    # 0: never resized
    "chat_height": (0, None),
}


def _expected(key):
    """What `key` has to be, in words: "a whole number of at least 64"."""
    kind = _KINDS.get(type(DEFAULTS[key]), "valid")
    low, high = RANGES.get(key, (None, None))
    if low is not None and high is not None:
        return f"{kind} from {low} to {high}"
    if low is not None:
        return f"{kind} of at least {low}"
    return kind


def _in_range(key, value):
    """`value` for `key` if it is one the setting can take; ValueError if not."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(value)
    low, high = RANGES.get(key, (None, None))
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(value)
    return value


def _coerce(default, value):
    """`value`, read from a config.json that may have been edited by hand, as the type
    of its `default`; ValueError when there is no plain way to make it one. MANUAL
    invites editing capture_max_long_edge, and "3600" - in quotes - used to make every
    capture fail with a TypeError."""
    if value is None or default is None:
        return value                    # "undecided" (hotkeys, see Config.load)
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        words = {"true": True, "false": False, "yes": True, "no": False, "on": True,
                 "off": False, "1": True, "0": False}
        if isinstance(value, str) and value.strip().lower() in words:
            return words[value.strip().lower()]
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError(value)
    if isinstance(default, int):
        if isinstance(value, bool):
            raise ValueError(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            return int(value.strip())
        raise ValueError(value)
    if isinstance(default, float):
        if isinstance(value, bool):
            raise ValueError(value)
        return float(value)
    if not isinstance(value, type(default)):
        raise ValueError(value)
    return value


def _plain_local(api_url):
    """Whether `api_url` is plain http to this machine or its own network."""
    parsed = urlparse((api_url or "").strip())
    return parsed.scheme == "http" and netguard.is_local(parsed.hostname)


class Config:
    """Settings, persisted to JSON.

    save() writes only the keys this instance actually changed, merged over whatever is
    on disk at the time. Writing the whole in-memory snapshot instead loses concurrent
    edits: the tray's premium checkbox only means to flip use_premium, but a full write
    reverts every other setting to what it was when the app started - so editing
    config.json by hand, or from another instance, was silently undone by the next
    checkbox click.
    """

    # Attributes that are bookkeeping rather than persisted settings.
    _INTERNAL = ("config_dir", "config_file", "load_error", "load_warnings", "keychain",
                 "keychain_error", "_dirty", "_loading", "_kept", "_loose")

    def __init__(self, path=None, keychain=None):
        """The user's config, its secrets in the desktop's keyring. Given a `path` of its
        own - a test's, say - the secrets stay in memory unless a `keychain` is given
        too, so that nothing but the real config ever reads or writes the keyring."""
        object.__setattr__(self, "_dirty", set())
        object.__setattr__(self, "_loading", False)
        object.__setattr__(self, "_kept", {})       # each secret as the keychain has it
        object.__setattr__(self, "_loose", set())   # secrets config.json held at load
        if path:
            self.config_file = os.path.abspath(os.path.expanduser(path))
            self.config_dir = os.path.dirname(self.config_file)
        else:
            self.config_dir = os.path.expanduser("~/.config/da-assistant")
            self.config_file = os.path.join(self.config_dir, "config.json")
        self.keychain = keychain or (MemoryKeychain() if path else SystemKeychain())
        self.keychain_error = None
        self.load_error = None
        self.load_warnings = []
        self.load()

    def __setattr__(self, name, value):
        # Track deliberate changes so save() knows what it is allowed to overwrite.
        if name in DEFAULTS and not object.__getattribute__(self, "_loading"):
            object.__getattribute__(self, "_dirty").add(name)
            changed = name in SECRETS and value != getattr(self, name, None)
            # A password typed anew into an address is confirmed anew for plain http.
            changed_password = name in ADDRESSES and netguard.credentials(
                value or "") != netguard.credentials(getattr(self, name, "") or "")
            if changed_password and isinstance(self.key_in_clear, dict) \
                    and name in self.key_in_clear:
                object.__setattr__(self, "key_in_clear", {
                    k: v for k, v in self.key_in_clear.items() if k != name})
                object.__getattribute__(self, "_dirty").add("key_in_clear")
            if changed:
                # A key entered now is for these settings' address, which save()
                # records for it: until then it goes with the address as it stands.
                # A No said to sending the key it replaces somewhere goes with it, and
                # so does its going over plain http, confirmed for the old key.
                for kept in ("key_origins", "key_declined", "key_in_clear"):
                    origins = getattr(self, kept, None)
                    if isinstance(origins, dict) and name in origins:
                        setattr(self, kept, {k: v for k, v in origins.items()
                                             if k != name})
        object.__setattr__(self, name, value)

    def _read_file(self, warnings=None):
        """Current on-disk settings, or {} if absent/unreadable. A value of the wrong
        type is made the right one where that is plain ("3600" for a number), and left
        out otherwise, so the default stands in - as is a number out of its setting's
        range (RANGES); `warnings` collects what was."""
        if not os.path.exists(self.config_file):
            return {}, None
        try:
            with open(self.config_file, "r") as f:
                # Edited by hand, or by anything else of this user's: nested past
                # reading is refused as unreadable, never a crash at start.
                loaded = safejson.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("config root is not an object")
        except (OSError, ValueError) as e:
            return {}, f"{self.config_file}: {e}"
        # Ignore unknown keys rather than crashing on an old/newer schema.
        values = {}
        for key, value in loaded.items():
            if key not in DEFAULTS:
                continue
            try:
                if value is None and key in FAIL_SAFE:
                    raise ValueError(value)     # never "undecided": see FAIL_SAFE
                values[key] = _in_range(key, _coerce(DEFAULTS[key], value))
            except (TypeError, ValueError):
                instead = FAIL_SAFE.get(key, DEFAULTS[key])
                if key in FAIL_SAFE:
                    values[key] = instead
                if warnings is not None:
                    warnings.append(f"{key} in {os.path.basename(self.config_file)} is "
                                    f"{json.dumps(value)[:40]}, which is not "
                                    f"{_expected(key)}; using "
                                    f"{json.dumps(instead)}.")
        return values, None

    def _fail_safe_from_damaged(self):
        """FAIL_SAFE's settings that a file too damaged to read plainly turns on."""
        import re
        try:
            with open(self.config_file, "r", errors="replace") as f:
                text = f.read()
        except OSError:
            return {}
        return {key: value for key, value in FAIL_SAFE.items()
                if re.search(_SAYS_ON % key, text, re.I)}

    def load(self):
        values = dict(DEFAULTS)
        self.load_warnings = []
        on_disk, error = self._read_file(self.load_warnings)
        for warning in self.load_warnings:
            print(f"[config] {warning}")
        # A key written into the file by an older version, to be moved to the keychain.
        loose = {key: on_disk.pop(key) for key in SECRETS if on_disk.get(key)}
        object.__setattr__(self, "_loose", set(loose.values()))
        values.update(on_disk)
        if on_disk and "hotkeys" not in on_disk:
            # Written before shortcuts could be chosen: hotkeys.migrate decides what it
            # keeps, from what worked in this session. Until then it is undecided, and
            # stays out of the file.
            values["hotkeys"] = None
        # Surfaced in the UI; we still start with defaults rather than dying - except for
        # what keeps a capture from going to the ordinary models (FAIL_SAFE).
        self.load_error = error
        if error:
            kept_on = self._fail_safe_from_damaged()
            values.update(kept_on)
            self.load_warnings += [f"{key} stays on, as the file that could not be read "
                                   f"has it." for key in kept_on]

        moved = False
        self.keychain_error = None
        kept = object.__getattribute__(self, "_kept")
        for key in SECRETS:
            try:
                stored = self.keychain.get(key) or ""
            except KeychainError as e:
                stored, self.keychain_error = "", str(e)
            if key in loose and not stored and self.keychain_error is None:
                try:
                    self.keychain.set(key, loose[key])
                    stored, moved = loose[key], True
                except KeychainError as e:
                    self.keychain_error = str(e)
            kept[key] = stored
            # Where the keychain could not take it, the file's copy is used, and stays
            # in the file until it can: deleting it would lose the key.
            values[key] = stored or loose.get(key, "")
        moved = self._load_addresses(values) or moved

        object.__setattr__(self, "_loading", True)
        try:
            for key, value in values.items():
                setattr(self, key, value)
        finally:
            object.__setattr__(self, "_loading", False)
        object.__getattribute__(self, "_dirty").clear()
        for (key, provider_key), retired in RETIRED_DEFAULTS.items():
            if on_disk.get(key) in retired and (provider_key is None or on_disk.get(
                    provider_key, DEFAULTS[provider_key]) == DEFAULTS[provider_key]):
                setattr(self, key, DEFAULTS[key])
                moved = True
        if on_disk and "text_provider" not in on_disk:
            moved = self._name_provider(on_disk.get("api_url")) or moved
        moved = self._adopt_key_origins() or moved
        if moved or (loose and all(kept[key] for key in loose)):
            try:
                self.save()                 # the same file, without the key in it
            except (OSError, KeychainError):
                pass                        # read-only: it goes at the next save

    def _adopt_key_origins(self):
        """A key with no origin recorded beside it - kept before origins were, or moved
        to another provider's setting just now (_name_provider) - takes its address's
        own, as the address saved beside it is the one it was saved for: no question is
        asked. True if one did, for load() to save."""
        origins = dict(self.key_origins or {})
        for key in SECRETS:
            if getattr(self, key, "") and key not in origins:
                origins[key] = netguard.origin(self.key_address(key))
        if origins == (self.key_origins or {}):
            return False
        self.key_origins = origins
        return True

    def key_address(self, key):
        """The address `key` (one of SECRETS) is sent with: its provider's chat
        endpoint, as these settings have it."""
        kind = KEYED[key]
        return providers.chat_url(kind, getattr(self, kind.url_key, "") if kind.url_key
                                  else "")

    def key_saved_for(self, key):
        """The origin `key` was saved for: as recorded, or - a key typed in this session
        and not saved yet - its address's own as it stands."""
        recorded = (self.key_origins or {}).get(key) if isinstance(self.key_origins,
                                                                   dict) else None
        return str(recorded) if recorded else netguard.origin(self.key_address(key))

    def key_declined_for(self, key):
        """The origin said No to sending `key` to (key_declined), or ""."""
        declined = self.key_declined if isinstance(self.key_declined, dict) else {}
        found = declined.get(key)
        return found if isinstance(found, str) else ""

    def keys_in_the_clear(self):
        """(what, address, secret) for each secret these settings would send over plain
        http to another machine on the local network, where anyone on that network can
        read it on its way - a café's or a hotel's Wi-Fi included: an API key, unless it
        is held back from its address (key_withheld), and a user name and password
        written into an address (ADDRESSES), which go with every request to it. To this
        machine itself (netguard.is_loopback) nothing leaves it, and nothing is counted.
        Settings asks before any of them is sent (SettingsDialog._may_send_plain), as
        SealedLore's keys_in_the_clear does (Nov 2026), confirmed for it or not."""
        return [(what, address, secret)
                for _name, what, address, secret in self.secrets_in_the_clear()]

    def secrets_in_the_clear(self):
        """keys_in_the_clear's, each with the name it is confirmed under (key_in_clear):
        (name, what, address, secret)."""
        found = []
        for key in SECRETS:
            secret = "" if self.key_withheld(key) else (getattr(self, key, "") or "")
            address = self.key_address(key)
            if secret and netguard.in_the_clear(address):
                found.append((key, f"The {KEYED[key].name} API key", address, secret))
        for name, what in ADDRESS_SAID.items():
            address = str(getattr(self, name, "") or "")
            secret = netguard.credentials(address)
            if secret and netguard.in_the_clear(address):
                found.append((name, what, address, secret))
        return found

    def clear_agreed(self, name, address):
        """Whether `name`'s secret was confirmed for going over plain http to
        `address`'s origin (key_in_clear)."""
        agreed = self.key_in_clear if isinstance(self.key_in_clear, dict) else {}
        return bool(address) and agreed.get(name) == netguard.origin(address)

    def _in_clear_words(self, name, what, address):
        going_to = netguard.origin(address)
        return (f"{what} would go over plain http, unencrypted, to {going_to}, another "
                f"machine on your network, where anyone on it can read it, so it is not "
                f"sent until you confirm that once: in Settings > Providers, under Keys "
                f"held back, Confirm...; or use the server's https:// address.")

    def key_in_clear_withheld(self, key, url=None):
        """Why `key`'s secret is not sent with a request to `url` - its own address, when
        not given - because it would go over plain http to another machine and that was
        not confirmed for it (key_in_clear), or None. Nothing is taken as confirmed."""
        if key not in KEYED or not (getattr(self, key, "") or ""):
            return None
        address = self.key_address(key) if url is None else url
        if not netguard.in_the_clear(address) or self.clear_agreed(key, address):
            return None
        return self._in_clear_words(key, f"The {KEYED[key].name} API key", address)

    def address_withheld(self, name):
        """Why the address `name` (one of ADDRESSES) may not be used: a user name and
        password written into it would go over plain http to another machine, and that
        was not confirmed (key_in_clear); or None."""
        address = str(getattr(self, name, "") or "")
        if name not in ADDRESS_SAID or not netguard.credentials(address) \
                or not netguard.in_the_clear(address) or self.clear_agreed(name, address):
            return None
        return self._in_clear_words(name, ADDRESS_SAID[name], address)

    def key_withheld(self, key, url=None):
        """Why `key`'s secret is not sent with a request to `url` - its own address, when
        not given - or None when it may be. A key goes only to the origin it was saved
        for (key_origins): changing the address in Settings, or editing it into
        config.json, does not take the key there with it. Settings asks, when it is
        saved, whether to (Assistant._ask_keys_follow); until it is told so, or a key is
        entered for the new address, nothing is sent with it there, and this says why.
        The same rule as DA Private Coder's creds.py (Nov 2026)."""
        if key not in KEYED or not (getattr(self, key, "") or ""):
            return None
        saved_for = self.key_saved_for(key)
        going_to = netguard.origin(self.key_address(key) if url is None else url)
        if going_to == saved_for:
            return None
        name = KEYED[key].name
        where = (f"the address in {os.path.basename(self.config_file)}"
                 if KEYED[key].id == providers.NANOGPT else f"{name}'s address")
        declined = " You said No to sending it there." if (
            going_to and self.key_declined_for(key) == going_to) else ""
        return (f"The {name} API key was saved for {saved_for or 'another address'}, "
                f"and {where} is now {going_to or netguard.shown(self.key_address(key))}: "
                f"the key is not sent there.{declined} If it is meant for that address, "
                f"open Settings and save, and say Yes when asked to use it there (Ask "
                f"again... in Settings > Providers, if you said No); or enter that "
                f"address's own key in Settings > Providers.")

    def key_for(self, key, url=None):
        """`key`'s secret, if it may go with a request to `url` (key_withheld,
        key_in_clear_withheld), else ""."""
        if self.key_withheld(key, url) or self.key_in_clear_withheld(key, url):
            return ""
        return getattr(self, key, "") or ""

    def _load_addresses(self, values):
        """Each of ADDRESSES as it was typed: the file's, with the password the keychain
        has for it. One the file has a password in - an older version wrote it, or it
        was edited in by hand - is moved to the keychain, and out of the file once it is
        there; where the keychain cannot take it, it stays in the file and is used, as an
        API key does. True if one moved."""
        kept = object.__getattribute__(self, "_kept")
        loose = object.__getattribute__(self, "_loose")
        moved = False
        for key in ADDRESSES:
            name = ADDRESS_SECRET.format(key)
            written = values.get(key) or ""
            stored = ""
            if written:                     # nothing to put a password back into
                try:
                    stored = self.keychain.get(name) or ""
                except KeychainError as e:
                    self.keychain_error = str(e)
            if netguard.without_password(written) != written:
                loose.add(written)
                if stored != written and self.keychain_error is None:
                    try:
                        self.keychain.set(name, written)
                        stored = written
                    except KeychainError as e:
                        self.keychain_error = str(e)
                moved = moved or stored == written      # the file's copy can go
            elif stored and netguard.without_password(stored) == written:
                values[key] = stored
            kept[name] = stored
        return moved

    def _address(self, key, written):
        """The address to use for `key`, of which config.json now has `written`: the one
        in memory or the keychain's, if either is that address with its password."""
        kept = object.__getattribute__(self, "_kept")
        for full in (getattr(self, key, ""), kept.get(ADDRESS_SECRET.format(key))):
            if full and netguard.without_password(full) == written:
                return full
        return written

    def _name_provider(self, api_url):
        """Written before providers had names: everything went to one API URL with one
        key. If that was not NanoGPT, the three models are given the provider it was,
        and the address and key are moved to that provider's own settings. True if
        anything changed."""
        provider, address = providers.legacy(api_url)
        if provider == providers.NANOGPT:
            return False
        if provider == providers.OLLAMA and self.api_key and not _plain_local(api_url):
            # Ollama takes no key, and a server that wanted one - on Ollama's port, but
            # behind https with a key of its own - is an endpoint of another kind. Named
            # Ollama, its key stayed NanoGPT's, and went to nano-gpt.com with the next
            # web search or model list. Not so plain http on one's own network: 3.0 to
            # 3.2 wanted a key whatever the address, so Ollama's was given one - NanoGPT's
            # own, or one made up - and is still Ollama, with the key left as NanoGPT's.
            provider, address = providers.CUSTOM, (api_url or "").strip()
        kind = providers.KINDS[provider]
        for _model_key, provider_key in providers.ROLES.values():
            setattr(self, provider_key, provider)
        self.api_url = DEFAULTS["api_url"]
        if address and kind.url_key:
            setattr(self, kind.url_key, address)
        if kind.key_key and kind.key_key != "api_key":
            setattr(self, kind.key_key, self.api_key)
            self.api_key = ""
        return True

    def as_dict(self):
        return {key: getattr(self, key) for key in DEFAULTS}

    def save(self):
        paths.own_folder(self.config_dir)

        # Start from what is on disk now, not from what we loaded at startup, then apply
        # only the keys we deliberately changed. Anything edited elsewhere in the
        # meantime is preserved instead of being reverted.
        dirty = object.__getattribute__(self, "_dirty")
        # A key changed here is saved for the address it goes with now: its origin is
        # recorded beside the address, and the key goes nowhere else (key_withheld). One
        # taken out takes its origin with it. A key not changed keeps the origin it had,
        # whatever has happened to the address since.
        origins = dict(self.key_origins or {}) if isinstance(self.key_origins, dict) else {}
        for key in SECRETS:
            if key in dirty:
                if getattr(self, key, ""):
                    origins[key] = netguard.origin(self.key_address(key))
                else:
                    origins.pop(key, None)
        if origins != self.key_origins:
            self.key_origins = origins          # dirty, so written below
        # A No said to sending a key somewhere goes once there is nothing to hold back:
        # the key taken out, or its address put back where it was saved for.
        declined = dict(self.key_declined) if isinstance(self.key_declined, dict) else {}
        for key in list(declined):
            if key not in SECRETS or not isinstance(declined[key], str) \
                    or not getattr(self, key, "") or not self.key_withheld(key):
                declined.pop(key)
        if declined != self.key_declined:
            self.key_declined = declined
        merged = dict(DEFAULTS)
        on_disk, error = self._read_file()
        if error:
            # Unreadable is not the same as empty. Writing over it would replace a file
            # that is one stray comma away from fine - API key included - with defaults,
            # so it is set aside first. rename keeps its 0600.
            try:
                os.replace(self.config_file, self.config_file + ".corrupt")
            except OSError:
                pass
            # And what this instance holds is written in its place, not the defaults:
            # those were adopted below as well, so a file broken by hand while the app
            # ran - then a zoom or a tray click - turned Privacy Mode off and sent every
            # model back to NanoGPT's, there and then, without a word.
            on_disk = {key: value for key, value in self.as_dict().items()
                       if key not in SECRETS and value is not None}
        merged.update(on_disk)
        for key in dirty:
            merged[key] = getattr(self, key)
        # Undecided (None, see load) and not on disk: written once it is decided, not
        # as the default in the meantime, which would read as decided.
        pending = [key for key in DEFAULTS if key not in on_disk and key not in dirty
                   and getattr(self, key, 0) is None]
        for key in pending:
            merged.pop(key, None)
        # Secrets go to the keychain, and only the ones changed here - first, so that
        # what the file keeps can follow what the keychain actually took. A copy in the
        # file goes once the keychain holds that same key. Until then, a key the file
        # already held stays in it, under the name it has now: an older version wrote
        # it there, the keychain cannot take it, and deleting it would lose it - which a
        # Settings save about something else entirely used to do. A key typed in this
        # session is never written to the file; it is used until the app quits.
        changed = {key: getattr(self, key) for key in SECRETS if key in dirty}
        # An address with a password goes to the keychain whole; one without takes out
        # the password kept for what it was.
        for key in ADDRESSES:
            if key in dirty:
                value = getattr(self, key) or ""
                changed[ADDRESS_SECRET.format(key)] = (
                    value if netguard.without_password(value) != value else "")
        try:
            self._save_secrets(changed)
            refused = None
        except KeychainError as e:
            refused = e
        kept = object.__getattribute__(self, "_kept")
        loose = object.__getattribute__(self, "_loose")
        for key in SECRETS:
            merged.pop(key, None)
            value = getattr(self, key, "") or ""
            if value and kept.get(key) != value and value in loose:
                merged[key] = value
        # And the file has the address without its password - but for one that was in
        # the file already, which stays there until the keychain takes it, as a key does.
        for key in ADDRESSES:
            full = merged.get(key) or ""
            if kept.get(ADDRESS_SECRET.format(key)) == full or full not in loose:
                merged[key] = netguard.without_password(full)

        # Whole or not at all, 0600, flushed to the disk with its folder (atomic.py).
        atomic.write_json(self.config_file, merged, indent=2)

        # Adopt the merged result so this object reflects what is actually stored - an
        # address with the password the file does not have.
        object.__setattr__(self, "_loading", True)
        try:
            for key, value in merged.items():
                if key in ADDRESSES:
                    value = self._address(key, value)
                if key not in SECRETS and key not in pending:
                    setattr(self, key, value)
        finally:
            object.__setattr__(self, "_loading", False)
        dirty.clear()
        if refused is not None:
            raise refused

    def _save_secrets(self, values):
        """Write the secrets that changed to the keychain. The rest of the settings are
        already saved when this fails: then the secret is used until the app quits,
        and KeychainError says why it was not kept."""
        kept = object.__getattribute__(self, "_kept")
        failed = None
        for key, value in values.items():
            if value == kept.get(key, ""):
                continue                    # unchanged: no keyring prompt for nothing
            try:
                if value:
                    self.keychain.set(key, value)
                else:
                    self.keychain.delete(key)
            except KeychainError as e:
                failed = self.keychain_error = str(e)
                continue
            kept[key] = value
        if failed:
            raise KeychainError(failed)

    def detached(self, **overrides):
        """A copy with `overrides` applied, sharing nothing with this one.

        For trying settings out - the Settings dialog's connection test - without the
        values ever touching the config the rest of the app is reading, or its dirty
        set, which decides what the next save() is allowed to overwrite.
        """
        copy = object.__new__(type(self))
        object.__setattr__(copy, "_dirty", set())
        object.__setattr__(copy, "_loading", True)
        object.__setattr__(copy, "_kept", dict(object.__getattribute__(self, "_kept")))
        object.__setattr__(copy, "_loose", set(object.__getattribute__(self, "_loose")))
        copy.load_warnings = list(self.load_warnings)
        copy.config_dir, copy.config_file = self.config_dir, self.config_file
        copy.keychain, copy.keychain_error = self.keychain, self.keychain_error
        copy.load_error = self.load_error
        values = self.as_dict()
        values.update({k: v for k, v in overrides.items() if k in DEFAULTS})
        # A key given here that is not the one kept is one typed for these settings -
        # Settings' own, being tried out - and goes with their address, as one entered
        # does (__setattr__). One kept goes only where it was saved for.
        for kept in ("key_origins", "key_declined", "key_in_clear"):
            if kept == "key_in_clear" and kept in overrides:
                continue                # given as Settings has it (its own answers)
            origins = values.get(kept)
            if isinstance(origins, dict):
                values[kept] = {
                    key: origin for key, origin in origins.items()
                    if not (key in overrides and overrides[key] != getattr(self, key, None))}
        for key, value in values.items():
            setattr(copy, key, value)
        object.__setattr__(copy, "_loading", False)
        return copy

    def model(self, role):
        """The model for "text", "premium" or "vision", carrying its provider."""
        return providers.role_model(self, role)

    def active_text_model(self):
        if self.privacy_mode:
            return self.private_premium_model if self.use_premium else self.private_text_model
        return self.model("premium" if self.use_premium else "text")
