#!/usr/bin/env bash
# Stage a native distribution directory.
#
# Usage:
#   make dist DIST=<new-directory> ZELLIJ_SRC=<pinned-zellij-checkout>
#
# Environment:
#   TDNEXT_PREBUILT_CORE=<existing-tdnext-core-binary>: when set, stage that
#   binary as tdnext-core instead of rebuilding it. The check entry point
#   sets this to its already-built, already-probed, already-tested fresh core
#   (same pinned source and script), so `make check` builds the core once.
#   Standalone `make dist` leaves it unset and builds the core from scratch.
#   The staged binary must report native chrome version 1 either way.
#
# There is no default destination: DIST must always be given explicitly, and
# it must not exist yet (mkdir is the atomic no-overwrite claim; only the
# directory this script created is ever removed). All paths are quoted and
# every mutating command takes `--` so leading-dash paths cannot smuggle
# options.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
dist="${1:-}"
zellij="${2:-${TDNEXT_ZELLIJ_SRC:-}}"
unset CARGO_TARGET_DIR
if [[ -z "$dist" || -z "$zellij" ]]; then
  echo 'usage: make dist DIST=<new-directory> ZELLIJ_SRC=<pinned-zellij-checkout>' >&2
  exit 2
fi
if [[ -e "$dist" || -L "$dist" ]]; then
  echo "build-dist: destination already exists: $dist" >&2
  exit 1
fi

for crate in native-manager native-panes/settings native-desktop native-panes/files; do
  test -f "$root/$crate/Cargo.toml" && test -f "$root/$crate/Cargo.lock" || {
    echo "build-dist: missing manifest or lockfile: $crate" >&2
    exit 1
  }
done
for crate in native-manager native-panes/settings native-desktop native-panes/files; do
  cargo build --locked --release --manifest-path "$root/$crate/Cargo.toml"
done

# mkdir is the atomic no-overwrite claim; only remove the directory we created.
mkdir -- "$dist"
complete=0
cleanup() {
  if [[ "$complete" = 0 ]]; then rm -rf -- "$dist"; fi
}
trap cleanup EXIT
install -m 0755 -- "$root/native-manager/target/release/tdnext" "$dist/tdnext"
install -m 0755 -- "$root/native-panes/settings/target/release/tdnext-settings" "$dist/tdnext-settings"
install -m 0755 -- "$root/native-desktop/target/release/tdnext-desktop" "$dist/tdnext-desktop"
install -m 0755 -- "$root/native-panes/files/target/release/tdnext-files" "$dist/tdnext-files"
if [[ -n "${TDNEXT_PREBUILT_CORE:-}" ]]; then
  test -x "${TDNEXT_PREBUILT_CORE}" \
    || { echo "build-dist: TDNEXT_PREBUILT_CORE is not executable: ${TDNEXT_PREBUILT_CORE}" >&2; exit 1; }
  test "$("${TDNEXT_PREBUILT_CORE}" --tdnext-chrome-version)" = 1 \
    || { echo "build-dist: TDNEXT_PREBUILT_CORE is not native chrome version 1: ${TDNEXT_PREBUILT_CORE}" >&2; exit 1; }
  echo "build-dist: staging prebuilt core ${TDNEXT_PREBUILT_CORE} (no rebuild)."
  install -m 0755 -- "${TDNEXT_PREBUILT_CORE}" "$dist/tdnext-core"
else
  bash "$root/scripts/build-zellij-core-navigation.sh" "$zellij" "$dist/tdnext-core"
fi
install -m 0644 -- "$root/patches/LICENSE.zellij-0.45.1" "$dist/LICENSE.zellij-0.45.1"
complete=1
echo "build-dist: staged native distribution at $dist"
