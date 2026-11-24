#!/usr/bin/env bash
set -Eeuo pipefail

dist="$(realpath "${1:?usage: bash scripts/smoke-dist.sh <distribution>}")"
tmp="$(mktemp -d -t tdnext-native-smoke-XXXXXX)"
socket="$tmp/tmux.sock"
cleanup() {
  if test -S "$socket"; then tmux -S "$socket" kill-server; fi
  rm -rf -- "$tmp"
}
trap cleanup EXIT
trap 'result=$?; echo "smoke-dist: failed at line $LINENO (exit $result)" >&2; if test -S "$socket"; then tmux -S "$socket" capture-pane -p -t smoke >&2; fi' ERR
mkdir "$tmp/home" "$tmp/state" "$tmp/work" "$tmp/bin"
ln -s /bin/sh "$tmp/bin/sh"
touch "$tmp/work/package-smoke-marker"
for binary in tdnext tdnext-settings tdnext-desktop tdnext-files tdnext-core; do
  test -x "$dist/$binary" || { echo "smoke-dist: missing executable $binary" >&2; exit 1; }
done
test -s "$dist/LICENSE.zellij-0.45.1"
if compgen -G "$dist/*.whl" >/dev/null; then
  echo 'smoke-dist: wheel present in native distribution' >&2
  exit 1
fi
runtime_path="$dist:$tmp/bin"
cd "$tmp/work"
runtime=(env -i HOME="$tmp/home" XDG_CONFIG_HOME="$tmp/home/config" XDG_DATA_HOME="$tmp/home/data"
  TDNEXT_STATE_DIR="$tmp/state" PATH="$runtime_path" SHELL=/bin/sh TERM=xterm-256color)
"${runtime[@]}" "$dist/tdnext" enable files
"${runtime[@]}" "$dist/tdnext" enable settings
"${runtime[@]}" "$dist/tdnext" install icons
"${runtime[@]}" "$dist/tdnext" list | grep -E '^icons[[:space:]]+installed'
test "$(env -i HOME="$tmp/home" PATH="$runtime_path" "$dist/tdnext-core" --tdnext-chrome-version)" = 1
env -i HOME="$tmp/home" PATH="$runtime_path" "$dist/tdnext-desktop" --help >/dev/null
if env -i HOME="$tmp/home" PATH="$runtime_path" "$dist/tdnext-desktop" --core /bin/true "$tmp/work" 2>"$tmp/reject"; then
  echo 'smoke-dist: desktop accepted an incompatible core' >&2
  exit 1
fi
grep -q 'not a compatible tdnext chrome' "$tmp/reject"
tmux -S "$socket" -f /dev/null new-session -d -s smoke -x 120 -y 36 \
  /bin/sh -c 'socket=$1; status_file=$2; shift 2; tmux -S "$socket" wait-for start; "$@"; status=$?; printf "%s\n" "$status" > "$status_file"; tmux -S "$socket" wait-for -S stopped' sh "$socket" "$tmp/status" \
  "${runtime[@]}" "$dist/tdnext" desktop "$tmp/work"
tmux -S "$socket" set-option -s exit-empty off

# Subscribe before each input; the byte stream does not require terminal newlines.
watch_output() {
  local needle="$1" signal="$2"
  tmux -S "$socket" pipe-pane -O -t smoke \
    "SOCKET='$socket' NEEDLE='$needle' SIGNAL='$signal' /usr/bin/perl -e '\$|=1; while (sysread(STDIN, \$c, 1)) { \$s .= \$c; if (index(\$s, \$ENV{NEEDLE}) >= 0) { system(\"tmux\", \"-S\", \$ENV{SOCKET}, \"wait-for\", \"-S\", \$ENV{SIGNAL}); exit 0 } }'"
}
watch_output package-smoke-marker files-ready
tmux -S "$socket" wait-for -S start
timeout 30 tmux -S "$socket" wait-for files-ready
tmux -S "$socket" capture-pane -p -t smoke | grep -F package-smoke-marker
watch_output 'Provider:' settings-ready
tmux -S "$socket" send-keys -t smoke M-o
timeout 30 tmux -S "$socket" wait-for settings-ready
tmux -S "$socket" capture-pane -p -t smoke | grep -F 'Provider:'
watch_output 'Codicons' icons-selected
tmux -S "$socket" send-keys -t smoke BTab Enter Down Down
timeout 30 tmux -S "$socket" wait-for icons-selected
tmux -S "$socket" capture-pane -p -t smoke | grep -E 'Codicons[[:space:]]+Installed'
watch_output 'Restart desktop to apply plugin changes.' icons-enabled
tmux -S "$socket" send-keys -t smoke e
timeout 30 tmux -S "$socket" wait-for icons-enabled
"${runtime[@]}" "$dist/tdnext" list | grep -E '^icons[[:space:]]+enabled'
watch_output package-smoke-marker files-returned
tmux -S "$socket" send-keys -t smoke Escape
timeout 30 tmux -S "$socket" wait-for files-returned
tmux -S "$socket" capture-pane -p -t smoke | grep -F package-smoke-marker
tmux -S "$socket" send-keys -t smoke C-q
timeout 30 tmux -S "$socket" wait-for stopped
test "$(<"$tmp/status")" = 0
echo 'smoke-dist: staged CLI, Files and Settings ran without Python on PATH'
