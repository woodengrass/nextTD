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
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: check

check:
	bash "$(ROOT)/scripts/check-project.sh" "$(ROOT)" "$(ZELLIJ_SRC)"
