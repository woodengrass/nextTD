#!/usr/bin/env bash
# Project-wide check behind `make check`.
#
# Usage:
#   bash scripts/check-project.sh [repo-root] [zellij-checkout]
#   make check [ZELLIJ_SRC=/path/to/zellij-checkout]
#
# Arguments / environment:
#   $1 / repo root; defaults to the checkout containing this script, so the
#      entry works from any worktree or checkout name (no absolute path or
#      checkout name appears below).
#   $2 / TDNEXT_ZELLIJ_SRC: an existing local Zellij git checkout whose HEAD
#      must be the pinned commit enforced by
#      scripts/build-zellij-core-navigation.sh (only its committed HEAD is
#      used; the checkout is never modified). When neither is given, the
#      pinned commit is shallow-fetched (depth 1) into a private temp
#      directory.
#   $3 / ARCHIVE_OUT: optional absolute path of the release archive to write
#      (e.g. /tmp/tdnext-linux-x86_64.tar.gz). The tested archive bytes are
#      copied there together with a `<path>.sha256` checksum file in
#      sha256sum text format (`<hash><two spaces><basename>`), but only
#      after every gate below succeeds.
#
# Scope (mirrors the workflow `ci` job; the tagged release smoke stays in
# the workflow, which owns it):
#   1. scripts/ci-verify-env.sh (Linux x86_64, Rust 1.95.x, tmux/btop/nvim/vi,
#      /dev/shm on a different device than the actual temp dir, user.* xattrs
#      on the repo and temp filesystems).
#   2. Project-local .venv rebuilt when its prefix no longer matches this
#      checkout (stale shebangs after a relocation), then uv sync --locked.
#   3. For the launcher (native-desktop, mandatory) and every crate discovered
#      via native-panes/*/Cargo.toml (at least one required): cargo fmt
#      --check, cargo clippy --locked --all-targets -- -D warnings,
#      cargo test --locked (a pass that ran zero tests fails), cargo build
#      --locked --release.
#   4. The pinned patched core via scripts/build-zellij-core-navigation.sh
#      with TDNEXT_TEST_PATCHED_CORE=1 (sibling suites run in the exported
#      patched source; a test filter matching zero tests aborts). The core is
#      installed to native-desktop/target/release/tdnext-core only when that
#      file is absent; otherwise the existing binary is probe-verified and the
#      fresh build goes to a private temp path, never overwriting live state.
#      The full pytest suite below always runs against the freshly built core
#      via TDNEXT_CORE_BINARY.
#   5. Project-local state/bin/btop is installed from PATH only when absent;
#      an existing file is never overwritten, and a present-but-not-executable
#      one fails loudly instead of being repaired silently. Every artifact
#      must be executable mode 0755 and the core must report native chrome
#      version 1.
#   6. uv run ruff check ., uv run basedpyright,
#      scripts/ci-run-pytest.sh (full default suite once, except the golden
#      file deselected via TDNEXT_GOLDEN_VIA_VERIFY_DIST=1; skips fail, an
#      empty pass fails).
#   7. uv build --wheel into a private temp dir (exactly one wheel).
#   8. scripts/build-dist.sh stages the tested siblings (reusing the fresh
#      core from step 4, no rebuild), the stage is archived exactly once
#      into tdnext-linux-x86_64.tar.gz, and scripts/verify-dist.py verifies
#      that archive (same gate as `make verify-dist`: venv python when
#      present, else python3). The verifier owns tests/test_desktop_golden.py
#      against the extracted dist root, so golden coverage still runs exactly
#      once per check. ARCHIVE_OUT (plus `<path>.sha256`) is written only
#      after this gate passes.
#
# Every step propagates failures (set -euo pipefail, no `|| true`); the first
# failure aborts the run and names the step. All private temp directories are
# removed by one trap on success and on failure. Total wall-clock time is
# printed at the end.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
ZELLIJ_SRC_INPUT="${2:-${TDNEXT_ZELLIJ_SRC:-}}"
ARCHIVE_OUT="${3:-${ARCHIVE_OUT:-}}"
cd "${ROOT}"

# Rust crate builds use their default target/ dirs (reusing existing build
# caches). The patched-core build must never share them: the core script runs
# in its own private target dir only when CARGO_TARGET_DIR is unset here, so
# the core target dir stays isolated from every crate build.
unset CARGO_TARGET_DIR

START="$(date +%s)"
CURRENT="startup"
TEMP_DIRS=()

on_err() {
  echo "check-project: error: failed at step '${CURRENT}' (after $(( $(date +%s) - START ))s)." >&2
}
cleanup_all() {
  local dir
  for dir in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
    test -n "${dir}" && test -d "${dir}" && rm -rf "${dir}"
  done
}
trap on_err ERR
trap cleanup_all EXIT INT TERM

mktemp_dir() {
  # Creates a private temp dir, registers it for the single exit trap, and
  # reports it in MKTEMP_DIR. Assigns via a global (never command
  # substitution: that runs in a subshell where the registration would be
  # lost and the dir would leak).
  MKTEMP_DIR="$(mktemp -d -t "$1-XXXXXX")"
  TEMP_DIRS+=("${MKTEMP_DIR}")
}

step() {
  echo "check-project: [$(date +%H:%M:%S)] $*"
}

fail() {
  echo "check-project: error: $1" >&2
  exit 1
}

discover_crates() {
  # $1 = root. Prints the launcher manifest first, then every native pane
  # manifest; fails when mandatory coverage is absent.
  local root="$1" manifest count
  for manifest in "${root}/native-desktop/Cargo.toml" "${root}/native-manager/Cargo.toml" "${root}/native-panes/settings/Cargo.toml"; do
    test -f "${manifest}" \
      || { echo "check-project: error: mandatory crate missing: ${manifest}" >&2; return 1; }
  done
  printf '%s\n' "${root}/native-desktop/Cargo.toml" "${root}/native-manager/Cargo.toml"
  count=0
  shopt -s nullglob
  for manifest in "${root}"/native-panes/*/Cargo.toml; do
    printf '%s\n' "${manifest}"
    count=$((count + 1))
  done
  shopt -u nullglob
  test "${count}" -gt 0 \
    || { echo "check-project: error: no native pane crates discovered under native-panes/*/Cargo.toml" >&2; return 1; }
}

assert_tests_ran() {
  # $1 = cargo test log, $2 = crate label. Refuse a pass that ran zero tests.
  grep -Eq "test result: ok\. [1-9][0-9]* passed" "$1" \
    || { echo "check-project: error: cargo test ran zero tests for $2; refusing an empty pass." >&2; return 1; }
}

ensure_project_btop() {
  # $1 = root. Additive only: an existing file is never overwritten, and a
  # present-but-not-executable one fails loudly instead of being repaired.
  local dest="$1/state/bin/btop"
  if test -e "${dest}"; then
    test -x "${dest}" \
      || { echo "check-project: error: ${dest} exists but is not executable; refusing to overwrite it (remove or repair it and retry)." >&2; return 1; }
    echo "check-project: state/bin/btop already present; leaving untouched."
  else
    command -v btop >/dev/null 2>&1 \
      || { echo "check-project: error: btop not found on PATH and ${dest} is absent." >&2; return 1; }
    mkdir -p "$(dirname "${dest}")"
    install -m 0755 "$(command -v btop)" "${dest}"
  fi
}

# 1. Supported platform and native test prerequisites.
CURRENT="verify environment (scripts/ci-verify-env.sh)"
step "${CURRENT}"
bash scripts/ci-verify-env.sh "${ROOT}"

# 2. Locked project-local Python environment.
CURRENT="require uv on PATH"
step "${CURRENT}"
command -v uv >/dev/null 2>&1 || fail "uv not found on PATH (workflow setup installs uv 0.9.5)."
uv --version

CURRENT="project-local virtualenv (rebuild when relocated)"
step "${CURRENT}"
VENV="${ROOT}/.venv"
# A relocated checkout keeps a working venv python but leaves console-script
# shebangs (e.g. .venv/bin/pytest) pointing at the old path; uv sync does not
# rewrite them, so detect the stale canary and rebuild the project-local env.
VENV_FRESH=0
if test -x "${VENV}/bin/python" && test -f "${VENV}/bin/pytest"; then
  case "$(head -1 "${VENV}/bin/pytest" 2>/dev/null)" in
    "#!${VENV}/"*) VENV_FRESH=1 ;;
  esac
fi
if test "${VENV_FRESH}" -ne 1; then
  step "project-local env is missing or has stale shebangs; rebuilding it (no global installs)."
  rm -rf "${VENV}"
fi
test -x "${VENV}/bin/python" || { step "creating project-local env"; uv venv; }
uv sync --locked

# 3. Rust crate discovery: launcher plus every shipped native pane.
CURRENT="discover Rust crates"
step "${CURRENT}"
DISCOVERY_LOG=""
mktemp_dir tdnext-discover
DISCOVERY_LOG="${MKTEMP_DIR}/crates.txt"
discover_crates "${ROOT}" > "${DISCOVERY_LOG}" || fail "Rust crate discovery found no coverage (see above)."
mapfile -t CRATE_MANIFESTS < "${DISCOVERY_LOG}"
echo "check-project: crates:"
for manifest in "${CRATE_MANIFESTS[@]}"; do
  echo "check-project:   - ${manifest#${ROOT}/}"
done

# Locate the pinned upstream checkout early so a bad input fails fast,
# before the slow crate loop below.
CURRENT="locate pinned upstream Zellij checkout"
step "${CURRENT}"
PINNED_HEAD="$(sed -n 's/^PINNED_HEAD="\(.*\)"/\1/p' "${ROOT}/scripts/build-zellij-core-navigation.sh")"
test -n "${PINNED_HEAD}" || fail "could not read PINNED_HEAD from scripts/build-zellij-core-navigation.sh"
FETCH_DIR=""
if test -n "${ZELLIJ_SRC_INPUT}"; then
  ZELLIJ_SRC="${ZELLIJ_SRC_INPUT}"
  test -d "${ZELLIJ_SRC}" || fail "Zellij checkout not found: ${ZELLIJ_SRC}"
  test "$(git -C "${ZELLIJ_SRC}" rev-parse HEAD)" = "${PINNED_HEAD}" \
    || fail "Zellij checkout HEAD is $(git -C "${ZELLIJ_SRC}" rev-parse HEAD 2>/dev/null || echo '<unreadable>'), expected ${PINNED_HEAD}"
  echo "check-project: reusing pinned checkout at ${ZELLIJ_SRC} (read-only; the core script exports via git archive)."
else
  FETCH_DIR=""
  mktemp_dir tdnext-zellij
  FETCH_DIR="${MKTEMP_DIR}"
  step "shallow-fetch pinned Zellij ${PINNED_HEAD} (depth 1) into private ${FETCH_DIR}"
  git init -q "${FETCH_DIR}"
  git -C "${FETCH_DIR}" remote add origin https://github.com/zellij-org/zellij.git
  git -C "${FETCH_DIR}" fetch --depth 1 origin "${PINNED_HEAD}"
  git -C "${FETCH_DIR}" checkout "${PINNED_HEAD}"
  test "$(git -C "${FETCH_DIR}" rev-parse HEAD)" = "${PINNED_HEAD}" \
    || fail "fetched checkout HEAD mismatch for ${PINNED_HEAD}"
  ZELLIJ_SRC="${FETCH_DIR}"
fi

for manifest in "${CRATE_MANIFESTS[@]}"; do
  crate_rel="$(dirname "${manifest}")"
  crate_rel="${crate_rel#${ROOT}/}"

  CURRENT="cargo fmt --check (${crate_rel})"
  step "${CURRENT}"
  cargo fmt --check --manifest-path "${manifest}"

  CURRENT="cargo clippy --locked --all-targets (${crate_rel})"
  step "${CURRENT}"
  cargo clippy --locked --all-targets --manifest-path "${manifest}" -- -D warnings

  CURRENT="cargo test --locked (${crate_rel})"
  step "${CURRENT}"
  TEST_LOG=""
  mktemp_dir tdnext-cargo-test
  TEST_LOG="${MKTEMP_DIR}/cargo-test.log"
  cargo test --locked --manifest-path "${manifest}" 2>&1 | tee "${TEST_LOG}"
  test "${PIPESTATUS[0]}" -eq 0 || fail "cargo test failed for ${crate_rel}."
  assert_tests_ran "${TEST_LOG}" "${crate_rel}" || fail "cargo test ran zero tests for ${crate_rel}."
  CURRENT="cargo build --locked --release (${crate_rel})"
  step "${CURRENT}"
  cargo build --locked --release --manifest-path "${manifest}"
done

# 4. Pinned patched upstream core (plus its sibling suites).
CURRENT="build pinned patched core with sibling suites (TDNEXT_TEST_PATCHED_CORE=1)"
STANDARD_CORE="${ROOT}/native-desktop/target/release/tdnext-core"
CORE_DEST="${STANDARD_CORE}"
if test -e "${STANDARD_CORE}"; then
  mktemp_dir tdnext-core
  CORE_DEST="${MKTEMP_DIR}/tdnext-core"
  echo "check-project: standard core exists; fresh build goes to ${CORE_DEST} (no overwrite)."
fi
step "${CURRENT} -> ${CORE_DEST}"
TDNEXT_TEST_PATCHED_CORE=1 bash scripts/build-zellij-core-navigation.sh "${ZELLIJ_SRC}" "${CORE_DEST}"

# 5. Project-local btop (additive only) and artifact probes.
CURRENT="provide project-local btop without overwriting"
step "${CURRENT}"
ensure_project_btop "${ROOT}" || fail "project-local btop gate failed (see above)."

CURRENT="check release artifacts (modes + native chrome probes)"
step "${CURRENT}"
# Cargo honors the caller's umask, so normalize modes explicitly. These are
# ordinary target build outputs, not live local state.
chmod 0755 \
  "${ROOT}/native-desktop/target/release/tdnext-desktop" \
  "${ROOT}/native-panes/files/target/release/tdnext-files"
for binary in \
  "${ROOT}/native-desktop/target/release/tdnext-desktop" \
  "${STANDARD_CORE}" \
  "${ROOT}/native-panes/files/target/release/tdnext-files" \
  "${ROOT}/state/bin/btop"; do
  test -x "${binary}" || fail "artifact not executable: ${binary}"
  test "$(stat -c %a "${binary}")" = 755 || fail "artifact mode is not 0755: ${binary}"
done
test "$("${STANDARD_CORE}" --tdnext-chrome-version)" = "1" \
  || fail "standard core is not native chrome version 1: ${STANDARD_CORE}"
test "$("${CORE_DEST}" --tdnext-chrome-version)" = "1" \
  || fail "freshly built core is not native chrome version 1: ${CORE_DEST}"

# 6. Python lint, types, and the full suite once against the fresh core.
CURRENT="lint Python (uv run ruff check .)"
step "${CURRENT}"
uv run ruff check .

CURRENT="type-check Python (uv run basedpyright)"
step "${CURRENT}"
uv run basedpyright

CURRENT="run the full suite once on the fresh core (scripts/ci-run-pytest.sh)"
step "${CURRENT}"
export TDNEXT_CORE_BINARY="${CORE_DEST}"
# The golden desktop file runs inside the archive gate below (step 8 owns it
# via scripts/verify-dist.py), so it is deselected exactly here and nowhere
# else: one run per check, never skipped, never duplicated.
export TDNEXT_GOLDEN_VIA_VERIFY_DIST=1
bash scripts/ci-run-pytest.sh "${ROOT}"

# 7. Packaging: the wheel must build.
CURRENT="build the wheel (uv build --wheel)"
step "${CURRENT}"
WHEEL_DIR=""
mktemp_dir tdnext-wheel
WHEEL_DIR="${MKTEMP_DIR}"
uv build --wheel --out-dir "${WHEEL_DIR}"
test "$(find "${WHEEL_DIR}" -maxdepth 1 -name '*.whl' | wc -l)" -eq 1 \
  || fail "expected exactly 1 wheel in ${WHEEL_DIR}"

CURRENT="stage siblings, archive once, verify the archive (scripts/verify-dist.py)"
step "${CURRENT}"
test -f "${ROOT}/scripts/verify-dist.py" \
  || fail "scripts/verify-dist.py is missing (archive verifier track owns it); refusing to pass without the archive gate."
STAGE_PARENT=""
mktemp_dir tdnext-stage
STAGE_PARENT="${MKTEMP_DIR}"
DIST_STAGE="${STAGE_PARENT}/tdnext"
# Reuse the probed fresh core: the core is built once per check (step 4),
# and staging must archive those exact tested bytes, not a second build.
export TDNEXT_PREBUILT_CORE="${CORE_DEST}"
bash scripts/build-dist.sh "${DIST_STAGE}" "${ZELLIJ_SRC}"
ARCHIVE="${STAGE_PARENT}/tdnext-linux-x86_64.tar.gz"
tar -C "${STAGE_PARENT}" -czf "${ARCHIVE}" tdnext
# Same interpreter rule as `make verify-dist`: the verifier imports project
# test dependencies, so prefer the synced project venv when present.
if test -x "${ROOT}/.venv/bin/python"; then
  VERIFY_PY="${ROOT}/.venv/bin/python"
else
  VERIFY_PY="python3"
fi
"${VERIFY_PY}" "${ROOT}/scripts/verify-dist.py" "${ARCHIVE}"
ARCHIVE_SHA="$(sha256sum "${ARCHIVE}")"
ARCHIVE_BYTES="$(stat -c %s "${ARCHIVE}")"
echo "check-project: archive verified: tdnext-linux-x86_64.tar.gz (${ARCHIVE_BYTES} bytes, sha256 ${ARCHIVE_SHA%% *})."
if test -n "${ARCHIVE_OUT:-}"; then
  case "${ARCHIVE_OUT}" in
    /*) ;;
    *) fail "ARCHIVE_OUT must be an absolute path: ${ARCHIVE_OUT}" ;;
  esac
  test -e "${ARCHIVE_OUT}" -o -L "${ARCHIVE_OUT}" \
    && fail "ARCHIVE_OUT already exists; refusing to overwrite: ${ARCHIVE_OUT}"
  mkdir -p "$(dirname "${ARCHIVE_OUT}")"
  cp -- "${ARCHIVE}" "${ARCHIVE_OUT}"
  ( cd "$(dirname "${ARCHIVE_OUT}")" && sha256sum "$(basename "${ARCHIVE_OUT}")" > "${ARCHIVE_OUT}.sha256" )
  echo "check-project: exported tested archive to ${ARCHIVE_OUT} (+ .sha256)."
fi

DUR="$(( $(date +%s) - START ))"
echo "check-project: ok in ${DUR}s (crates: ${#CRATE_MANIFESTS[@]}, fresh core: ${CORE_DEST}, wheel: $(basename "$(find "${WHEEL_DIR}" -maxdepth 1 -name '*.whl')"), archive: tdnext-linux-x86_64.tar.gz)."
