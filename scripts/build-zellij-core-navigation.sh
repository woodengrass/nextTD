#!/usr/bin/env bash
# Rebuild the pinned Zellij 0.45.1 core-navigation patch into a
# project-local executable without touching live sessions.
#
# Usage:
#   scripts/build-zellij-core-navigation.sh <path-to-zellij-git-checkout> [destination]
#
# Inputs:
#   $1  Existing local Zellij git checkout (pristine or dirty working tree
#       is accepted; only its committed HEAD is used).
#   $2  Optional destination; defaults to state/bin/tdnext.
#
# What it does:
#   1. Verifies the checkout HEAD is exactly
#      efd8fd5a89a20c07a111d248ad7fce53848d2c18.
#   2. Exports that committed HEAD with `git archive` to a private mktemp
#      directory (working-tree changes are never copied; the checkout is
#      never modified).
#   3. Applies the navigation patch followed by the native chrome patch.
#   4. Builds `cargo build --locked --release --no-default-features
#      --bin zellij` inside the exported source.
#   5. When TDNEXT_TEST_PATCHED_CORE=1, runs the navigation test suites
#      inside the SAME exported patched source (zellij-server navigation +
#      fixed-width tests, then the full zellij-utils lib suite) and refuses
#      to treat a filter that matched no tests as a pass.
#   6. Checks navigation commands and the native chrome version probe.
#   7. Atomically installs the requested destination ONLY if absent (never
#      overwrites).
#
# Environment:
#   TDNEXT_TEST_PATCHED_CORE=1 runs the patched-core test suites inside the
#     same exported patched source before the probes/install step (default 0,
#     production build path unchanged): the zellij-server navigation and
#     fixed-width tests plus the full zellij-utils lib suite, per
#     patches/zellij-0.45.1-core-navigation.md. Test failures, or a server
#     filter that matched no passing tests, abort before install.
#   PATH and CARGO_HOME are used as inherited from the caller's
#   environment and are never overridden here. Install the Rust 1.95
#   toolchain (e.g. `rustup toolchain install 1.95`) plus the upstream
#   native build prerequisites (a C compiler, pkg-config, and OpenSSL
#   development headers; see the Zellij repository README) before running.
#   Cargo may download locked dependencies unless its offline mode is enabled.
#   The script does not use sudo, change Git configuration, commit, or operate
#   on any session.
#
# Release wheel builds invoke this script with a private destination.
set -euo pipefail

PINNED_HEAD="efd8fd5a89a20c07a111d248ad7fce53848d2c18"
PATCH_NAME="zellij-0.45.1-core-navigation.patch"
CHROME_PATCH_NAME="zellij-0.45.1-native-chrome.patch"
LICENSE_NAME="LICENSE.zellij-0.45.1"
DEST_NAME="tdnext"

usage() {
  echo "usage: $(basename "$0") <path-to-zellij-git-checkout> [destination]" >&2
  echo "Rebuilds the pinned Zellij core patch to state/bin/${DEST_NAME} (no overwrite)." >&2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PATCH_FILE="${PROJECT_ROOT}/patches/${PATCH_NAME}"
CHROME_PATCH_FILE="${PROJECT_ROOT}/patches/${CHROME_PATCH_NAME}"
LICENSE_FILE="${PROJECT_ROOT}/patches/${LICENSE_NAME}"
DEST_FILE="${2:-${PROJECT_ROOT}/state/bin/${DEST_NAME}}"

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  usage
  exit 2
fi

ZELLIJ_SRC="$1"

if [ ! -d "${ZELLIJ_SRC}" ]; then
  echo "error: source checkout not found: ${ZELLIJ_SRC}" >&2
  usage
  exit 2
fi

if [ ! -f "${PATCH_FILE}" ]; then
  echo "error: patch not found: ${PATCH_FILE}" >&2
  exit 1
fi

if [ ! -f "${CHROME_PATCH_FILE}" ]; then
  echo "error: patch not found: ${CHROME_PATCH_FILE}" >&2
  exit 1
fi

if [ ! -f "${LICENSE_FILE}" ]; then
  echo "error: upstream license not found (refusing to build without it): ${LICENSE_FILE}" >&2
  exit 1
fi

if [ -e "${DEST_FILE}" ]; then
  echo "error: destination already exists, refusing to overwrite: ${DEST_FILE}" >&2
  exit 1
fi

for tool in git cargo rustc tar; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "error: required tool '${tool}' not found on PATH." >&2
    echo "Install the Rust 1.95 toolchain (rustup) plus the upstream native" >&2
    echo "prerequisites (C compiler, pkg-config, OpenSSL dev headers) and retry." >&2
    exit 1
  fi
done

RUSTC_VERSION="$(rustc --version | awk '{print $2}')"
RUST_MAJOR="$(echo "${RUSTC_VERSION}" | cut -d. -f1)"
RUST_MINOR="$(echo "${RUSTC_VERSION}" | cut -d. -f2)"
if [ "${RUST_MAJOR}" != "1" ] || [ "${RUST_MINOR}" -lt 95 ]; then
  echo "error: Rust 1.95+ required (found rustc ${RUSTC_VERSION})." >&2
  echo "Install it, e.g.: rustup toolchain install 1.95" >&2
  exit 1
fi

ACTUAL_HEAD="$(git -C "${ZELLIJ_SRC}" rev-parse HEAD)"
if [ "${ACTUAL_HEAD}" != "${PINNED_HEAD}" ]; then
  echo "error: checkout HEAD is ${ACTUAL_HEAD}, expected ${PINNED_HEAD}." >&2
  echo "Check out the pinned commit in that repository and retry; this script" >&2
  echo "does not modify the checkout." >&2
  exit 1
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT INT TERM

git -C "${ZELLIJ_SRC}" archive HEAD | tar -x -C "${WORK_DIR}"

(cd "${WORK_DIR}" && git apply --check "${PATCH_FILE}" && git apply "${PATCH_FILE}")
(cd "${WORK_DIR}" && git apply --check "${CHROME_PATCH_FILE}" && git apply "${CHROME_PATCH_FILE}")

TARGET_DIR="${CARGO_TARGET_DIR:-${WORK_DIR}/target}"
if [[ "${TARGET_DIR}" != /* ]]; then
  TARGET_DIR="${WORK_DIR}/${TARGET_DIR}"
fi
(cd "${WORK_DIR}" && cargo build --locked --release --no-default-features --bin zellij --target-dir "${TARGET_DIR}")

BUILT_BIN="${TARGET_DIR}/release/zellij"
if [ ! -x "${BUILT_BIN}" ]; then
  echo "error: expected build output missing: ${BUILT_BIN}" >&2
  exit 1
fi

if [ "${TDNEXT_TEST_PATCHED_CORE:-0}" = "1" ]; then
  echo "TDNEXT_TEST_PATCHED_CORE=1: testing the exported patched source (no install until tests pass)."
  (cd "${WORK_DIR}" && RUST_MIN_STACK=16777216 cargo test --locked --no-default-features -p zellij-utils --lib --target-dir "${TARGET_DIR}")
  SERVER_TEST_LOG="${WORK_DIR}/zellij-server-navigation-tests.log"
  if ! (cd "${WORK_DIR}" && RUST_MIN_STACK=16777216 cargo test --locked --no-default-features -p zellij-server --lib --target-dir "${TARGET_DIR}" -- set_pane_fixed_width navigation_ 2>&1 | tee "${SERVER_TEST_LOG}"); then
    echo "error: zellij-server navigation/fixed-width tests failed." >&2
    exit 1
  fi
  if ! grep -Eq "test result: ok\. [1-9][0-9]* passed" "${SERVER_TEST_LOG}"; then
    echo "error: zellij-server test filter matched no passing tests; refusing to treat this as a pass." >&2
    exit 1
  fi
fi

ACTION_HELP="$("${BUILT_BIN}" action --help)"
for required in open-tab-with-return open-tab-with-return-from-header restore-focus-from-header return-to-origin set-pane-fixed-width; do
  if ! [[ "${ACTION_HELP}" =~ (^|[[:space:]])${required}([[:space:]]|$) ]]; then
    echo "error: built binary lacks '${required}' in 'action --help'." >&2
    exit 1
  fi
done

if [ "$("${BUILT_BIN}" --tdnext-chrome-version)" != "1" ]; then
  echo "error: built binary lacks native chrome version 1." >&2
  exit 1
fi

if [ -e "${DEST_FILE}" ]; then
  echo "error: destination appeared during build, refusing to overwrite: ${DEST_FILE}" >&2
  exit 1
fi

mkdir -p "$(dirname "${DEST_FILE}")"
STAGE_FILE="${DEST_FILE}.tmp.$$"
install -m 0755 "${BUILT_BIN}" "${STAGE_FILE}"
if ! ln "${STAGE_FILE}" "${DEST_FILE}"; then
  rm -f "${STAGE_FILE}"
  echo "error: install failed; existing file left untouched: ${DEST_FILE}" >&2
  exit 1
fi
rm -f "${STAGE_FILE}"

echo "installed: ${DEST_FILE}"
echo "Upstream Zellij license retained at: ${LICENSE_FILE}"
