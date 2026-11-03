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
(GPL-3.0-or-later), and the Ubuntu copyright file of every package a fallback came from (the
Ayatana indicator and libnotify are LGPL; their source is in the apt snapshot the build
pins). Each Python package carries its licence in its `dist-info`, and Ruffle its own
`LICENSE` files.

The bundled PyGObject needs GLib 2.80 or newer, which sets the oldest supported systems:
Ubuntu 24.04 / Mint 22, Debian 13, and current Fedora and Arch. The AppImage uses the static
type 2 runtime, so it doesn't need `libfuse2`.

## Building

You need rootless [Podman](https://podman.io) (`sudo apt install podman`).

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

Only committed files are built (`git archive HEAD`), and every timestamp is HEAD's commit
time (`SOURCE_DATE_EPOCH`). Builds run in `packaging/appimage/Containerfile`: Ubuntu 24.04 pinned
by digest, with packages from the Ubuntu archive as it was at a fixed moment
([snapshot.ubuntu.com](https://snapshot.ubuntu.com)). Downloads are cached in
`~/.cache/da-assistant-appimage` and verified on every build.

## What is pinned, and where

| What | Pinned in |
|---|---|
| Base image (digest), apt snapshot, Python build, AppImage runtime | `appimage/pins.sh` |
| Runtime Python packages (wheels, with hashes) | `appimage/requirements.lock`, from `requirements.in` |
| PyGObject, pycairo (source archives, with hashes) | `appimage/sdists.lock`, from `sdists.in` |
| meson, meson-python, ninja, setuptools (with hashes) | `appimage/build-requirements.lock` |
| Privacy Mode's client (wheels, with hashes) | `private-mode.lock`, by `lock-private-mode.sh`, resolved against `requirements.lock` |
| Ruffle (npm tarball, SHA-512) | `ruffle.lock` |
| The fallback libraries and typelibs | the apt snapshot in `appimage/pins.sh` |

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
compiled once, hash-checked rather than dated, with a fixed hash seed; files are owned by root
with no extended attributes, and dated `SOURCE_DATE_EPOCH`; pip's `direct_url.json` files, which
name the build folder, are removed.

## Verifying a release

1. Check out the release's tag.
2. `packaging/build-appimage.sh build`.
3. Compare `sha256sum dist/DA_Assistant-*.AppImage` with the release's `.sha256`.

If they differ, `diffoscope` on the two files shows where.

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
