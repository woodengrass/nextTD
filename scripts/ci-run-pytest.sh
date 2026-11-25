#!/usr/bin/env bash
# Run the full Python suite exactly once against the current binary
# artifacts, then refuse silent coverage loss: any skipped test fails.
#
# Usage:
#   scripts/ci-run-pytest.sh [repo-root]
#
# Environment:
#   TDNEXT_TEST_PATCHED_CORE=1 is exported here so the checkout-level
#   patched-core tests (owned by a sibling script) run; the in-tree suite
#   ignores the flag when those tests are absent.
#   TDNEXT_GOLDEN_VIA_VERIFY_DIST=1 deselects exactly
#   tests/test_desktop_golden.py here, and only then: the calling check run
#   invokes scripts/verify-dist.py unconditionally afterwards, which owns
#   that component and runs it against the archived distribution (extracted
#   dist root mapped to TDNEXT_DESKTOP_BINARY/TDNEXT_FILES_BINARY/
#   TDNEXT_CORE_BINARY). Deselected is not skipped, and the component still
#   runs exactly once per check. Standalone runs leave the flag unset and
#   execute the whole suite including the golden file.
#
# Exit status is non-zero when pytest fails, when no tests ran, when
# the summary reports a skip, or when the golden deselection state does not
# match the flag. Report skips explicitly instead of passing
# with reduced coverage.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "${ROOT}"

export TDNEXT_TEST_PATCHED_CORE=1

GOLDEN_SPEC="tests/test_desktop_golden.py"
PYTEST_ARGS=()
if test "${TDNEXT_GOLDEN_VIA_VERIFY_DIST:-0}" = "1"; then
  PYTEST_ARGS=(--deselect "${GOLDEN_SPEC}")
fi

LOG="$(mktemp)"
trap 'rm -f "${LOG}"' EXIT INT TERM

set +e
uv run pytest -q -ra "${PYTEST_ARGS[@]}" 2>&1 | tee "${LOG}"
STATUS="${PIPESTATUS[0]}"
set -e

if test "${STATUS}" -ne 0; then
  echo "ci-run-pytest: error: pytest exited with status ${STATUS}; see the log above." >&2
  exit "${STATUS}"
fi
if grep -qE "^(SKIPPED|XFAIL|XPASS)" "${LOG}"; then
  echo "ci-run-pytest: error: isolated CI forbids skipped tests; summary:" >&2
  grep -E "^(SKIPPED|XFAIL|XPASS)" "${LOG}" >&2
  exit 1
fi
if test "${TDNEXT_GOLDEN_VIA_VERIFY_DIST:-0}" = "1"; then
  # The verifier owns the golden component in this run: prove it was excluded
  # here (so it runs exactly once there, never twice and never zero times).
  grep -qE "[0-9]+ deselected" "${LOG}" \
    || { echo "ci-run-pytest: error: expected ${GOLDEN_SPEC} to be deselected via TDNEXT_GOLDEN_VIA_VERIFY_DIST=1." >&2; exit 1; }
else
  if grep -qE "[0-9]+ deselected" "${LOG}"; then
    echo "ci-run-pytest: error: unexpected deselection without TDNEXT_GOLDEN_VIA_VERIFY_DIST=1; refusing reduced coverage." >&2
    exit 1
  fi
fi
if ! grep -qE "[0-9]+ passed" "${LOG}"; then
  echo "ci-run-pytest: error: no passing tests observed; refusing an empty pass." >&2
  exit 1
fi
echo "ci-run-pytest: ok (full suite ran once with no skips)."
