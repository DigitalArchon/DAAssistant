#!/usr/bin/env bash
#
# Start DA Assistant when you log in, run from this checkout. Writes
#
#   ${XDG_CONFIG_HOME:-~/.config}/autostart/da-assistant.desktop
#
# from tools/da-assistant.desktop, naming this checkout's run-assistant and icon, and
# nothing else. The path is written as a desktop entry reads it back - in quotes, and
# escaped for them and then for the file - so a folder called "R&D" or "My Apps" is
# run whole. A path with a % in it is refused: the session would read it as a field
# code. So is one with a line break or another control character. Run it again after
# moving the checkout. For the AppImage, see "Starting automatically" in MANUAL.md.
#
# Usage:
#   tools/install-autostart.sh            install, or write it again
#   tools/install-autostart.sh --remove   delete it again

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/da-assistant.desktop"

case "${1-}" in
  --remove) rm -f "$DEST"; echo "Removed $DEST"; exit 0 ;;
  "") ;;
  *) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 2 ;;
esac

case "$ROOT" in
  *%* | *[[:cntrl:]]*)
    echo "$ROOT has a % or a control character in it, which a desktop entry cannot" >&2
    echo "hold so that the session runs it as it is. Move the checkout; nothing was" >&2
    echo "written." >&2
    exit 1 ;;
esac

# Exec: a backslash before each \ " ` and $ (the Desktop Entry spec's quoting), and
# then every backslash doubled (the file's own escapes). Icon: the second alone.
launcher="$(printf '%s' "$ROOT/run-assistant" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g')"
icon="$(printf '%s' "$ROOT/assistant/data/icons/hicolor/256x256/apps/da-assistant.png" \
  | sed -e 's/\\/\\\\/g')"

mkdir -p "$(dirname "$DEST")"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    "Exec=@LAUNCHER@") printf 'Exec="%s"\n' "$launcher" ;;
    "Icon=@ICON@") printf 'Icon=%s\n' "$icon" ;;
    *) printf '%s\n' "$line" ;;
  esac
done < "$ROOT/tools/da-assistant.desktop" > "$DEST.new"
mv "$DEST.new" "$DEST"
echo "Wrote $DEST: DA Assistant starts from $ROOT at the next login."
