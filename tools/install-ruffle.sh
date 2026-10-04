#!/usr/bin/env bash
#
# Install Ruffle, which plays the encyclopedia DVD's Flash animations, for running the
# assistant from source. The AppImage already carries it.
#
# Ruffle is a Flash Player emulator written in Rust and compiled to WebAssembly; the
# article viewer runs it in a window of its own. Only the release packaging/ruffle.lock
# names is installed, and only if the download's SHA-512 matches the one pinned there.
# It goes into
#
#   ${XDG_DATA_HOME:-~/.local/share}/da-assistant/ruffle
#
# and nothing else is touched. About 29 MB: two builds of the player, of which the
# browser picks one.
#
# Usage:
#   tools/install-ruffle.sh            install, or reinstall from the current lock
#   tools/install-ruffle.sh --remove   delete it again
#   tools/install-ruffle.sh --to DIR   install into DIR instead (the AppImage build)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/packaging/ruffle.lock"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/da-assistant/ruffle"

case "${1-}" in
  --remove) rm -rf "$DEST"; echo "Removed $DEST"; exit 0 ;;
  --to) DEST="${2:?--to needs a directory}" ;;
  "") ;;
  *) sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 2 ;;
esac

[ -f "$LOCK" ] || { echo "Missing $LOCK" >&2; exit 1; }
value() { sed -n "s/^$1=//p" "$LOCK"; }
version="$(value version)"
url="$(value url)"
sha512="$(value sha512)"
command -v curl >/dev/null || { echo "curl is needed: sudo apt install curl" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work" "$DEST.new"' EXIT

echo "Downloading Ruffle $version"
curl -fsSL --retry 2 -o "$work/ruffle.tgz" "$url"
actual="$(openssl dgst -sha512 -binary "$work/ruffle.tgz" | base64 -w0)"
if [ "$actual" != "$sha512" ]; then
  echo "The download does not match packaging/ruffle.lock; nothing was installed." >&2
  echo "  expected $sha512" >&2
  echo "  got      $actual" >&2
  exit 1
fi

# Into a fresh directory, then swapped in: a failed install leaves the old one alone.
tar -xzf "$work/ruffle.tgz" -C "$work"
rm -rf "$DEST.new"
mkdir -p "$DEST.new"
cp "$work"/package/*.js "$work"/package/*.wasm "$work"/package/LICENSE_* "$DEST.new"/
echo "$version" > "$DEST.new/VERSION"
rm -rf "$DEST"
mv "$DEST.new" "$DEST"
echo "Installed Ruffle $version into $DEST ($(du -sh "$DEST" | cut -f1))"
