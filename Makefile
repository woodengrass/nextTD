# Project-wide check entry.
#
# Everything resolves relative to this Makefile, so `make check` works from
# any worktree or checkout name. The workflow runs `make check` after the
# usual setup (Rust 1.95 toolchain, Python 3.12, uv, system prerequisites,
# pinned upstream Zellij checkout); pass the checkout via the environment:
#
#   TDNEXT_ZELLIJ_SRC=<path-to-pinned-zellij> make check
#   make check ZELLIJ_SRC=<path-to-pinned-zellij>
#
# Without either, the pinned commit is shallow-fetched into a private temp
# directory. See scripts/check-project.sh for the covered steps.
#
# `make check` builds once, stages the siblings, archives once, and verifies
# the archive with scripts/verify-dist.py (the same gate as `make
# verify-dist`). When ARCHIVE_OUT names an absolute file path, the tested
# archive bytes plus a `<path>.sha256` file (sha256sum text format:
# `<hash><two spaces><basename>`) are written there, but only after every
# gate succeeds. `make verify-dist ARCHIVE=<archive.tar.gz>` runs only the
# archive gate, using the project venv interpreter when present so the
# verifier can import the project test dependencies.
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ARCHIVE ?=
ARCHIVE_OUT ?=

.PHONY: check dist verify-dist

check:
	bash "$(ROOT)/scripts/check-project.sh" "$(ROOT)" "$(ZELLIJ_SRC)" "$(ARCHIVE_OUT)"

dist:
	bash "$(ROOT)/scripts/build-dist.sh" "$(DIST)" "$(ZELLIJ_SRC)"

verify-dist:
	test -n "$(ARCHIVE)" || { echo "usage: make verify-dist ARCHIVE=<archive.tar.gz>" >&2; exit 2; }
	test -f "$(ROOT)/scripts/verify-dist.py" || { echo "verify-dist: scripts/verify-dist.py is missing (archive verifier track owns it)." >&2; exit 1; }
	if test -x "$(ROOT)/.venv/bin/python"; then PY="$(ROOT)/.venv/bin/python"; else PY="python3"; fi; \
	"$${PY}" "$(ROOT)/scripts/verify-dist.py" "$(ARCHIVE)"
