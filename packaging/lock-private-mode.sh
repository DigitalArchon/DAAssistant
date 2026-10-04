#!/usr/bin/env bash
#
# Pin Privacy Mode's end-to-end encryption client, and everything it pulls in, by
# version and hash:  packaging/private-mode.in  ->  packaging/private-mode.lock
#
# Run after changing private-mode.in, and commit the lock with it. Both the AppImage
# build and tools/install-private-mode.sh install only what the lock names, and only if
# every hash matches.
#
# The resolver is uv at a fixed release, checked against its published sha256 before
# it runs - the same one StoryLoom locks the same SDK with.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${BUILD_DIR:-$ROOT/build}/cache"

UV_VERSION="0.12.18"
UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
UV_SHA256="89eadd7c76fc063887959510d5ba0ab1264dfd5f1143b925ddb73021a40acf16"

archive="$CACHE/uv-$UV_VERSION.tar.gz"
mkdir -p "$CACHE"
if [ ! -f "$archive" ]; then
  curl --proto '=https' --tlsv1.2 -fL --retry 3 -o "$archive.part" "$UV_URL"
  mv "$archive.part" "$archive"
fi
if ! echo "$UV_SHA256  $archive" | sha256sum --check --status; then
  rm -f "$archive"
  echo "uv $UV_VERSION does not match its checksum; not running it." >&2
  exit 1
fi
UV="$CACHE/uv-$UV_VERSION/uv"
if [ ! -x "$UV" ]; then
  mkdir -p "$CACHE/uv-$UV_VERSION"
  tar -xzf "$archive" -C "$CACHE/uv-$UV_VERSION" --strip-components=1
fi

# Nothing from this machine's uv or pip settings: no other index, no other cache.
export UV_NO_CONFIG=1 UV_CACHE_DIR="$CACHE/uv" UV_PYTHON_DOWNLOADS=never

# Python 3.12 is what the AppImage bundles and what Ubuntu 24.04 runs from source;
# manylinux_2_28 keeps the compiled wheels (cryptography, pydantic-core) loadable on any
# host the rest of the bundle runs on.
cd "$ROOT/packaging"
"$UV" pip compile --quiet private-mode.in -o private-mode.lock \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --only-binary :all: --generate-hashes \
  --custom-compile-command "packaging/lock-private-mode.sh"
echo "Locked $(grep -c '==' private-mode.lock) packages into packaging/private-mode.lock"
