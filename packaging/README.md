# Packaging: the AppImage

DA Assistant ships as a single AppImage for x86-64 Linux, built so that anyone can rebuild a
release from its commit and get the same file, byte for byte.

## What's in it, and what comes from the system

| In the AppImage | From the system |
|---|---|
| Python 3.12 ([python-build-standalone](https://github.com/astral-sh/python-build-standalone)) | GTK 3 and **WebKitGTK 4.1** (the chat view; plain text without it) |
| Every Python package, pinned with hashes | The Secret Service keyring (GNOME Keyring, KWallet), for the API key |
| PyGObject and pycairo, built from pinned sources | Screenshot and clipboard tools (`grim`, `spectacle`, `xclip`, ...) |
| Privacy Mode's end-to-end encryption client (`private-mode.lock`) | Certificate authorities (AppRun points `SSL_CERT_FILE` at the system's) |
| Ruffle, for the encyclopedia DVD's animations (`ruffle.lock`) | |
| Fallbacks, used only where the system lacks them: `libgirepository-2.0`, GTK's typelibs and the ones they refer to, the Ayatana tray indicator, libnotify | |

GTK and WebKitGTK are deliberately **not** bundled: they should get the distribution's security
updates, not wait for a DA Assistant release, and a GTK stack from another distribution is what
used to stop the host's WebKit loading at all. The fallbacks are loaded by `assistant/bundle.py`
before anything imports GTK, and only those the host has no copy of; `--probe` lists them on its
*Fallbacks* line.

Licences travel with the image, in `usr/share/licenses/da-assistant`: DA Assistant's own
(GPL-3.0-or-later); in `python/`, the bundled Python's and those of the libraries built into
it (OpenSSL, SQLite, libedit, ncurses, libffi, xz, bzip2, zlib, expat, mpdecimal, libuuid,
Berkeley DB, Tcl/Tk and the X11 libraries Tk links); in `runtime/`, the AppImage runtime's and
those of what is linked into it (libfuse, squashfuse, zstd, zlib, mimalloc, musl); pycairo's
and tinfoil-ehbp's, whose wheels carry none; and in `fallbacks/`, the Ubuntu copyright file of
every package a fallback came from. The Ayatana tray indicator is GPL-3; its ido and
appindicator libraries and libdbusmenu are part GPL-3, part LGPL; libnotify and the typelibs
are LGPL: all of it within what GPL-3.0-or-later allows. Beside them, `fallbacks/SOURCES`
names the apt snapshot their source is in, and `SOURCES` the archive every other text was
taken from; those texts are kept in `packaging/licenses`. Every other Python package carries
its licence in its `dist-info` (the build stops if one has none), and Ruffle its own
`LICENSE` files.

The bundled PyGObject needs GLib 2.80 or newer, which sets the oldest supported systems:
Ubuntu 24.04 / Mint 22, Debian 13, and current Fedora and Arch. The AppImage uses the static
type 2 runtime, so it doesn't need `libfuse2`.

## Building

You need rootless [Podman](https://podman.io) (`sudo apt install podman`), with SELinux
enforcing or not.

```bash
packaging/build-appimage.sh build    # dist/DA_Assistant-<version>-x86_64.AppImage (+ .sha256, .zsync)
packaging/build-appimage.sh check    # build twice from scratch, compare: must be identical
packaging/test-appimage.sh           # smoke-test it on Ubuntu, Debian, Fedora, Arch, CachyOS
packaging/build-appimage.sh lock     # lock what the .in files add (review the diff, commit)
UPGRADE=1 packaging/build-appimage.sh lock    # re-resolve every pin to today's newest
packaging/lock-private-mode.sh       # the same for private-mode.in (UPGRADE=1 too)
DIST=/some/dir packaging/build-appimage.sh check   # results there instead of dist/
```

`lock` and `lock-private-mode.sh` keep the pins already in a lock, so a package added to a
`.in` file is locked without moving everything else, and a pin moves only when the `.in`
file rules it out: a floor such as `pyasn1>=0.6.5` is how a security fix gets in.
`UPGRADE=1` is for a deliberate update of the lot.

Only committed files are built, each read from git's objects exactly as HEAD has it (not
through `git archive`, which would apply your git's line-ending settings, filters and
attributes first), and every timestamp is HEAD's commit time (`SOURCE_DATE_EPOCH`). Builds run in `packaging/appimage/Containerfile`: Ubuntu 24.04 pinned
by digest, with packages from the Ubuntu archive as it was at a fixed moment
([snapshot.ubuntu.com](https://snapshot.ubuntu.com)). Everything is downloaded over https,
never plain http, not even through a redirect; downloads are cached in
`~/.cache/da-assistant-appimage` and verified on every build.

## What is pinned, and where

| What | Pinned in |
|---|---|
| Base image (digest), apt snapshot, Python build, AppImage runtime | `appimage/pins.sh` |
| Runtime Python packages (wheels, with hashes) | `appimage/requirements.lock`, from `requirements.in` |
| PyGObject, pycairo (source archives, with hashes) | `appimage/sdists.lock`, from `sdists.in` |
| meson, meson-python, ninja, setuptools (with hashes) | `appimage/build-requirements.lock` |
| pip and pip-tools, which write the locks above, and what they run on (with hashes) | `appimage/lock-requirements.lock`, from `lock-requirements.in` |
| Privacy Mode's client (wheels, with hashes) | `private-mode.lock`, by `lock-private-mode.sh`, resolved against `requirements.lock` |
| Ruffle (npm tarball, SHA-512) | `ruffle.lock` |
| The fallback libraries and typelibs | the apt snapshot in `appimage/pins.sh` |
| The licence texts of the Python build, the runtime and tinfoil-ehbp | `licenses/`, each with its archive and checksum in `licenses/SOURCES` |

Changing any of these changes the AppImage, which is the point: an update is a reviewed commit.

The Privacy Mode client is installed in a directory of its own, ahead of the app's
`site-packages` on the path (`assistant/vendored.py`), so where both have a package its copy
is the one imported. `lock-private-mode.sh` therefore pins every package `requirements.lock`
also has to the same version, and the build stops if the two ever disagree. (3.4.0 shipped
cryptography 50.0.2 and ran 50.0.1 - an older OpenSSL - from the client's directory.)
Runtime packages are installed only as prebuilt wheels; PyGObject and pycairo, which publish
none for Linux, are built from their pinned sources with the pinned tools.

What keeps the result the same wherever it is built: each source is unpacked into a fixed
directory and the build path is mapped out of compiled code (`-ffile-prefix-map`); bytecode is
compiled once, with a fixed hash seed, as *unchecked-hash* `.pyc` files, which store their
source's hash where a date would go and are loaded without being checked against the source
(nothing can change it on the read-only image); files are owned by root with no extended
attributes, and dated `SOURCE_DATE_EPOCH`.

Nor does the build's own folder get in. Each `.pyc` names its source as it is inside the image
(`/usr/python/lib/...`); pip's `direct_url.json` files and the console scripts it writes
(`idna`, `keyring`, ...), whose `#!` line names the build's Python, are left out; and the build
stops if anything in the image still names that folder.

## Verifying a release

1. Check out the release's tag, with no line-ending conversion:
   `git clone -c core.autocrlf=false https://github.com/DigitalArchon/DAAssistant`, then
   `git checkout v<version>` in it.
2. `packaging/build-appimage.sh build`.
3. Compare `sha256sum dist/DA_Assistant-*.AppImage` with the release's `.sha256`.

Your git settings cannot change what is built: the build reads each file from git's objects as
the commit has it. They can change the checkout, though, and the build scripts run from there;
with `core.autocrlf=true` they would have CRLF line endings and not start at all, hence the
`-c` in step 1.

If the hashes differ, `diffoscope` on the two files shows where.

## Releasing

1. Update `__version__` in `assistant/__init__.py`, commit, tag.
2. `packaging/build-appimage.sh check` (reproducible), then `packaging/test-appimage.sh`.
3. Attach the AppImage, its `.sha256` and its `.zsync` to the release.

The AppImage carries update information
(`gh-releases-zsync|DigitalArchon|DAAssistant|latest|DA_Assistant-*-x86_64.AppImage.zsync`):
AppImageUpdate, Gear Lever and AppImageLauncher read it, and fetch from the latest release's
`.zsync` only the blocks that changed. The app itself never looks for updates.

Rebuild and re-release when a pinned dependency has a security fix: the locks don't move by
themselves. GTK and WebKitGTK update with the system.
