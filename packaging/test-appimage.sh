#!/bin/bash
# Smoke-test the AppImage in clean containers of the distributions it targets, under a virtual X
# display. Each starts with GTK's library alone - no typelibs, tray indicator or libnotify of its
# own, so the AppImage's fallbacks are what it runs on - and --probe must find GTK, the indicator
# and the plain-text chat view, and say what to install for the rendered one. Then WebKitGTK is
# installed, and --probe must find the rendered view.
#   packaging/test-appimage.sh [ubuntu debian fedora arch cachyos]   (default: all)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
APPIMAGE="$(realpath "${APPIMAGE:-$(ls -t dist/DA_Assistant-*-x86_64.AppImage | head -1)}")"   # or APPIMAGE=path

declare -A IMAGE BASE WEBKIT
IMAGE[ubuntu]=docker.io/library/ubuntu:24.04;     BASE[ubuntu]="apt-get update -qq && apt-get install -y -qq --no-install-recommends xvfb xauth libgtk-3-0t64 >/dev/null";  WEBKIT[ubuntu]="apt-get install -y -qq gir1.2-webkit2-4.1 >/dev/null"
IMAGE[debian]=docker.io/library/debian:trixie;    BASE[debian]="apt-get update -qq && apt-get install -y -qq --no-install-recommends xvfb xauth libgtk-3-0t64 >/dev/null";  WEBKIT[debian]="${WEBKIT[ubuntu]}"
IMAGE[fedora]=registry.fedoraproject.org/fedora:latest; BASE[fedora]="dnf install -y -q xorg-x11-server-Xvfb xorg-x11-xauth gtk3 >/dev/null"; WEBKIT[fedora]="dnf install -y -q webkit2gtk4.1 >/dev/null"
IMAGE[arch]=docker.io/library/archlinux:latest;   BASE[arch]="pacman -Syu --noconfirm -q xorg-server-xvfb xorg-xauth gtk3 >/dev/null"; WEBKIT[arch]="pacman -S --noconfirm -q webkit2gtk-4.1 >/dev/null"
IMAGE[cachyos]=docker.io/cachyos/cachyos:latest;  BASE[cachyos]="${BASE[arch]}"
# CachyOS's pacman won't run its install hooks in a container (sandbox vs network access): do what a
# real install's hooks do for GTK (image loaders, schemas, MIME database)
WEBKIT[cachyos]="${WEBKIT[arch]} 2>/dev/null; gdk-pixbuf-query-loaders --update-cache; glib-compile-schemas /usr/share/glib-2.0/schemas; update-mime-database /usr/share/mime"

status=0
for d in "${@:-ubuntu debian fedora arch cachyos}"; do
  for name in $d; do
    echo "== $name (${IMAGE[$name]})"
    podman run --rm -v "$APPIMAGE:/app/DA_Assistant.AppImage:ro,Z" "${IMAGE[$name]}" bash -c "
      set -e
      ${BASE[$name]}
      cd /tmp && /app/DA_Assistant.AppImage --appimage-extract >/dev/null
      export APPIMAGE=/app/DA_Assistant.AppImage HOME=/tmp/home
      check() { if grep -Eq \"\$2\" /tmp/probe; then echo \"PASS \$1\"; else echo \"FAIL \$1\"; cat /tmp/probe; exit 1; fi; }
      xvfb-run -a squashfs-root/AppRun --probe > /tmp/probe 2>&1 || true
      check 'GTK loads (from the fallback typelibs where missing)' '^Tray backend +: '
      check 'the tray indicator is found' '^Tray backend +: appindicator'
      check 'without WebKitGTK: plain text' '^WebKit view +: no'
      check 'and it says what to install' 'install WebKitGTK'
      grep -E '^(Fallbacks|Notifications)' /tmp/probe | sed 's/^/  | /' || true
      ${WEBKIT[$name]}
      xvfb-run -a squashfs-root/AppRun --probe > /tmp/probe 2>&1 || true
      check 'with WebKitGTK: the rendered view' '^WebKit view +: yes'
    " 2>&1 | grep -E '^(PASS|FAIL)|^  \| |Traceback|Error' | grep -vE 'libEGL|MESA' || status=1
  done
done
exit $status
