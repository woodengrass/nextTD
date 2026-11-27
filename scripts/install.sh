#!/usr/bin/env bash
# Install or update Terminal Desktop Next for the current user.
#
#   curl -fsSL https://github.com/woodengrass/nextTD/releases/latest/download/install.sh | bash
#   bash install.sh [--version vX.Y.Z] [--dest DIR] [--bin-dir DIR] [--base-url URL]
#
# Downloads one release (the archive, its .sha256 and install-verified-dist.sh
# from the same release), checks GitHub's build attestation for both when the
# GitHub CLI is logged in, then activates the archive with
# `install-verified-dist.sh --digest-file`. Without a logged-in `gh` the
# published checksum only guards against a corrupted download, not against a
# tampered release; the script says so.
#
# Writes only under DEST (default ${XDG_DATA_HOME:-~/.local/share}/tdnext;
# earlier releases are kept) and one `tdnext` symlink in BIN_DIR (default
# ~/.local/bin). An existing BIN_DIR/tdnext that is not this installation's
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
  --dest DIR       installation root (default: ${XDG_DATA_HOME:-~/.local/share}/tdnext)
  --bin-dir DIR    where the `tdnext` command link goes (default: ~/.local/bin)
  --base-url URL   download the release assets from URL instead of GitHub
                   (for mirrors and offline copies; file:// works)
  -h, --help       print this help and exit
EOF
}

# The whole script is one function so `curl | bash` parses all of it before
# running anything.
main() {
  VERSION="latest"
  DEST="${XDG_DATA_HOME:-$HOME/.local/share}/tdnext"
  BIN_DIR="$HOME/.local/bin"
  BASE_URL=""

  while (($# > 0)); do
    case "$1" in
      --version) VERSION="${2:-}"; shift 2 ;;
      --dest) DEST="${2:-}"; shift 2 ;;
      --bin-dir) BIN_DIR="${2:-}"; shift 2 ;;
      --base-url) BASE_URL="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown argument: $1 (see --help)" ;;
    esac
  done

  test -n "$VERSION" || fail "--version needs a release tag"
  test -n "$DEST" || fail "--dest needs a directory"
  test -n "$BIN_DIR" || fail "--bin-dir needs a directory"
  [[ "$(uname -s)" = Linux && "$(uname -m)" = x86_64 ]] \
    || fail "only Linux x86-64 is supported (this is $(uname -s) $(uname -m))"
  for tool in curl tar gzip sha256sum find cmp awk; do
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

  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    for name in "$ASSET" "$ACTIVATOR"; do
      gh attestation verify "$WORK/$name" --repo "$REPO" --signer-workflow "$SIGNER_WORKFLOW" >/dev/null \
        || fail "$name has no valid build attestation from $SIGNER_WORKFLOW; nothing installed"
    done
    echo "$PROG: build attestation verified: built and published by $SIGNER_WORKFLOW."
  else
    echo "$PROG: note: GitHub CLI not logged in; skipping the build attestation check." >&2
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
EOF
}

main "$@"
