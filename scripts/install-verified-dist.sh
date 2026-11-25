#!/usr/bin/env bash
# Activate exactly the distribution archive that passed verification.
#
# Operator-local use only (local machine or VM staging target). This script
# unpacks ONE verified tarball under DEST/releases/ and flips the
# DEST/current symlink to it with a same-filesystem atomic rename. It never
# touches user settings or sessions (TDNEXT_STATE_DIR and friends), never
# publishes anything, and performs no network or external writes: the only
# writes are the new release directory, its digest.sha256 activation record,
# and the current pointer, all under DEST.
#
# Trust boundary (exactly ONE trust option is enforced):
#   UNTRUSTED: the tarball bytes and anything inside them, including any
#     checksum file shipped inside the archive (never read).
#   TRUSTED, option --verify: the shared gate `make verify-dist
#     ARCHIVE=<private immutable copy>` (scripts/verify-dist.py: frozen lock,
#     exact tar layout, probes, functional smoke, archive golden scenes) runs
#     on the exact copied bytes. Exit 0 is required, AND the sha256 digest
#     the verifier prints must equal the digest of the copied bytes; only
#     that verified copy is unpacked.
#   TRUSTED, option --digest-file PATH: a digest produced by a passed
#     verification run (e.g. the CI digest written after the passed run) and
#     carried by the operator over a separate channel. It is never auto-read
#     from the package directory or the archive itself.
#   Without a passing verification or a matching trusted digest, this script
#   exits nonzero and leaves `current` untouched (fail closed).
#
# The freshly extracted private tree is authoritative: the expected layout
# (single top-level tdnext/ with exactly the five sibling executables plus
# the license, all regular files) is checked on the extracted candidate, and
# an existing release directory is reused only after every file's contents
# and modes byte-match the candidate. A tampered release is refused, never
# activated.
#
# Runtime: digest mode needs only bash, tar, sha256sum, ln, mv, stat and
# mktemp (no Python). --verify additionally needs the verification
# workstation: this repo checkout with its Python/venv toolchain, because it
# runs the real verifier.
set -euo pipefail

PROG="install-verified-dist"
EXPECTED_TOP="tdnext"
EXPECTED_MEMBERS="tdnext tdnext-settings tdnext-desktop tdnext-files tdnext-core LICENSE.zellij-0.45.1"
BINARIES="tdnext tdnext-settings tdnext-desktop tdnext-files tdnext-core"

fail() {
  echo "$PROG: error: $1" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage: bash scripts/install-verified-dist.sh --archive <tar.gz> --dest <dir>
       (--digest-file <archive.sha256> | --verify) [--dry-run]

  --archive PATH       candidate distribution tarball (untrusted bytes)
  --dest DIR           activation root; created when absent. Holds
                       releases/<full-sha256>/ trees and the `current` symlink.
  --digest-file PATH   trusted digest in `sha256sum` output format, obtained
                       over a separate operator-side channel (never from the
                       package dir). Exactly one of --digest-file/--verify.
  --verify             run the real verifier (`make verify-dist
                       ARCHIVE=<private immutable copy>`) on the exact bytes
                       being installed first; exit 0 plus a matching printed
                       digest is required. Needs the verification workstation
                       (repo checkout + Python/venv toolchain).
  --dry-run            validate everything and print the deterministic plan
                       without creating a release or switching `current`.
  -h, --help           print this help and exit.
EOF
}

ARCHIVE=""
DEST=""
DIGEST_FILE=""
VERIFY=0
DRY_RUN=0

while (($# > 0)); do
  case "$1" in
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --dest) DEST="${2:-}"; shift 2 ;;
    --digest-file) DIGEST_FILE="${2:-}"; shift 2 ;;
    --verify) VERIFY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1 (see --help)" ;;
  esac
done

test -n "$ARCHIVE" || fail "missing --archive (see --help)"
test -n "$DEST" || fail "missing --dest (see --help)"
test -f "$ARCHIVE" || fail "archive not found: $ARCHIVE"
if ((VERIFY == 1)) && [[ -n "$DIGEST_FILE" ]]; then
  fail "single trust option: pass exactly one of --verify or --digest-file"
fi
if ((VERIFY == 0)) && [[ -z "$DIGEST_FILE" ]]; then
  fail "fail closed: no trusted digest; pass exactly one of --verify or --digest-file"
fi
if [[ -n "$DIGEST_FILE" ]]; then
  test -f "$DIGEST_FILE" || fail "digest file not found: $DIGEST_FILE"
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

WORK=""
LINK_TMP=""
cleanup() {
  if [[ -n "$LINK_TMP" && -L "$LINK_TMP" ]]; then rm -f -- "$LINK_TMP"; fi
  if [[ -n "$WORK" && -d "$WORK" ]]; then rm -rf -- "$WORK"; fi
}
trap cleanup EXIT INT TERM

WORK="$(mktemp -d -t tdnext-install-XXXXXX)"

# Always install from a private immutable copy, so the hashed bytes and the
# unpacked bytes are provably the same even if the source path changes.
COPY="$WORK/archive.tar.gz"
cp -- "$ARCHIVE" "$COPY"
chmod 444 -- "$COPY"

ACTUAL="$(sha256sum -- "$COPY")"
ACTUAL="${ACTUAL%% *}"
ACTUAL="${ACTUAL,,}"

if ((VERIFY == 1)); then
  VERIFY_LOG="$WORK/verify.log"
  if ! make -C "$ROOT" verify-dist "ARCHIVE=$COPY" >"$VERIFY_LOG" 2>&1; then
    echo "$PROG: verification failed; leaving current untouched." >&2
    tail -n 20 -- "$VERIFY_LOG" >&2 || true
    exit 1
  fi
  # Bind the verifier's success to these exact bytes: its printed digest must
  # equal the digest of the copy it ran on.
  REPORTED="$(grep -Eo 'sha256=[0-9a-f]{64}' "$VERIFY_LOG" | tail -n 1)"
  REPORTED="${REPORTED#sha256=}"
  test -n "$REPORTED" || fail "verifier printed no sha256 digest; no activation"
  test "$REPORTED" = "$ACTUAL" \
    || fail "verifier digest $REPORTED != copied bytes $ACTUAL; no activation"
  EXPECTED="$ACTUAL"
  echo "$PROG: verifier passed on the exact bytes being installed (sha256 $ACTUAL)."
else
  # First field of the first content line: the GNU `sha256sum` format the
  # producer emits (`<hash>  <name>`); a bare hex line is also accepted.
  EXPECTED="$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -- "$DIGEST_FILE" | head -n 1)"
  EXPECTED="${EXPECTED%%[[:space:]]*}"
  EXPECTED="${EXPECTED,,}"
  [[ "$EXPECTED" =~ ^[0-9a-f]{64}$ ]] \
    || fail "digest file does not start with a sha256 hex digest: $DIGEST_FILE"
  test "$ACTUAL" = "$EXPECTED" \
    || fail "digest mismatch: archive sha256 $ACTUAL != trusted $EXPECTED; no activation"
fi

# The digest now vouches for these bytes (the verifier already enforced its
# exact layout on them in --verify mode, and the digest pins them in digest
# mode), so validation stays simple: reject path escapes before extract, then
# enforce the exact expected tree on the extracted candidate.
tar -tzf "$COPY" >"$WORK/members.txt"
test -s "$WORK/members.txt" || fail "archive lists no members"
if grep -E '(^/)|(^|/)\.\.(/|$)' "$WORK/members.txt" >/dev/null; then
  fail "archive contains absolute or parent-escaping paths; no activation"
fi
mkdir "$WORK/stage"
tar --no-same-owner --no-same-permissions -xzf "$COPY" -C "$WORK/stage"

mapfile -t TOPS < <(find "$WORK/stage" -mindepth 1 -maxdepth 1)
if ((${#TOPS[@]} != 1)) || [[ "${TOPS[0]}" != "$WORK/stage/$EXPECTED_TOP" ]]; then
  fail "archive must contain exactly one top-level $EXPECTED_TOP/ directory; no activation"
fi
CANDIDATE="${TOPS[0]}"
test -d "$CANDIDATE" && ! test -L "$CANDIDATE" \
  || fail "top-level $EXPECTED_TOP/ is not a real directory; no activation"

check_exact_layout() {
  local root="$1" name count
  count="$(find "$root" -mindepth 1 -maxdepth 1 | wc -l)"
  test "$count" -eq 6 || { echo "expected exactly 6 members, found $count"; return 1; }
  for name in $EXPECTED_MEMBERS; do
    test -f "$root/$name" && ! test -L "$root/$name" \
      || { echo "member $name is missing or not a regular file"; return 1; }
  done
  for name in $BINARIES; do
    test -x "$root/$name" || { echo "member $name is not executable"; return 1; }
  done
  test -s "$root/LICENSE.zellij-0.45.1" || { echo "license is empty"; return 1; }
  return 0
}

# An existing release is reused only when every file byte-matches the freshly
# validated candidate (contents and modes); anything else is refused.
compare_release() {
  local old="$1" new="$2" entry base name om nm
  while IFS= read -r entry; do
    base="$(basename -- "$entry")"
    case " $EXPECTED_MEMBERS digest.sha256 " in
      *" $base "*) ;;
      *) echo "existing release has unexpected member: $base"; return 1 ;;
    esac
  done < <(find "$old" -mindepth 1 -maxdepth 1)
  for name in $EXPECTED_MEMBERS; do
    test -f "$old/$name" && ! test -L "$old/$name" \
      || { echo "existing member $name is missing or not a regular file"; return 1; }
    om="$(stat -c %a -- "$old/$name")"; nm="$(stat -c %a -- "$new/$name")"
    test "$om" = "$nm" || { echo "mode mismatch on $name ($om != $nm)"; return 1; }
    cmp -s -- "$old/$name" "$new/$name" \
      || { echo "content mismatch on $name"; return 1; }
  done
  return 0
}

LAYOUT_ERR=""
LAYOUT_ERR="$(check_exact_layout "$CANDIDATE" 2>&1)" \
  || fail "package layout rejected ($LAYOUT_ERR); no activation"

RELEASE="$DEST/releases/$ACTUAL"

OLD="absent"
if [[ -L "$DEST/current" ]]; then
  OLD="$(readlink -- "$DEST/current")"
elif [[ -e "$DEST/current" ]]; then
  fail "DEST/current exists and is not a symlink; refusing to clobber: $DEST/current"
fi

if [[ -d "$RELEASE" ]]; then
  REUSE_ERR=""
  REUSE_ERR="$(compare_release "$RELEASE" "$CANDIDATE" 2>&1)" \
    || fail "existing release $RELEASE differs from the verified bytes ($REUSE_ERR); refusing to activate"
  REUSE="reused byte-identical release"
else
  REUSE="new release"
fi

if ((DRY_RUN == 1)); then
  cat <<EOF
$PROG: dry-run plan (no changes made)
archive_sha256=$ACTUAL
expected_sha256=$EXPECTED
layout=ok (tdnext/ with 5 executables + license, regular files only)
release=$RELEASE ($REUSE)
current: $OLD -> releases/$ACTUAL
dry-run: no switch performed
EOF
  exit 0
fi

mkdir -p -- "$DEST/releases"
if [[ -d "$RELEASE" ]]; then
  echo "$PROG: reusing byte-identical release $RELEASE."
else
  mv -- "$CANDIDATE" "$RELEASE"
  echo "$PROG: staged $RELEASE."
fi
printf '%s  %s\n' "$ACTUAL" "$(basename -- "$ARCHIVE")" >"$RELEASE/digest.sha256"

LINK_TMP="$DEST/.current.new.$$"
ln -s "releases/$ACTUAL" "$LINK_TMP"
# Same directory, so rename(2) is atomic: readers see the old or the new
# release, never a half switch. Old releases are kept for rollback.
mv -T -- "$LINK_TMP" "$DEST/current"
LINK_TMP=""
echo "$PROG: activated: current -> releases/$ACTUAL (sha256 $ACTUAL)."
