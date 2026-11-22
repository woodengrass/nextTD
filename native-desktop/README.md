# Standalone desktop

Build with `cargo build --locked --release`. Place the patched core next to
`tdnext-desktop` as `tdnext-core`, or use `--core /path/to/patched/core`.
Run `tdnext-desktop [--core PATH] [--config PATH] [DIRECTORY]` in a terminal.
Without `--config`, the launcher reads `desktop.json` beside its executable.
If that file is absent, it opens `$SHELL`, or `/bin/sh` if unset.
An explicit config containing `{"views":[]}`, or only disabled views, opens
an empty desktop instead; no shell or content program is started. Press
Ctrl+q to exit. Enable a view in the config and restart to open a tool.

The optional JSON configuration lists independent programs, not shell strings:

```json
{"views":[
  {"id":"shell", "name":"Shell", "icon":">_", "placement":"sidebar", "command":"/bin/sh", "args":[]},
  {"id":"monitor", "name":"Monitor", "icon":"M", "placement":"sidebar", "command":"btop", "args":[], "enabled":true},
  {"id":"settings", "name":"Settings", "icon":"S", "placement":"header", "command":"/path/to/settings", "args":[], "shortcuts":"Esc back"}
]}
```

`enabled` defaults to true; `icon`, `shortcuts`, and `args` are optional.
View names and ids must be unique. Sidebar views are ordered before headers;
Alt+1 through Alt+9 select sidebar views, Alt+o opens the first header,
and F7 toggles the native sidebar. A header program can return using
`$TDNEXT_ZELLIJ action return-to-origin`; `TDNEXT_DIRECTORY` is the initial
directory. Programs inherit the working directory and their arguments exactly;
no shell is used to interpret configured commands. This core supports one
attached client per desktop session.

The launcher and shell-only desktop do not use Python. Files is the Rust
program `tdnext-files`: use its full path as the view's `command` or install
it on `PATH`. Settings and other Python content packages can be configured
as external programs with their own dependencies.
