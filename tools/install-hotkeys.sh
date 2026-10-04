#!/usr/bin/env bash
#
# Install global hotkeys for DA Assistant into the desktop's own keyboard
# settings.
#
# Why this exists: a Wayland client cannot grab global keys for itself. The portal
# interface that would allow it (org.freedesktop.portal.GlobalShortcuts) only has a
# backend on KDE Plasma 6 and Hyprland - Cinnamon and GNOME do not provide one. The
# portable answer is to register bindings with the desktop, pointing at
# `run-assistant --action=NAME`, which hands the action to the running instance over
# D-Bus and exits.
#
# This also works on X11, and is more reliable there than in-process key grabs.
#
# Usage:
#   ./install-hotkeys.sh              show what would change (default; changes nothing)
#   ./install-hotkeys.sh --apply      write the bindings
#   ./install-hotkeys.sh --remove     remove bindings previously written by this script
#   ./install-hotkeys.sh --desktop=kde  force a desktop instead of auto-detecting

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Bindings outlive this process, so they have to name a path that outlives it too. In an
# AppImage the script sits on a mount that disappears at exit; $APPIMAGE is the .AppImage
# file itself, which is what the desktop must be told to run.
if [ -n "${APPIMAGE:-}" ]; then
  LAUNCHER="$APPIMAGE"
else
  LAUNCHER="$(cd "$SCRIPT_DIR/.." && pwd)/run-assistant"
fi

MODE="print"
FORCE_DESKTOP=""

for arg in "$@"; do
  case "$arg" in
    --apply)   MODE="apply" ;;
    --remove)  MODE="remove" ;;
    --print|--dry-run) MODE="print" ;;
    --desktop=*) FORCE_DESKTOP="${arg#--desktop=}" ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# action name | human label | key combination (in each desktop's own notation below)
ACTIONS=(
  "translate_text|Translate clipboard text|1"
  "ocr_translate|Translate image selection|2"
  "query_text|Query clipboard text|3"
  "query_image|Query image selection|4"
  "ocr_only|OCR only|5"
  "look_up_word|Look up word|6"
  "search_encyclopedia|Search Encyclopedia|7"
  "ask_encyclopedia|Ask Encyclopedia|8"
)

# The encyclopedia's two only when it is turned on in Settings > Reference: for anyone
# without the DVD they would just take Ctrl+Shift+7 and 8 from other programs. Those
# left out are cleared, so that running this again after turning it off removes them.
CONFIG="$HOME/.config/da-assistant/config.json"
INSTALL=()
SKIPPED=()
for entry in "${ACTIONS[@]}"; do
  case "$entry" in
    *_encyclopedia\|*)
      if grep -Eq '"encyclopedia_enabled": *true' "$CONFIG" 2>/dev/null; then
        INSTALL+=("$entry")
      else
        SKIPPED+=("$entry")
      fi ;;
    *) INSTALL+=("$entry") ;;
  esac
done

PREFIX="da-assistant"
BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RESET=$'\033[0m'

info() { printf '%s%s%s\n' "$BLUE" "$*" "$RESET"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$*" "$RESET"; }

run() {
  if [ "$MODE" = "print" ]; then
    printf '  %s\n' "$*"
  else
    "$@" >/dev/null 2>&1 || { warn "  command failed: $*"; return 1; }
  fi
}

detect_desktop() {
  [ -n "$FORCE_DESKTOP" ] && { echo "$FORCE_DESKTOP"; return; }
  [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] && { echo hyprland; return; }
  [ -n "${SWAYSOCK:-}" ] && { echo sway; return; }
  local raw
  raw="$(printf '%s' "${XDG_CURRENT_DESKTOP:-${XDG_SESSION_DESKTOP:-${DESKTOP_SESSION:-}}}" \
         | tr '[:upper:]' '[:lower:]')"
  case "$raw" in
    *cinnamon*) echo cinnamon ;;
    *kde*|*plasma*) echo kde ;;
    *gnome*|*unity*) echo gnome ;;
    *hyprland*) echo hyprland ;;
    *sway*) echo sway ;;
    *mate*) echo mate ;;
    *xfce*) echo xfce ;;
    *) echo unknown ;;
  esac
}

# ------------------------------------------------------- the desktop's own list
#
# Cinnamon and GNOME each keep one list naming every custom shortcut, and a shortcut
# that is not on it does not exist. It is the user's list, not ours: this script used to
# write it as exactly our five entries, and --remove wrote it as empty, either of which
# silently unregistered every shortcut the user had made for anything else.

# The entries of a gsettings string list, one per line. `@as []` is how it prints empty.
list_entries() {
  gsettings get "$1" "$2" 2>/dev/null |
    sed -e 's/^@as //' -e 's/^\[//' -e 's/\]$//' | tr ',' '\n' |
    sed -e "s/^ *'//" -e "s/' *$//" -e '/^ *$/d'
}

# merged_list SCHEMA KEY [ENTRY...] - the list as it stands, less anything of ours,
# plus ENTRY... So installing twice adds nothing twice, and removing passes no entries.
merged_list() {
  local schema="$1" key="$2" entry out=()
  shift 2
  while IFS= read -r entry; do
    case "$entry" in *"${PREFIX}-"*) continue ;; esac
    out+=("$entry")
  done < <(list_entries "$schema" "$key")
  out+=("$@")

  local list="["
  for entry in ${out[@]+"${out[@]}"}; do list="${list}'${entry}', "; done
  printf '%s]' "${list%, }"
}

# --------------------------------------------------------------------- Cinnamon

install_cinnamon() {
  local schema_list="org.cinnamon.desktop.keybindings"
  local base="/org/cinnamon/desktop/keybindings/custom-keybindings"
  local schema_item="org.cinnamon.desktop.keybindings.custom-keybinding"
  local names=()

  for entry in "${INSTALL[@]}"; do
    IFS='|' read -r action label key <<< "$entry"
    local name="${PREFIX}-${action}"
    local path="${base}/${name}/"
    # The name, not the path. Cinnamon builds the path itself, as base/<entry>/, so a
    # full path in the list pointed at nothing and the keys never fired.
    names+=("$name")
    info "  ${label}  ->  Ctrl+Shift+${key}"
    run gsettings set "${schema_item}:${path}" name "DA Assistant: ${label}"
    run gsettings set "${schema_item}:${path}" command "${LAUNCHER} --action=${action}"
    # Cinnamon stores the binding as a list of strings.
    run gsettings set "${schema_item}:${path}" binding "['<Control><Shift>${key}']"
  done
  for entry in ${SKIPPED[@]+"${SKIPPED[@]}"}; do
    IFS='|' read -r action _ _ <<< "$entry"
    run gsettings reset-recursively "${schema_item}:${base}/${PREFIX}-${action}/"
  done

  info "  adding them to the list of custom keybindings"
  run gsettings set "$schema_list" custom-list \
      "$(merged_list "$schema_list" custom-list "${names[@]}")"
}

remove_cinnamon() {
  local base="/org/cinnamon/desktop/keybindings/custom-keybindings"
  local schema_item="org.cinnamon.desktop.keybindings.custom-keybinding"
  for entry in "${ACTIONS[@]}"; do
    IFS='|' read -r action _ _ <<< "$entry"
    run gsettings reset-recursively "${schema_item}:${base}/${PREFIX}-${action}/"
  done
  run gsettings set org.cinnamon.desktop.keybindings custom-list \
      "$(merged_list org.cinnamon.desktop.keybindings custom-list)"
}

# ------------------------------------------------------------------------ GNOME

install_gnome() {
  local schema="org.gnome.settings-daemon.plugins.media-keys"
  local item="${schema}.custom-keybinding"
  local base="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
  local paths=()

  for entry in "${INSTALL[@]}"; do
    IFS='|' read -r action label key <<< "$entry"
    local path="${base}/${PREFIX}-${action}/"
    paths+=("$path")
    info "  ${label}  ->  Ctrl+Shift+${key}"
    run gsettings set "${item}:${path}" name "DA Assistant: ${label}"
    run gsettings set "${item}:${path}" command "${LAUNCHER} --action=${action}"
    # GNOME stores the binding as a single string, unlike Cinnamon.
    run gsettings set "${item}:${path}" binding "<Control><Shift>${key}"
  done
  for entry in ${SKIPPED[@]+"${SKIPPED[@]}"}; do
    IFS='|' read -r action _ _ <<< "$entry"
    run gsettings reset-recursively "${item}:${base}/${PREFIX}-${action}/"
  done

  info "  adding them to the list of custom keybindings"
  run gsettings set "$schema" custom-keybindings \
      "$(merged_list "$schema" custom-keybindings "${paths[@]}")"
}

remove_gnome() {
  local item="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
  local base="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
  for entry in "${ACTIONS[@]}"; do
    IFS='|' read -r action _ _ <<< "$entry"
    run gsettings reset-recursively "${item}:${base}/${PREFIX}-${action}/"
  done
  run gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
      "$(merged_list org.gnome.settings-daemon.plugins.media-keys custom-keybindings)"
}

# -------------------------------------------------------------------------- KDE
#
# Plasma 6 removed khotkeys, so the Plasma 5 recipe of writing khotkeysrc no longer
# applies. The supported route now is a .desktop file carrying X-KDE-Shortcuts, which
# kglobalaccel picks up once the sycoca cache is rebuilt. We also write the matching
# kglobalshortcutsrc entry, because on some Plasma builds the .desktop alone does not
# take effect until the shortcut is touched in System Settings.

kde_tool() { command -v "${1}6" >/dev/null 2>&1 && echo "${1}6" || echo "${1}5"; }

install_kde() {
  local appdir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  local kwrite; kwrite="$(kde_tool kwriteconfig)"
  local sycoca; sycoca="$(kde_tool kbuildsycoca)"

  [ "$MODE" = "apply" ] && mkdir -p "$appdir"

  for entry in ${SKIPPED[@]+"${SKIPPED[@]}"}; do
    IFS='|' read -r action _ _ <<< "$entry"
    if [ "$MODE" = "print" ]; then
      printf '  remove %s/%s-%s.desktop\n' "$appdir" "$PREFIX" "$action"
    else
      rm -f "${appdir}/${PREFIX}-${action}.desktop"
    fi
    run "$kwrite" --file kglobalshortcutsrc \
        --group "services" --group "${PREFIX}-${action}.desktop" --key "_launch" --delete
  done

  for entry in "${INSTALL[@]}"; do
    IFS='|' read -r action label key <<< "$entry"
    local file="${appdir}/${PREFIX}-${action}.desktop"
    local shortcut="Ctrl+Shift+${key}"
    info "  ${label}  ->  ${shortcut}"

    if [ "$MODE" = "print" ]; then
      printf '  write %s containing X-KDE-Shortcuts=%s\n' "$file" "$shortcut"
    else
      cat > "$file" <<DESKTOP
[Desktop Entry]
Type=Application
Name=DA Assistant: ${label}
Exec=${LAUNCHER} --action=${action}
Icon=accessories-text-editor
NoDisplay=true
StartupNotify=false
Terminal=false
X-KDE-Shortcuts=${shortcut}
DESKTOP
      chmod +x "$file"
    fi

    run "$kwrite" --file kglobalshortcutsrc \
        --group "services" --group "${PREFIX}-${action}.desktop" \
        --key "_launch" "${shortcut},none,DA Assistant: ${label}"
    run "$kwrite" --file kglobalshortcutsrc \
        --group "services" --group "${PREFIX}-${action}.desktop" \
        --key "_k_friendly_name" "DA Assistant: ${label}"
  done

  info "  rebuilding the application cache"
  run "$sycoca" --noincremental

  if [ "$MODE" = "apply" ]; then
    local major; major="$(kde_tool kwriteconfig)"; major="${major##kwriteconfig}"
    warn ""
    warn "  KDE caches global shortcuts in a running daemon. If the keys do not work"
    warn "  immediately, restart it:"
    warn "      kquitapp${major} kglobalaccel && sleep 2 && kglobalaccel${major} &"
    warn "  or log out and back in. Verify under:"
    warn "      System Settings > Keyboard > Shortcuts  (search for 'DA Assistant')"
  fi
}

remove_kde() {
  local appdir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  local kwrite; kwrite="$(kde_tool kwriteconfig)"
  for entry in "${ACTIONS[@]}"; do
    IFS='|' read -r action _ _ <<< "$entry"
    if [ "$MODE" = "print" ]; then
      printf '  remove %s/%s-%s.desktop\n' "$appdir" "$PREFIX" "$action"
    else
      rm -f "${appdir}/${PREFIX}-${action}.desktop"
    fi
    run "$kwrite" --file kglobalshortcutsrc \
        --group "services" --group "${PREFIX}-${action}.desktop" --key "_launch" --delete
  done
  run "$(kde_tool kbuildsycoca)" --noincremental
}

# ----------------------------------------------------------------- Hyprland/Sway
#
# Both are configured from a text file, so we emit a snippet rather than editing the
# user's config behind their back.

snippet_hyprland() {
  echo "# --- DA Assistant hotkeys (generated by tools/install-hotkeys.sh) ---"
  for entry in "${INSTALL[@]}"; do
    IFS='|' read -r action label key <<< "$entry"
    printf 'bind = CTRL SHIFT, %s, exec, %s --action=%s  # %s\n' \
           "$key" "$LAUNCHER" "$action" "$label"
  done
  echo "# --- end DA Assistant hotkeys ---"
}

snippet_sway() {
  echo "# --- DA Assistant hotkeys (generated by tools/install-hotkeys.sh) ---"
  for entry in "${INSTALL[@]}"; do
    IFS='|' read -r action label key <<< "$entry"
    printf 'bindsym Ctrl+Shift+%s exec %s --action=%s  # %s\n' \
           "$key" "$LAUNCHER" "$action" "$label"
  done
  echo "# --- end DA Assistant hotkeys ---"
}

install_wm() {
  local kind="$1" conf snippet
  if [ "$kind" = hyprland ]; then
    conf="${HOME}/.config/hypr/hyprland.conf"; snippet="$(snippet_hyprland)"
  else
    conf="${HOME}/.config/sway/config"; snippet="$(snippet_sway)"
  fi

  echo "$snippet"
  echo

  if [ "$MODE" = "apply" ]; then
    if [ ! -f "$conf" ]; then
      warn "  $conf does not exist - add the lines above to your config yourself."
      return
    fi
    if grep -q "DA Assistant hotkeys" "$conf"; then
      warn "  $conf already contains these bindings; leaving it alone."
      return
    fi
    cp "$conf" "${conf}.da-assistant.bak"
    printf '\n%s\n' "$snippet" >> "$conf"
    ok "  appended to $conf (backup at ${conf}.da-assistant.bak)"
    [ "$kind" = hyprland ] && command -v hyprctl >/dev/null && hyprctl reload >/dev/null 2>&1
    [ "$kind" = sway ] && command -v swaymsg >/dev/null && swaymsg reload >/dev/null 2>&1
  else
    info "  --apply would append the above to $conf"
  fi
}

remove_wm() {
  local kind="$1" conf
  if [ "$kind" = hyprland ]; then
    conf="${HOME}/.config/hypr/hyprland.conf"
  else
    conf="${HOME}/.config/sway/config"
  fi
  if [ "$MODE" = "print" ]; then
    printf '  strip the generated block from %s\n' "$conf"
    return
  fi
  [ -f "$conf" ] || return
  cp "$conf" "${conf}.da-assistant.bak"
  sed -i '/--- DA Assistant hotkeys/,/--- end DA Assistant hotkeys ---/d' "$conf"
  ok "  removed the block from $conf"
}

# ------------------------------------------------------------------------- main

DESKTOP="$(detect_desktop)"

echo
info "DA Assistant hotkey setup"
echo "  desktop  : ${DESKTOP}"
echo "  session  : ${XDG_SESSION_TYPE:-unknown}"
echo "  launcher : ${LAUNCHER}"
echo "  mode     : ${MODE}"
echo

if [ ! -x "$LAUNCHER" ]; then
  warn "Launcher not found or not executable: $LAUNCHER"
  if [ -z "${APPIMAGE:-}" ]; then
    warn "Run this script from inside the project's tools/ directory."
  fi
  exit 1
fi

if [ "$MODE" = "print" ]; then
  warn "Dry run - nothing will be changed. Re-run with --apply to install."
  echo
fi

case "$DESKTOP" in
  cinnamon)
    if [ "$MODE" = "remove" ]; then remove_cinnamon; else install_cinnamon; fi ;;
  gnome)
    if [ "$MODE" = "remove" ]; then remove_gnome; else install_gnome; fi ;;
  kde)
    if [ "$MODE" = "remove" ]; then remove_kde; else install_kde; fi ;;
  hyprland)
    if [ "$MODE" = "remove" ]; then remove_wm hyprland; else install_wm hyprland; fi ;;
  sway)
    if [ "$MODE" = "remove" ]; then remove_wm sway; else install_wm sway; fi ;;
  *)
    # MATE and XFCE are recognised and land here all the same: MATE used to be sent to
    # the Cinnamon installer, whose schemas it does not have.
    warn "No automatic setup for this desktop: ${DESKTOP}"
    echo
    echo "Bind these commands to keys using your desktop's keyboard settings:"
    for entry in "${INSTALL[@]}"; do
      IFS='|' read -r action label key <<< "$entry"
      printf '  Ctrl+Shift+%s   %s --action=%s   (%s)\n' \
             "$key" "$LAUNCHER" "$action" "$label"
    done
    echo
    echo "Or force a desktop:  $0 --desktop=kde --apply"
    exit 0 ;;
esac

echo
if [ "$MODE" = "apply" ]; then
  ok "Done. Set hotkey_mode to 'static' so the app does not also grab these keys:"
  echo "    tray menu > Settings > Hotkey mode > static"
  echo
  echo "Make sure the assistant is running, then try Ctrl+Shift+5."
elif [ "$MODE" = "remove" ]; then
  ok "Removed."
else
  ok "Dry run complete. Re-run with --apply to make these changes."
fi
