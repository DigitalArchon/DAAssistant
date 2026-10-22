#!/bin/bash
# Runs inside the build container (see Containerfile); started by packaging/build-appimage.sh.
#   build.sh build   /src.tar (git archive of the commit) -> /out/DA_Assistant-<version>-x86_64.AppImage
#   build.sh lock    re-resolve the *.lock files into /out (review, then commit them)
# Inputs are pinned (pins.sh, *.lock with hashes, packaging/private-mode.lock, packaging/ruffle.lock)
# and every timestamp is SOURCE_DATE_EPOCH, so the same commit gives the same AppImage, byte for byte.
set -euo pipefail
umask 022
export LC_ALL=C.UTF-8 TZ=UTC PYTHONHASHSEED=0 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
    PIP_CACHE_DIR=/cache/pip
: "${SOURCE_DATE_EPOCH:?}"

mode="${1:-build}"
W=/work
rm -rf "$W" && mkdir -p "$W/src" "$W/wheels" "$W/sdists" "$W/AppDir/usr"
tar -xf /src.tar -C "$W/src"
P="$W/src/packaging/appimage"
# shellcheck source=pins.sh
. "$P/pins.sh"

fetch() {  # url sha256 dest: download once into the cache, always verify
    local url=$1 sum=$2 dest=$3
    if [ ! -f "$dest" ] || ! echo "$sum  $dest" | sha256sum -c --quiet - 2>/dev/null; then
        curl -fsSL --retry 3 -o "$dest.part" "$url"
        echo "$sum  $dest.part" | sha256sum -c --quiet -
        mv "$dest.part" "$dest"
    fi
}
mkdir -p /cache
fetch "$PBS_URL" "$PBS_SHA256" /cache/python.tar.gz
fetch "$RUNTIME_URL" "$RUNTIME_SHA256" /cache/runtime-x86_64

# a Python for building (with the build tools) and a clean copy that becomes the AppImage's
mkdir -p "$W/buildpy"
tar -xzf /cache/python.tar.gz -C "$W/buildpy" --strip-components=1 --no-same-owner
tar -xzf /cache/python.tar.gz -C "$W/AppDir/usr" --no-same-owner        # -> usr/python
"$W/buildpy/bin/python3" -m venv "$W/venv"
V="$W/venv/bin"
export PATH="$V:$PATH"      # meson and ninja for the source builds

if [ "$mode" = lock ]; then
    # The resolver's own TLS trust while it reads PyPI: the container's CA store (the snapshot's
    # ca-certificates, which blacklists emSign ECC Root CA - G3 since 20260816ubuntu0.24.04.1),
    # never pip's vendored certifi, which pip reads beside the system store; pip 26.2.2 is the
    # first with the root gone from its own copy too. The build itself needs none of this: every
    # package it installs is checked against the lock's hashes.
    export PIP_CERT=/etc/ssl/certs/ca-certificates.crt
    "$V/pip" install -q "pip==26.2.2" "pip-tools==7.6.1"
    cd "$P"
    for name in requirements build-requirements sdists; do
        "$V/pip-compile" -q --generate-hashes --allow-unsafe --no-strip-extras \
            --output-file "/out/$name.lock" "$name.in"
    done
    sed -i "s#/out/#packaging/appimage/#g" /out/*.lock
    exit 0
fi

: "${VERSION:?}"
"$V/pip" install -q --require-hashes --no-deps -r "$P/build-requirements.lock"

# PyGObject and pycairo have no Linux wheels: build them from their pinned sources against the
# GLib 2.80 / girepository-2.0 headers. They load the host's GLib and GTK at run time.
fetch_sdists() {  # lock: fetch the pinned source archives from PyPI, checked against the lock
    "$W/buildpy/bin/python3" - "$W/sdists" "$1" <<'PY'
import hashlib, json, re, sys, urllib.request
dest, lockfile = sys.argv[1], sys.argv[2]
lock = open(lockfile).read()
for name, version, hashes in re.findall(r"^([\w.-]+)==([\w.]+)((?:\s*\\\s*--hash=sha256:\w+)+)", lock, re.M):
    meta = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json"))
    sdist = next(u for u in meta["urls"] if u["packagetype"] == "sdist")
    data = urllib.request.urlopen(sdist["url"]).read()
    if hashlib.sha256(data).hexdigest() not in re.findall(r"sha256:(\w+)", hashes):
        sys.exit(f"{sdist['filename']}: its hash isn't in {lockfile}")
    # saved under a normalised name, which the build steps below look for
    open(f"{dest}/{name.lower().replace('-', '_')}-{version}.tar.gz", "wb").write(data)
PY
}
fetch_sdists "$P/sdists.lock"
# Source paths end up in compiled code (__FILE__ in assertion messages), so each source is unpacked
# into a fixed directory (pip would use a random one) and the build path is mapped out entirely.
export CFLAGS="-ffile-prefix-map=$W/=" CXXFLAGS="-ffile-prefix-map=$W/="
unpack() {  # name -> fixed source directory
    mkdir -p "$W/srcs/$1"
    tar -xzf "$W/sdists/$1"-*.tar.gz -C "$W/srcs/$1" --strip-components=1 --no-same-owner
    echo "$W/srcs/$1"
}
build_sdist() {  # name: fixed source and build directories, stripped
    "$V/pip" wheel -q --no-build-isolation --no-deps -w "$W/wheels" \
        -Cbuild-dir="$W/build-$1" -Cinstall-args=--strip "$(unpack "$1")"
}
build_sdist pycairo
"$V/pip" install -q --no-deps "$W/wheels"/pycairo-*.whl     # PyGObject builds its cairo support against it
build_sdist pygobject

# the AppImage's Python: locked wheels only (hash-checked), then the two built here
PY="$W/AppDir/usr/python/bin/python3"
"$PY" -m pip install -q --no-compile --no-deps --require-hashes --only-binary :all: -r "$P/requirements.lock"
"$PY" -m pip install -q --no-compile --no-deps "$W/wheels"/*.whl

# The app, as the source tree has it: run-assistant beside the package, which it puts on the path.
A="$W/AppDir/usr/lib/da-assistant"
mkdir -p "$A"
cp -r "$W/src/assistant" "$W/src/run-assistant" "$A/"
# Privacy Mode's end-to-end encryption client, beside the package where assistant/vendored.py
# looks first: wheels only, each checked against packaging/private-mode.lock. Its console
# scripts would only name the build's Python.
"$PY" -m pip install -q --no-compile --no-deps --require-hashes --only-binary :all: \
    --target "$A/private-mode" -r "$W/src/packaging/private-mode.lock"
rm -rf "$A/private-mode/bin"
# Ruffle, for the encyclopedia DVD's Flash animations; checked against packaging/ruffle.lock
"$W/src/tools/install-ruffle.sh" --to "$A/ruffle" >/dev/null
"$PY" -m pip uninstall -q -y pip                  # nothing is installed at run time

# Fallbacks for what a host may lack, one directory each, used only where the host has none
# (assistant/bundle.py; the host's own always win):
F="$W/AppDir/usr/lib/fallback"
L=/usr/lib/x86_64-linux-gnu
# - girepository-2.0, which PyGObject is built against: Debian and Ubuntu package it apart from GLib
install -D -m 644 "$(readlink -f $L/libgirepository-2.0.so.0)" "$F/libgirepository-2.0.so.0"
# - typelibs: GLib's own and the base ones GTK's refer to (Debian and Arch ship them apart), GTK's
#   own (Debian ships them apart, and not every KDE install has them), the tray indicator's and
#   libnotify's
for pkg in gir1.2-glib-2.0 gir1.2-freedesktop gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 gir1.2-pango-1.0 \
           gir1.2-harfbuzz-0.0 gir1.2-atk-1.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7; do
    for t in $(dpkg -L "$pkg" | grep '\.typelib$'); do
        install -D -m 644 "$t" "$F/typelibs/$(basename "$t" .typelib)/$(basename "$t")"
    done
done
# - the libraries behind the last two typelibs, by soname, in the order they load
for so in libayatana-ido3-0.4.so.0 libdbusmenu-glib.so.4 libdbusmenu-gtk3.so.4 \
          libayatana-indicator3.so.7 libayatana-appindicator3.so.1 libnotify.so.4; do
    install -D -m 644 "$(readlink -f "$L/$so")" "$F/libs/$so"
done

# DA Assistant's licence, and the copyright notice of each Ubuntu package a fallback came from
# (the Python packages carry theirs in their dist-info, Ruffle its LICENSE files)
D="$W/AppDir/usr/share/licenses/da-assistant"
install -D -m 644 "$W/src/LICENSE" "$D/LICENSE"
for pkg in $(find "$F" -type f -printf '%f\n' | while read -r f; do
                 dpkg -S "*/$f" | cut -d: -f1 | tr "," " "; done | sort -u); do
    install -D -m 644 "/usr/share/doc/$pkg/copyright" "$D/fallbacks/$pkg.copyright"
done

find "$W/AppDir" -name direct_url.json -delete    # build paths
find "$W/AppDir" -name __pycache__ -type d -prune -exec rm -rf {} +
# bytecode keyed to the source's hash, not its timestamp: reproducible, and valid on a read-only mount
"$PY" -m compileall -q -j1 --invalidation-mode unchecked-hash "$W/AppDir/usr/python/lib" "$A" >/dev/null

# AppDir layout (AppImage spec): entry point, desktop entry, icon, AppStream metadata
APP_ID="$(sed -n 's/^APP_ID = "\(.*\)"/\1/p' "$W/src/assistant/__init__.py")"
DATE="$(date -u -d "@$SOURCE_DATE_EPOCH" +%F)"
install -m 755 "$P/AppRun" "$W/AppDir/AppRun"
grep -v '^X-AppImage-' "$P/da-assistant.desktop" > "$W/AppDir/da-assistant.desktop"
printf '%s\n' "X-AppImage-Name=DA Assistant" "X-AppImage-Version=$VERSION" "X-AppImage-Arch=x86_64" \
    >> "$W/AppDir/da-assistant.desktop"
desktop-file-validate "$W/AppDir/da-assistant.desktop"
install -d "$W/AppDir/usr/share/applications"
ln -s ../../../da-assistant.desktop "$W/AppDir/usr/share/applications/da-assistant.desktop"
# The icons the app itself uses (packaging/make-icon.py), rather than a second rendering of them.
cp -r "$W/src/assistant/data/icons" "$W/AppDir/usr/share/"
install -m 644 "$W/src/assistant/data/icons/hicolor/256x256/apps/da-assistant.png" "$W/AppDir/"
ln -s da-assistant.png "$W/AppDir/.DirIcon"
install -d "$W/AppDir/usr/share/metainfo"
cat > "$W/AppDir/usr/share/metainfo/$APP_ID.appdata.xml" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>$APP_ID</id>
  <name>DA Assistant</name>
  <summary>Translate and query screen regions and clipboard text</summary>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>GPL-3.0-or-later</project_license>
  <description>
    <p>
      Sends a screen region, an image or clipboard text to an OpenAI-compatible API and
      shows the answer: translation, OCR, or a question of your own.
    </p>
  </description>
  <launchable type="desktop-id">da-assistant.desktop</launchable>
  <categories>
    <category>Utility</category>
  </categories>
  <releases>
    <release version="$VERSION" date="$DATE"/>
  </releases>
</component>
XML
grep -qx "X-AppImage-Version=$VERSION" "$W/AppDir/da-assistant.desktop"

chmod -R u+rwX,go+rX,go-w "$W/AppDir"
find "$W/AppDir" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
# (mksquashfs takes every timestamp from SOURCE_DATE_EPOCH by itself)
mksquashfs "$W/AppDir" "$W/app.squashfs" -quiet -noappend -root-owned -no-xattrs \
    -comp zstd -Xcompression-level 19 -b 1M

NAME="DA_Assistant-$VERSION-x86_64.AppImage"
cat /cache/runtime-x86_64 "$W/app.squashfs" > "/out/$NAME"
chmod 755 "/out/$NAME"
(cd /out && sha256sum "$NAME" > "$NAME.sha256" && cat "$NAME.sha256")
