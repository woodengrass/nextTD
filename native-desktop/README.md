# Standalone desktop

`tdnext-desktop` is the native launcher. It runs beside its siblings and
needs no Python. Place the patched core next to it as `tdnext-core`, or use
`--core /path/to/patched/core`. Run
`tdnext-desktop [--core PATH] [--config PATH] [DIRECTORY]` in a terminal.
Direct-config startup with `--config` works on its own and does not go
through `tdnext desktop`.

Without `--config`, the launcher reads `desktop.json` beside its executable.
If that file is absent, it opens `$SHELL`, or `/bin/sh` if unset.
An explicit config containing `{"views":[]}`, or only disabled views, opens
an empty desktop instead; no shell or content program is started. Press
Ctrl+q to exit. The desktop cannot be started inside an existing Zellij
session. Changes to the configuration and tool state apply after restarting
the desktop.

The optional JSON configuration lists independent programs, not shell strings:

```json
{"views":[
  {"id":"shell", "name":"Shell", "icon":">_", "placement":"sidebar", "command":"/bin/sh", "args":[]},
  {"id":"monitor", "name":"Monitor", "icon":"M", "placement":"sidebar", "command":"btop", "args":[], "enabled":true},
  {"id":"settings", "name":"Settings", "icon":"S", "placement":"header", "command":"/path/to/tdnext-settings", "args":[], "shortcuts":"Esc back"}
]}
```

Files is the Rust program `tdnext-files`: use its sibling path as the view's
`command`, or install it on `PATH` as `"command":"tdnext-files"`. Shell uses
`$SHELL` (or `/bin/sh` when unset); Monitor runs `btop`. Migrating a
hand-written view means replacing the old Python `-m tdnext.pane view ...`
entrypoint with the sibling executable and its arguments. Migrated Python
plugins do not run unmodified.

`enabled` defaults to true; `icon`, `shortcuts`, and `args` are optional.
View names and ids must be unique. Sidebar views are ordered before headers;
Alt+1 through Alt+9 select sidebar views, Alt+o opens the first header,
and F7 toggles the native sidebar. A header program can return using
`$TDNEXT_ZELLIJ action return-to-origin`; `TDNEXT_DIRECTORY` is the initial
directory. Programs inherit the working directory and their arguments exactly;
no shell is used to interpret configured commands. Tool state resolves
through the existing `TDNEXT_STATE_DIR`; the launcher never converts or
overwrites legacy configuration or state. This core supports one
attached client per desktop session.

Tool-managed downloads require your confirmation before anything is installed.
