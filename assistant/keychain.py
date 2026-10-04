"""Where the API key is kept: the desktop's keyring, never config.json.

On Linux that is the Secret Service - GNOME Keyring, or KWallet on Plasma - reached
through the `keyring` package. The key is encrypted at rest there and unlocked with the
login session, where a file in ~/.config is readable by anything running as the user
and travels with every backup of the home folder.

With no keyring to hand (none installed, or none unlocked), a key typed into Settings
is used until the app quits and not stored anywhere; problem() says why, for Settings
and Diagnostics to show.
"""

SERVICE = "da-assistant"


class KeychainError(Exception):
    pass


class SystemKeychain:
    """The desktop's keyring. Every failure comes back as KeychainError: a locked
    keyring, a refused unlock prompt and a missing D-Bus all mean the same here."""

    def problem(self):
        """Why secrets cannot be stored, or None when they can."""
        try:
            import keyring
            backend = keyring.get_keyring()
        except Exception as e:      # an import error, a broken backend: all reportable
            return f"No keyring available: {e}"
        if getattr(backend, "priority", 0) < 1:
            return ("No secure keyring available. Install and unlock a Secret Service "
                    "provider such as GNOME Keyring or KWallet.")
        return None

    def describe(self):
        """For Diagnostics: the keyring in use, or why there is none."""
        problem = self.problem()
        if problem:
            return problem
        import keyring
        backend = keyring.get_keyring()
        names = [getattr(b, "name", type(b).__name__)
                 for b in getattr(backend, "backends", [backend])]
        return ", ".join(names)

    def get(self, name):
        try:
            import keyring
            return keyring.get_password(SERVICE, name)
        except Exception as e:
            raise KeychainError(f"Could not read the keyring: {e}") from e

    def set(self, name, value):
        problem = self.problem()
        if problem:
            raise KeychainError(problem)
        try:
            import keyring
            keyring.set_password(SERVICE, name, value)
        except Exception as e:
            raise KeychainError(f"Could not write to the keyring: {e}") from e

    def delete(self, name):
        try:
            import keyring
            from keyring.errors import PasswordDeleteError
        except Exception as e:
            raise KeychainError(f"No keyring available: {e}") from e
        try:
            keyring.delete_password(SERVICE, name)
        except PasswordDeleteError:
            pass                            # nothing stored: already as asked
        except Exception as e:
            raise KeychainError(f"Could not write to the keyring: {e}") from e


class MemoryKeychain:
    """Secrets for this process only. What a Config with a file of its own uses, so
    that one made for a test or a second profile never touches the user's keyring."""

    def __init__(self):
        self.secrets = {}

    def problem(self):
        return None

    def describe(self):
        return "memory only (a config with its own file)"

    def get(self, name):
        return self.secrets.get(name)

    def set(self, name, value):
        self.secrets[name] = value

    def delete(self, name):
        self.secrets.pop(name, None)
