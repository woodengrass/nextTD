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

for var in PYTEST_ADDOPTS PYTEST_PLUGINS PYTEST_DISABLE_PLUGIN_AUTOLOAD; do
  if [[ -n "${!var:-}" ]]; then
    echo "ci-run-pytest: refusing inherited $var for the full suite" >&2
    exit 1
  fi
  unset "$var"
done
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
LOG="$WORK/pytest.log"
export TDNEXT_DESELECTED_REPORT="$WORK/deselected"
: > "$TDNEXT_DESELECTED_REPORT"
: > "$WORK/expected"
if [[ "${TDNEXT_GOLDEN_VIA_VERIFY_DIST:-0}" = 1 ]]; then
  uv run python - <<'PY' | sort > "$WORK/expected"
import runpy

gate = runpy.run_path("scripts/verify-dist.py")
for scene in gate["lock_screens"]():
    for term in gate["TERMINALS"]:
        print(f'{gate["GOLDEN_TEST"]}::{gate["GOLDEN_CASE"]}[{scene}-{term}]')
PY
fi

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
sort "$TDNEXT_DESELECTED_REPORT" | diff -u "$WORK/expected" - || {
  echo "ci-run-pytest: deselected node IDs differ from the expected golden set" >&2
  exit 1
}
if ! grep -qE "[0-9]+ passed" "${LOG}"; then
  echo "ci-run-pytest: error: no passing tests observed; refusing an empty pass." >&2
  exit 1
fi
echo "ci-run-pytest: ok (full suite ran once with no skips)."
