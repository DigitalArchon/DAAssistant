"""Configuration storage.

What matters here:
  - The API key is kept in the desktop's keyring (see keychain.py), never in
    config.json. A key left in the file by an older version is moved there on start.
  - config.json is written 0600 all the same: it says which models and services are used.
  - Writes are atomic (temp + rename) so a crash mid-save cannot truncate the file.
  - Corrupt JSON is reported instead of being swallowed by a bare `except:`.
"""

import json
import os
import stat
import tempfile

from .keychain import KeychainError, MemoryKeychain, SystemKeychain

DEFAULTS = {
    "api_url": "https://nano-gpt.com/api/v1/chat/completions",
    "api_key": "",
    "default_language": "English",
    "text_model": "deepseek-ai/DeepSeek-V3.1-Terminus",
    "premium_text_model": "gpt-5-chat-latest",
    # Reads images: OCR, stage one of Translate image selection, and - when the text
    # model answering a question about an image cannot see images - the transcription
    # and description it answers from. A cheap, literal reader.
    "vision_model": "Qwen/Qwen3-VL-235B-A22B-Instruct",
    "use_premium": False,
    # Privacy Mode: every operation, and every follow-up, goes only to these two models,
    # each attested before anything is sent to it (see assistant/privacy.py). TEE/ models
    # are attested and sign their replies; private/ models are end-to-end encrypted as
    # well. A conversation keeps the privacy it started with, whatever happens here.
    # The defaults are the two that verified end to end - attestation with NVIDIA's GPU
    # evidence, and signed replies - in StoryLoom's survey of Sept 2026, and need
    # nothing installed.
    "privacy_mode": False,
    "private_text_model": "TEE/glm-5.3-flash",
    # Answers instead of private_text_model when the premium option is on, as
    # premium_text_model does outside Privacy Mode.
    "private_premium_model": "TEE/glm-5.3",
    "private_vision_model": "TEE/qwen3.6-27b",
    # auto: use the GlobalShortcuts portal when a backend provides it (KDE, Hyprland).
    # static: bindings were installed by tools/install-hotkeys.sh; do not also use the portal.
    # portal: force the portal. off: no hotkeys.
    "hotkey_mode": "auto",
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
    # clean prose per result. See websearch.PROVIDERS for the full table.
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
    "request_timeout": 180,
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
}

# Settings kept in the keychain under their own names, and never written to config.json.
SECRETS = ("api_key",)


class ConfigError(Exception):
    pass


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
    _INTERNAL = ("config_dir", "config_file", "load_error", "keychain", "keychain_error",
                 "_dirty", "_loading", "_kept")

    def __init__(self, path=None, keychain=None):
        """The user's config, its secrets in the desktop's keyring. Given a `path` of its
        own - a test's, say - the secrets stay in memory unless a `keychain` is given
        too, so that nothing but the real config ever reads or writes the keyring."""
        object.__setattr__(self, "_dirty", set())
        object.__setattr__(self, "_loading", False)
        object.__setattr__(self, "_kept", {})       # each secret as the keychain has it
        if path:
            self.config_file = os.path.abspath(os.path.expanduser(path))
            self.config_dir = os.path.dirname(self.config_file)
        else:
            self.config_dir = os.path.expanduser("~/.config/da-assistant")
            self.config_file = os.path.join(self.config_dir, "config.json")
        self.keychain = keychain or (MemoryKeychain() if path else SystemKeychain())
        self.keychain_error = None
        self.load_error = None
        self.load()

    def __setattr__(self, name, value):
        # Track deliberate changes so save() knows what it is allowed to overwrite.
        if name in DEFAULTS and not object.__getattribute__(self, "_loading"):
            object.__getattribute__(self, "_dirty").add(name)
        object.__setattr__(self, name, value)

    def _read_file(self):
        """Current on-disk settings, or {} if absent/unreadable."""
        if not os.path.exists(self.config_file):
            return {}, None
        try:
            with open(self.config_file, "r") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("config root is not an object")
        except (OSError, ValueError) as e:
            return {}, f"{self.config_file}: {e}"
        # Ignore unknown keys rather than crashing on an old/newer schema.
        return {k: v for k, v in loaded.items() if k in DEFAULTS}, None

    def load(self):
        values = dict(DEFAULTS)
        on_disk, error = self._read_file()
        # A key written into the file by an older version, to be moved to the keychain.
        loose = {key: on_disk.pop(key) for key in SECRETS if on_disk.get(key)}
        values.update(on_disk)
        # Surfaced in the UI; we still start with defaults rather than dying.
        self.load_error = error

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

        object.__setattr__(self, "_loading", True)
        try:
            for key, value in values.items():
                setattr(self, key, value)
        finally:
            object.__setattr__(self, "_loading", False)
        object.__getattribute__(self, "_dirty").clear()
        if moved or (loose and all(kept[key] for key in loose)):
            try:
                self.save()                 # the same file, without the key in it
            except OSError:
                pass                        # read-only: it goes at the next save

    def as_dict(self):
        return {key: getattr(self, key) for key in DEFAULTS}

    def save(self):
        os.makedirs(self.config_dir, mode=0o700, exist_ok=True)

        # Start from what is on disk now, not from what we loaded at startup, then apply
        # only the keys we deliberately changed. Anything edited elsewhere in the
        # meantime is preserved instead of being reverted.
        dirty = object.__getattribute__(self, "_dirty")
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
        merged.update(on_disk)
        for key in dirty:
            merged[key] = getattr(self, key)
        # Secrets go to the keychain, and only the ones changed here. A copy in the file
        # goes too - unless the keychain could not take it at load (see load) and
        # nothing has replaced it since.
        changed = {key: getattr(self, key) for key in SECRETS if key in dirty}
        for key in SECRETS:
            merged.pop(key, None)
            if (on_disk.get(key) and key not in dirty
                    and not object.__getattribute__(self, "_kept").get(key)):
                merged[key] = on_disk[key]

        fd, tmp_path = tempfile.mkstemp(dir=self.config_dir, prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(merged, f, indent=2)
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 - private all the same
            os.replace(tmp_path, self.config_file)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        # Adopt the merged result so this object reflects what is actually stored.
        object.__setattr__(self, "_loading", True)
        try:
            for key, value in merged.items():
                if key not in SECRETS:
                    setattr(self, key, value)
        finally:
            object.__setattr__(self, "_loading", False)
        dirty.clear()
        self._save_secrets(changed)

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
        copy.config_dir, copy.config_file = self.config_dir, self.config_file
        copy.keychain, copy.keychain_error = self.keychain, self.keychain_error
        copy.load_error = self.load_error
        values = self.as_dict()
        values.update({k: v for k, v in overrides.items() if k in DEFAULTS})
        for key, value in values.items():
            setattr(copy, key, value)
        object.__setattr__(copy, "_loading", False)
        return copy

    def active_text_model(self):
        if self.privacy_mode:
            return self.private_premium_model if self.use_premium else self.private_text_model
        return self.premium_text_model if self.use_premium else self.text_model
