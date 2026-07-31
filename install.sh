#!/bin/sh
set -eu

RELEASE_BASE_URL=${RALPH_RELEASE_BASE_URL:-https://github.com/dameg/ralph/releases}
RELEASE_BASE_URL=${RELEASE_BASE_URL%/}
BIN_DIR=${RALPH_BIN_DIR:-}
VERSION=
FORCE=0
UNINSTALL=0

usage() {
    cat <<'EOF'
Install Ralph as a user-wide command.

Usage:
  install.sh [--version X.Y.Z] [--bin-dir PATH] [--force] [--uninstall]

Options:
  --version X.Y.Z  Install a specific stable release (a leading v is accepted).
  --bin-dir PATH   Install into PATH instead of ~/.local/bin.
  --force          Replace an unrecognized target file.
  --uninstall      Remove the installed executable only.
  -h, --help       Show this help.
EOF
}

fail() {
    printf 'ralph installer: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            [ "$#" -ge 2 ] || fail "--version requires a value"
            [ -n "$2" ] || fail "--version requires a value"
            VERSION=$2
            shift 2
            ;;
        --bin-dir)
            [ "$#" -ge 2 ] || fail "--bin-dir requires a value"
            [ -n "$2" ] || fail "--bin-dir requires a non-empty path"
            BIN_DIR=$2
            shift 2
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

if [ -z "$BIN_DIR" ]; then
    [ -n "${HOME:-}" ] || fail "HOME is not set; pass --bin-dir PATH"
    BIN_DIR=$HOME/.local/bin
fi

TARGET=$BIN_DIR/ralph

if [ -d "$TARGET" ] && [ ! -L "$TARGET" ]; then
    fail "$TARGET is a directory and cannot be managed by the installer"
fi

is_ralph() {
    [ -f "$1" ] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -c 'import sys, zipfile
try:
    with zipfile.ZipFile(sys.argv[1]) as archive:
        valid = archive.read("ralph_loop/zipapp-marker.txt") == b"ralph-zipapp-v1\n"
except (OSError, KeyError, zipfile.BadZipFile):
    valid = False
raise SystemExit(0 if valid else 1)' "$1"
}

if [ "$UNINSTALL" -eq 1 ]; then
    if [ ! -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
        printf 'Ralph is not installed at %s\n' "$TARGET"
        exit 0
    fi
    if { [ -L "$TARGET" ] || ! is_ralph "$TARGET"; } && [ "$FORCE" -ne 1 ]; then
        fail "$TARGET is not a recognized Ralph installation; use --force to remove it"
    fi
    rm -f -- "$TARGET"
    printf 'Removed Ralph executable: %s\n' "$TARGET"
    printf 'Project .ralph directories were not changed.\n'
    exit 0
fi

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "Python 3.9 or newer is required"
case $(uname -s 2>/dev/null) in
    Darwin|Linux) ;;
    *) fail "only macOS and Linux are supported" ;;
esac
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "Python 3.9 or newer is required"

if [ -n "$VERSION" ]; then
    VERSION=$(printf '%s' "$VERSION" | sed 's/^v//')
    printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
        || fail "version must use stable MAJOR.MINOR.PATCH format"
    ASSET_URL=$RELEASE_BASE_URL/download/v$VERSION/ralph
else
    ASSET_URL=$RELEASE_BASE_URL/latest/download/ralph
fi
CHECKSUM_URL=$ASSET_URL.sha256

mkdir -p "$BIN_DIR"
if { [ -L "$TARGET" ] || { [ -e "$TARGET" ] && ! is_ralph "$TARGET"; }; } \
    && [ "$FORCE" -ne 1 ]; then
    fail "$TARGET already exists and is not a recognized Ralph installation; use --force"
fi

TEMP_DIR=$(mktemp -d "$BIN_DIR/.ralph-install.XXXXXX") \
    || fail "cannot create a temporary directory in $BIN_DIR"
cleanup() {
    rm -rf -- "$TEMP_DIR"
}
trap cleanup 0 1 2 15

ARTIFACT=$TEMP_DIR/ralph
CHECKSUM=$TEMP_DIR/ralph.sha256
curl -fsSL --retry 3 "$ASSET_URL" -o "$ARTIFACT" \
    || fail "cannot download $ASSET_URL"
curl -fsSL --retry 3 "$CHECKSUM_URL" -o "$CHECKSUM" \
    || fail "cannot download $CHECKSUM_URL"

EXPECTED=$(awk 'NR == 1 { print $1 }' "$CHECKSUM" | tr 'A-F' 'a-f')
printf '%s\n' "$EXPECTED" | grep -Eq '^[0-9a-fA-F]{64}$' \
    || fail "release checksum is invalid"
if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL=$(sha256sum "$ARTIFACT" | awk '{ print $1 }')
elif command -v shasum >/dev/null 2>&1; then
    ACTUAL=$(shasum -a 256 "$ARTIFACT" | awk '{ print $1 }')
else
    fail "sha256sum or shasum is required"
fi
[ "$ACTUAL" = "$EXPECTED" ] \
    || fail "checksum mismatch (expected $EXPECTED, received $ACTUAL)"

chmod 755 "$ARTIFACT"
is_ralph "$ARTIFACT" || fail "downloaded Ralph artifact is not a valid zipapp"
DETECTED=$("$ARTIFACT" --version 2>/dev/null) \
    || fail "downloaded Ralph executable failed validation"
printf '%s\n' "$DETECTED" | grep -Eq '^ralph [0-9]+\.[0-9]+\.[0-9]+$' \
    || fail "downloaded Ralph executable returned an invalid version"
if [ -n "$VERSION" ] && [ "$DETECTED" != "ralph $VERSION" ]; then
    fail "downloaded Ralph version does not match requested version $VERSION"
fi

mv -f -- "$ARTIFACT" "$TARGET"
trap - 0 1 2 15
cleanup

printf 'Installed %s at %s\n' "$DETECTED" "$TARGET"
case :${PATH:-}: in
    *:"$BIN_DIR":*) ;;
    *)
        printf 'Add %s to PATH, for example:\n' "$BIN_DIR"
        printf '  export PATH="%s:$PATH"\n' "$BIN_DIR"
        ;;
esac
