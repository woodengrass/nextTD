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
#
# Exit status is non-zero when pytest fails, when no tests ran, or when
# the summary reports a skip. Report skips explicitly instead of passing
# with reduced coverage.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "${ROOT}"

export TDNEXT_TEST_PATCHED_CORE=1

LOG="$(mktemp)"
trap 'rm -f "${LOG}"' EXIT INT TERM

set +e
uv run pytest -q -ra 2>&1 | tee "${LOG}"
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
if ! grep -qE "[0-9]+ passed" "${LOG}"; then
  echo "ci-run-pytest: error: no passing tests observed; refusing an empty pass." >&2
  exit 1
fi
echo "ci-run-pytest: ok (full suite ran once with no skips)."
