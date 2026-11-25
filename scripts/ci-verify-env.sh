#!/usr/bin/env bash
# Fail fast when this machine cannot run the isolated native test suite.
#
# Usage:
#   scripts/ci-verify-env.sh [repo-root]
#
# Checks (non-zero exit with a clear message on the first failure):
#   1. Linux on x86_64.
#   2. Rust 1.95.x active (rustc --version).
#   3. tmux, btop, nvim, vi present and executable; btop answers --version.
#   4. /dev/shm exists, is writable, and lives on a different device than the
#      actual temp dir (${TMPDIR:-/tmp}), so the Files cross-device move
#      tests (default tempdir vs /dev/shm) exercise a real second device.
#   5. The repo filesystem and the actual temp dir filesystem hold user.*
#      xattrs (round-trip probes), so the Files xattr-preservation tests
#      exercise preservation, not the best-effort fallback.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

fail() {
  echo "ci-verify-env: error: $1" >&2
  exit 1
}

ARCH="$(uname -m)"
test "${ARCH}" = "x86_64" || fail "requires x86_64 (uname -m reports ${ARCH})."
test "$(uname -s)" = "Linux" || fail "requires Linux (uname -s reports $(uname -s))."

command -v cargo >/dev/null 2>&1 || fail "cargo not found on PATH (install Rust 1.95)."
command -v rustc >/dev/null 2>&1 || fail "rustc not found on PATH (install Rust 1.95)."
RUSTC_VERSION="$(rustc --version | awk '{print $2}')"
case "${RUSTC_VERSION}" in
  1.95.*) ;;
  *) fail "requires Rust 1.95.x (rustc reports ${RUSTC_VERSION})." ;;
esac

for tool in tmux btop nvim vi; do
  command -v "${tool}" >/dev/null 2>&1 || fail "native test requires ${tool} on PATH."
  test -x "$(command -v "${tool}")" || fail "${tool} is not executable: $(command -v "${tool}")."
done
BTOP_VERSION="$(btop --version 2>&1 | head -1)"
case "${BTOP_VERSION}" in
  *[Bb][Tt][Oo][Pp]*) ;;
  *) fail "btop --version did not report btop (got: ${BTOP_VERSION})." ;;
esac

test -d /dev/shm || fail "/dev/shm is missing (Files cross-device tests need it)."
SHM_PROBE="$(mktemp -p /dev/shm tdnext-ci-probe.XXXXXX)" || fail "/dev/shm is not writable."
rm -f "${SHM_PROBE}"
# The cross-device tests compare the default tempdir against /dev/shm, so
# compare those two actual devices (not /).
TMP_BASE="${TMPDIR:-/tmp}"
test -d "${TMP_BASE}" || fail "temp dir is missing: ${TMP_BASE} (TMPDIR points nowhere)."
TMP_DEV="$(stat -c %d "${TMP_BASE}")"
SHM_DEV="$(stat -c %d /dev/shm)"
test "${TMP_DEV}" != "${SHM_DEV}" \
  || fail "/dev/shm is on the same device as the temp dir ${TMP_BASE} (cross-device move tests would not cross devices)."

XATTR_PROBE_FILE="$(mktemp "${ROOT}/.ci-xattr-probe.XXXXXX")"
XATTR_PROBE_TMP="$(mktemp "${TMP_BASE}/tdnext-ci-xattr-probe.XXXXXX")"
trap 'rm -f "${XATTR_PROBE_FILE}" "${XATTR_PROBE_TMP}"' EXIT INT TERM
python3 - "${XATTR_PROBE_FILE}" "${XATTR_PROBE_TMP}" <<'EOF'
import os
import sys

for path in sys.argv[1:]:
    name = "user.tdnext-ci-probe"
    try:
        os.setxattr(path, name, b"1")
        assert os.getxattr(path, name) == b"1"
        os.removexattr(path, name)
    except OSError as exc:
        print(f"ci-verify-env: error: filesystem for {path} must hold user.* xattrs: {exc}", file=sys.stderr)
        raise SystemExit(1)
EOF

echo "ci-verify-env: ok (x86_64 Linux, rustc ${RUSTC_VERSION}, ${BTOP_VERSION}, /dev/shm vs ${TMP_BASE} cross-device, xattrs held)."
