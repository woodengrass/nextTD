#!/usr/bin/env bash
# Install or update Terminal Desktop Next for the current user.
#
#   curl -fsSLO https://github.com/woodengrass/nextTD/releases/latest/download/install.sh && bash install.sh
#   bash install.sh [--version vX.Y.Z] [--dest DIR] [--bin-dir DIR] [--base-url URL]
#
# Downloads one release (the archive, its .sha256 and install-verified-dist.sh
# from the same release), checks GitHub's build attestation for both when the
# GitHub CLI is logged in, then activates the archive with
# `install-verified-dist.sh --digest-file`. Without a logged-in `gh` the
# published checksum only guards against a corrupted download, not against a
# tampered release; the script says so.
#
# Writes only under DEST (default ~/.local/opt/tdnext; earlier releases are
# kept) and one `tdnext` symlink in BIN_DIR (default ~/.local/bin). DEST may
# not overlap the settings directory (TDNEXT_STATE_DIR, else
# ${XDG_DATA_HOME:-~/.local/share}/tdnext), so removing DEST never removes
# settings. An existing BIN_DIR/tdnext that is not this installation's
# symlink is never replaced. No root, no Python, no settings or sessions are
# touched. Needs bash, curl, tar, gzip, coreutils, findutils, diffutils, awk.
set -euo pipefail

PROG="tdnext-install"
REPO="woodengrass/nextTD"
ASSET="tdnext-linux-x86_64.tar.gz"
ACTIVATOR="install-verified-dist.sh"
SIGNER_WORKFLOW="$REPO/.github/workflows/release-smoke.yml"

fail() {
  echo "$PROG: error: $1" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage: install.sh [--version vX.Y.Z] [--dest DIR] [--bin-dir DIR] [--base-url URL]

  --version TAG    release tag to install (default: the latest release)
  --dest DIR       installation root (default: ~/.local/opt/tdnext)
  --bin-dir DIR    where the `tdnext` command link goes (default: ~/.local/bin)
  --base-url URL   download the release assets from URL instead of GitHub
                   (for mirrors and offline copies; file:// works)
  -h, --help       print this help and exit
EOF
}

need_arg() {
  (($# >= 2)) && [[ -n "$2" ]] || fail "$1 needs a value (see --help)"
}

# Settings directory as native-manager/src/state_dir.rs resolves it for an
# installed binary.
state_dir() {
  local dir
  if [[ -n "${TDNEXT_STATE_DIR:-}" ]]; then
    dir="$TDNEXT_STATE_DIR"
    # shellcheck disable=SC2088 # matching a literal leading ~ on purpose
    [[ "$dir" = "~" || "$dir" = "~/"* ]] && dir="$HOME${dir:1}"
  elif [[ "${XDG_DATA_HOME:-}" = /* ]]; then
    dir="$XDG_DATA_HOME/tdnext"
  else
    dir="$HOME/.local/share/tdnext"
  fi
  realpath -m -- "$dir"
}

# The whole script is one function so a piped or partly downloaded script
# never runs a prefix of itself.
main() {
  VERSION="latest"
  DEST=""
  BIN_DIR=""
  BASE_URL=""

  while (($# > 0)); do
    case "$1" in
      --version) need_arg "$@"; VERSION="$2"; shift 2 ;;
      --dest) need_arg "$@"; DEST="$2"; shift 2 ;;
      --bin-dir) need_arg "$@"; BIN_DIR="$2"; shift 2 ;;
      --base-url) need_arg "$@"; BASE_URL="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown argument: $1 (see --help)" ;;
    esac
  done

  [[ -n "${HOME:-}" ]] || fail "HOME is not set"
  DEST="${DEST:-$HOME/.local/opt/tdnext}"
  BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
  STATE="$(state_dir)"
  DEST_ABS="$(realpath -m -- "$DEST")"
  if [[ "$DEST_ABS" = "$STATE" || "$DEST_ABS/" = "$STATE/"* || "$STATE/" = "$DEST_ABS/"* ]]; then
    fail "--dest $DEST_ABS overlaps the settings directory $STATE; choose another --dest"
  fi
  [[ "$(uname -s)" = Linux && "$(uname -m)" = x86_64 ]] \
    || fail "only Linux x86-64 is supported (this is $(uname -s) $(uname -m))"
  for tool in curl tar gzip sha256sum find cmp awk realpath; do
    command -v "$tool" >/dev/null 2>&1 || fail "required tool '$tool' not found on PATH"
  done

  if [[ -z "$BASE_URL" ]]; then
    if [[ "$VERSION" = latest ]]; then
      BASE_URL="https://github.com/$REPO/releases/latest/download"
    else
      BASE_URL="https://github.com/$REPO/releases/download/$VERSION"
    fi
  fi
  BASE_URL="${BASE_URL%/}"

  WORK="$(mktemp -d -t tdnext-install-XXXXXX)"
  trap 'rm -rf -- "$WORK"' EXIT

  for name in "$ASSET" "$ASSET.sha256" "$ACTIVATOR"; do
    echo "$PROG: downloading $BASE_URL/$name"
    curl -fsSL --proto '=https,file' --retry 3 -o "$WORK/$name" "$BASE_URL/$name" \
      || fail "download failed: $BASE_URL/$name"
  done

  if command -v gh >/dev/null 2>&1 && gh attestation verify --help >/dev/null 2>&1 \
    && gh auth status >/dev/null 2>&1; then
    for name in "$ASSET" "$ACTIVATOR"; do
      gh attestation verify "$WORK/$name" --repo "$REPO" --signer-workflow "$SIGNER_WORKFLOW" >/dev/null \
        || fail "$name has no valid build attestation from $SIGNER_WORKFLOW; nothing installed"
    done
    echo "$PROG: build attestation verified: built and published by $SIGNER_WORKFLOW."
  else
    echo "$PROG: note: no logged-in GitHub CLI with 'gh attestation'; skipping the build attestation check." >&2
    echo "$PROG: note: the checksum below only detects a corrupted download. To check the" >&2
    echo "$PROG: note: release's origin, run: gh attestation verify $ASSET --repo $REPO" >&2
  fi

  bash "$WORK/$ACTIVATOR" --archive "$WORK/$ASSET" --dest "$DEST" --digest-file "$WORK/$ASSET.sha256"

  DEST="$(cd -- "$DEST" && pwd -P)"
  TARGET="$DEST/current/tdnext"
  LINK="$BIN_DIR/tdnext"
  mkdir -p -- "$BIN_DIR"
  if [[ -L "$LINK" ]]; then
    [[ "$(readlink -- "$LINK")" = "$TARGET" ]] \
      || fail "$LINK already points to $(readlink -- "$LINK"); leaving it alone. Run $TARGET directly or replace the link yourself"
  elif [[ -e "$LINK" ]]; then
    fail "$LINK already exists and is not a link to this installation; leaving it alone. Run $TARGET directly"
  else
    ln -s -- "$TARGET" "$LINK"
  fi
  echo "$PROG: installed: $LINK -> $TARGET"

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "$PROG: note: $BIN_DIR is not on PATH; add it, or run $LINK directly." ;;
  esac
  cat <<EOF
Next steps:
  tdnext list
  tdnext enable files
  tdnext desktop .
The Monitor view needs btop, installed separately.
To uninstall: rm -- "$LINK" && rm -rf -- "$DEST"
(settings stay in the state directory, ~/.local/share/tdnext by default)
EOF
}

main "$@"
