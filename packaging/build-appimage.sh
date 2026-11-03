#!/bin/bash
# Build DA Assistant's AppImage reproducibly with rootless Podman (see packaging/README.md).
#   packaging/build-appimage.sh build   AppImage of HEAD -> dist/ (+ .sha256, .zsync)
#   packaging/build-appimage.sh check   build HEAD twice from scratch and compare the hashes
#   packaging/build-appimage.sh lock    lock what the *.in files add (UPGRADE=1: re-resolve
#                                       everything); review, then commit
# DIST=dir puts the results somewhere other than dist/.
# Only committed files are built (HEAD's, as committed); SOURCE_DATE_EPOCH is HEAD's commit time.
# lock reads the *.in files as they are in the working tree.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
. packaging/appimage/pins.sh
mode="${1:-build}"

command -v podman >/dev/null || { echo "Podman is needed: sudo apt install podman (or dnf/pacman)" >&2; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Note: uncommitted changes are not built; this builds HEAD ($(git rev-parse --short HEAD))." >&2
fi
for ca in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
    [ -f "$ca" ] && break
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

TAG="da-assistant-appimage-builder:$(cat packaging/appimage/Containerfile packaging/appimage/pins.sh | sha256sum | cut -c1-12)"
if ! podman image exists "$TAG"; then
    # The host's CA bundle, for apt to reach the snapshot over https, goes in as a copy in
    # the build context rather than a mount: on an SELinux host (Fedora, RHEL) a mount needs
    # :Z, and rootless Podman may not relabel a file root owns - nor should anything
    # relabel a file the host's own programs read. Readable by all, whatever the umask:
    # COPY keeps its mode, and apt fetches as its own user, _apt.
    mkdir "$WORK/context"
    install -m 0644 "$ca" "$WORK/context/hostca.crt"
    podman build -t "$TAG" \
        --build-arg BASE_IMAGE="$BASE_IMAGE" --build-arg APT_SNAPSHOT="$APT_SNAPSHOT" \
        -f packaging/appimage/Containerfile "$WORK/context"
fi

EPOCH="$(git log -1 --format=%ct HEAD)"
# HEAD's files exactly as committed, from git's object store. git archive would pass each
# through this machine's git settings first - core.autocrlf, core.eol, the filters and
# ident its attributes name, tar.umask - and a verifier's own settings would change the
# AppImage. Only plain files are taken; the tree has nothing else.
mkdir "$WORK/tree"
git ls-tree -r -z HEAD | while IFS= read -r -d '' entry; do
    read -r fmode ftype oid <<< "${entry%%$'\t'*}"
    path="${entry#*$'\t'}"
    case "$fmode" in
        100644|100755) ;;
        *) echo "$path is a $ftype of mode $fmode, which the build does not take" >&2; exit 1 ;;
    esac
    mkdir -p "$WORK/tree/$(dirname "$path")"
    git cat-file blob "$oid" > "$WORK/tree/$path"
    chmod "${fmode#100}" "$WORK/tree/$path"
done
(cd "$WORK/tree" && shopt -s dotglob && tar -cf "$WORK/src.tar" --sort=name --owner=0 \
    --group=0 --numeric-owner --mode=u+rwX,go+rX,go-w --mtime="@$EPOCH" -- *)
VERSION="$(git show HEAD:assistant/__init__.py | sed -n 's/^__version__ = "\(.*\)"/\1/p')"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/da-assistant-appimage"
DIST="$(mkdir -p "${DIST:-dist}" && cd "${DIST:-dist}" && pwd)"
mkdir -p "$CACHE"

run() {  # out-dir mode
    mkdir -p "$1"
    podman run --rm -v "$WORK/src.tar:/src.tar:ro,Z" -v "$CACHE:/cache:Z" -v "$1:/out:Z" \
        -e SOURCE_DATE_EPOCH="$EPOCH" -e VERSION="$VERSION" -e UPGRADE="${UPGRADE:-0}" "$TAG" \
        bash -c 'tar -xOf /src.tar packaging/appimage/build.sh > /tmp/build.sh && bash /tmp/build.sh "$0"' "$2"
}

case "$mode" in
    build)
        run "$DIST" build ;;
    check)
        run "$WORK/a" build
        rm -rf "$CACHE/pip"          # the second build downloads its wheels again
        run "$WORK/b" build
        a=$(cut -d' ' -f1 "$WORK"/a/*.sha256); b=$(cut -d' ' -f1 "$WORK"/b/*.sha256)
        if [ "$a" = "$b" ] && cmp -s "$WORK"/a/*.zsync "$WORK"/b/*.zsync; then
            cp "$WORK"/a/* "$DIST"/
            echo "Reproducible: both builds are $a"
        else
            echo "NOT reproducible: $a vs $b (compare with diffoscope; copies in $DIST/check-a, $DIST/check-b)" >&2
            rm -rf "$DIST/check-a" "$DIST/check-b" && cp -r "$WORK/a" "$DIST/check-a" && cp -r "$WORK/b" "$DIST/check-b"
            exit 1
        fi ;;
    lock)
        # from the working tree, so that a requirement is locked before it is committed
        tar -cf "$WORK/src.tar" packaging/appimage
        run "$WORK/lock" lock
        cp "$WORK"/lock/*.lock packaging/appimage/
        git --no-pager diff --stat -- packaging/appimage/*.lock ;;
    *)
        echo "usage: $0 build|check|lock" >&2; exit 2 ;;
esac
