#!/usr/bin/env bash
# Rebuild the pinned Zellij 0.45.1 core-navigation patch into a
# project-local executable without touching live sessions.
#
# Usage:
#   scripts/build-zellij-core-navigation.sh <path-to-zellij-git-checkout>
#
# Inputs:
#   $1  Existing local Zellij git checkout (pristine or dirty working tree
#       is accepted; only its committed HEAD is used).
#
# What it does:
#   1. Verifies the checkout HEAD is exactly
#      efd8fd5a89a20c07a111d248ad7fce53848d2c18.
#   2. Exports that committed HEAD with `git archive` to a private mktemp
#      directory (working-tree changes are never copied; the checkout is
#      never modified).
#   3. Applies patches/zellij-0.45.1-core-navigation.patch with `git apply`.
#   4. Builds `cargo build --locked --release --no-default-features
#      --bin zellij` inside the exported source.
#   5. Checks `./zellij action --help` lists all required navigation commands.
#   6. Atomically installs state/bin/zellij-core-navigation-v3 ONLY if that
#      destination is absent (never overwrites).
#
# Environment:
#   PATH and CARGO_HOME are used as inherited from the caller's
#   environment and are never overridden here. Install the Rust 1.95
#   toolchain (e.g. `rustup toolchain install 1.95`) plus the upstream
#   native build prerequisites (a C compiler, pkg-config, and OpenSSL
#   development headers; see the Zellij repository README) before running.
#   Cargo may download locked dependencies unless its offline mode is enabled.
#   The script does not use sudo, change Git configuration, commit, or operate
#   on any session.
#
# The patch is experimental until verified, and this script never runs
# automatically: invoke it explicitly when you want a local rebuild.
set -euo pipefail

PINNED_HEAD="efd8fd5a89a20c07a111d248ad7fce53848d2c18"
PATCH_NAME="zellij-0.45.1-core-navigation.patch"
LICENSE_NAME="LICENSE.zellij-0.45.1"
DEST_NAME="zellij-core-navigation-v3"

usage() {
  echo "usage: $(basename "$0") <path-to-zellij-git-checkout>" >&2
  echo "Rebuilds the pinned Zellij core patch to state/bin/${DEST_NAME} (no overwrite)." >&2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PATCH_FILE="${PROJECT_ROOT}/patches/${PATCH_NAME}"
LICENSE_FILE="${PROJECT_ROOT}/patches/${LICENSE_NAME}"
DEST_FILE="${PROJECT_ROOT}/state/bin/${DEST_NAME}"

if [ "$#" -ne 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
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

ACTION_HELP="$("${BUILT_BIN}" action --help)"
for required in open-tab-with-return open-tab-with-return-from-header restore-focus-from-header return-to-origin set-pane-fixed-width; do
  if ! [[ "${ACTION_HELP}" =~ (^|[[:space:]])${required}([[:space:]]|$) ]]; then
    echo "error: built binary lacks '${required}' in 'action --help'." >&2
    exit 1
  fi
done

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
