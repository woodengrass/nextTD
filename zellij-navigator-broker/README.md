# Navigator broker (Zellij 0.45.1 WASM plugin, atomic unit 1)

Persistent, per-client open/back broker for the header tab. This is the first
atomic unit of the user-approved persistent navigator: version-pinned,
reproducibly buildable source plus deterministic state tests. The Python
side (`src/tdnext/plugins/desktop/navigation.py`) is untouched: it keeps
the legacy tab-name route and additionally speaks the broker pipes only
when the native launcher sets the broker env for a warmed session. The
launcher preloads exactly one broker pane per new native session with the
primary header, keeps the Python `Alt+o` binding as the initial owner,
and warms the env only on cached prior grants.

Built on the working prototype in `/tmp/opencode/zj-direct-nav-20261111`,
with these deliberate changes:

- Header tab name comes from plugin **configuration** (`header_id`), not a
  hardcoded fixture. Layout properties arrive KDL-quoted, so one layer of
  surrounding quotes (and whitespace) is stripped; empties mean unconfigured.
  The future Python integration will pass `tab_name(plugin_id)`
  (`tdnext-header:<plugin_id>`); destination matching mirrors
  `navigation.matches_tab` (exact name or `name@...` suffix).
- Origins are **scoped per client** and each client runs its **own plugin
  instance** with its own origin map, so a broadcast CLI pipe reaches every
  instance and exactly the selected target responds and acts; all other
  instances stay silent (no output, no mutation).
- The first field of `get_focused_pane_info` is the **stable tab id**
  (server `active_tab_ids`), despite the `tab_index` field name. It is used
  directly; it is never mapped through tab positions.
- **Escape is never bound.** `Alt+o` is bound at runtime to this plugin id
  (`MessagePluginId <id>`, message `navigate`) across
  `shared_except "locked"` modes via `reconfigure(..., false)` (runtime-only,
  nothing written to disk), mirroring the application's `zellij.kdl` scope.
  Return is served only through the `back` CLI pipe, which rechecks the live
  focused pane before acting.
- `READY` is claimed only after host-sent keybinds **contain our binding**
  (Alt+o in Normal mode), never on event ordering alone. A stale pre-request
  keybinds event is ignored.
- Return uses a **single** `focus_pane_with_id` host call by stable pane id;
  the host switches to the pane's tab as part of focusing, so no separate
  tab switch with a potentially stale position index is needed.

## Pipe wire format

- `navigate` (keybinding `Alt+o`): no payload required. Records the calling
  client's origin (focused tab id + pane) and switches to the destination.
  Repeated opens are safe: already on the destination is a no-op
  (`ALREADY THERE`); a keyboard open from outside the destination records
  the latest origin, replacing any stale previous one. A broadcast CLI
  `navigate` opens for every client instance, each on its own client.
- `forget` (CLI pipe, e.g. `zellij action pipe --name forget --
  "client=1"`; `--args` override payload pairs): requires an explicit
  `client` id. Contract: **uniquely claimed client**, as with `back` — an
  instance serves only when the claimed client is its own acting client;
  every other instance stays silent, so a broadcast yields exactly one
  response from the selected target. The serving instance drops only the
  selected client's recorded origin (no-op when none is recorded, so
  repeated forgets succeed) and answers exactly
  `ok:FORGOT CLIENT=<id>`. A message with no usable client id names no
  target and is silently ignored. This is the hybrid-handoff acknowledgment:
  the mouse legacy path proceeds only after the broker confirms the stale
  keyboard origin is cleared. Like `back`, `forget` has no keybinding
  (Escape stays unbound); it is CLI-pipe only.
- `back` (CLI pipe, e.g. `zellij action pipe --name back --
  "client=1 source=terminal_3"`; `--args` override payload pairs): requires
  `source` (`terminal_<id>` / `plugin_<id>`) and, for CLI sources, an
  explicit `client`. Contract: **uniquely claimed client**. An instance serves
  a message only when the claimed client is its own acting client; every
  other instance stays silent, so a broadcast yields exactly one response
  from the selected target and no wrong client mutates. A message with no
  payload and no args is the end-of-pipe marker and is silently ignored
  (answering it would duplicate responses: a piped CLI invocation sends one
  such marker after its payload lines).

Back validation order (first failure wins; the serving instance reports it
as the status line **and** as exactly one scoped CLI response):

1. Claim parses; empty marker stays silent.
2. Claimed client equals this instance's acting client, else silent
   (`ActingClientMismatch` / `NotSelected` never emit output).
3. Claimed client owns an origin (`NO ORIGIN FOR CLIENT=n`; this is the
   client-mismatch rejection against recorded state).
4. Destination tab still exists (`DESTINATION GONE`).
5. Live focused pane equals the claimed source (`SOURCE MISMATCH`).
6. Focused tab resolves and is the destination (`FOCUS TAB UNKNOWN`,
   `NOT ON DESTINATION`).
7. Origin tab and pane still exist (`ORIGIN GONE`, `ORIGIN PANE GONE`;
    stale origins are kept: a rejected back never consumes the recorded
    origin, so the caller sees a repeated rejection on retry — never a
    turned-into absence — until a successful return or an explicit
    `forget` clears it).

Success reports `RETURN client=n pane=...` and focuses the origin pane by
stable id (single host call). Every CLI-source outcome, ok or err, is
returned as exactly one `ok:<detail>` / `err:<detail>` line on the pipe
plus unblock, so the CLI caller exits with the response and no duplicates.

## Statuses (rendered, grep-stable)

`BINDING client=n header=...` (requested, unconfirmed),
`READY client=n header=...`, `READY client=n NO HEADER`, `DENIED`,
`NOT READY` (navigate arrived before a live grant; no host call
is issued and nothing is recorded),
`NO HEADER CONFIGURED`, `FOCUS QUERY FAILED`, `FOCUS TAB UNKNOWN`,
`MISSING DESTINATION`, `DESTINATION POSITION INVALID`,
`OPEN client=n tab=n pane=...`, `ALREADY THERE client=n`,
`NO ORIGIN FOR CLIENT=n`,
`DESTINATION GONE`, `SOURCE MISMATCH expected=.. focused=..`,
`NOT ON DESTINATION`, `ORIGIN GONE tab=n`, `ORIGIN PANE GONE pane=..`,
`BAD BACK CLAIM`, `RETURN client=n pane=...`,
`FORGOT CLIENT=n`.

## Build (offline, pinned)

From the project root, with Rust 1.95, the `wasm32-wasip1` target, and the
locked dependencies available locally:

```sh
cargo build --manifest-path zellij-navigator-broker/Cargo.toml --offline --locked --target wasm32-wasip1
```

The tracked `Cargo.lock` pins the dependency versions. Host-side tests also
require the native build prerequisites for the current machine:

```sh
cargo test --manifest-path zellij-navigator-broker/Cargo.toml --offline --locked
```

`zellij-tile` is pinned to exactly `0.45.1`.

## Known limits (not claimed otherwise)

- **Cold ungranted pipes are not delivered.** While ungranted the host
  caches pipe events instead of answering, so the broker emits no
  response, issues no host call, and records nothing. On a cold
  grant/deny the broker closes itself and the session continues on the
  legacy route. This is not readiness: the legacy Python route stays
  authoritative until the launcher observes cached prior grants and sets
  the broker env.
- **Check/action interval is non-atomic.** Validation queries
  (`get_focused_pane_info`, `get_pane_info`, cached `TabUpdate`) and the
  subsequent host calls are separate round-trips; tabs or panes can change
  in between. The broker rechecks immediately before acting and fails
  closed, but it cannot promise atomic focus.
- The tab list is a `TabUpdate` cache and can be stale between events.
  The runtime rebind is therefore deferred until a `TabUpdate` inventory
  actually resolves the configured header to a tab (merely non-empty is
  not enough); until then `Alt+o` stays with the legacy route (fallback
  functional until the broker is genuinely ready). Every navigate then
  resolves through the stable tab id verified by a live `get_tab_info`
  probe (name still matches, suffix form included) and switches by the
  probe's live 1-based position — fresh across reorders no event has
  reported yet. The last known destination id is kept and re-verified
  when the cache cannot resolve the header. An unverifiable destination
  (closed or renamed-away tab) fails visibly as `MISSING DESTINATION`
  with no mutation and no navigation call; success or mutation without a
  verified id is impossible by construction. No name-based switch is
  issued anywhere: the server matches `go_to_tab_name` by exact name only
  (`zellij-server/src/screen.rs::switch_active_tab_name`), so a
  bare-header switch would silently no-op on a suffixed or renamed tab
  after reporting success. The host offers no synchronous list-tabs call
  (only per-tab `get_tab_info`), so the full inventory can only come
  from `TabUpdate`.
- CLI pipes carry no authenticated client; the claim is trusted and safety
  comes from the per-instance self-filter, the live focused-pane recheck,
  and per-client ownership. A wrong claim fails closed (silence, or an
  explicit `err:` from the selected target) rather than mis-navigating.
- The plugin API drops `KeybindPipe` fields (name, plugin id) when
  reporting keybinds back to plugins, so READY-confirmation checks the
  Alt+o key in Normal mode rather than the full action identity. Stock
  never binds Alt+o in Normal mode, so the signal is unambiguous in a
  controlled config.
- The host does not reliably deliver a post-reconfigure keybinds event, so
  READY may stay at BINDING even while the binding works (M-o function is
  the operative proof; see session evidence below). This is a host gap, not
  a broker guess: READY is never claimed without the bind in the payload.
## Activation contract (`active` config)

The layout passes `active "true"` only for warmed sessions: the launcher
sets it exactly when the permission cache already held a prior explicit
grant for this binary, and sets the broker env on the same condition.
Anything else (`"false"`, missing, malformed) is a cold session.

- Cold: the plugin still loads and requests permissions (the prompt is the
  reachable 12-row pane, selectable until grant/deny), but a cold
  grant/deny closes the broker pane itself with no rendered status and
  never rebinds Alt+o, and the whole session keeps the legacy route. The
  Python `Alt+o` binding from the static config stays the owner throughout.
- Warm: on grant the broker runtime-rebinds Alt+o (overlaying only that
  key; every other binding is preserved), claims READY only after the host
  keybinds confirm it, and serves navigate/back/forget for its client.

- Stock Zellij floating-pane focus is shared among clients in a tab; the
  broker does not change that.
- Timing samples (isolated sessions, `zellij action` wall time, n=20):
  `query-tab-names` min 5ms / med 5.0ms / max 6ms / mean 5.4ms; broker
  back-pipe (granted) min 6ms / med 6.0ms / max 7ms / mean 6.0ms. No stress
  test was performed for this unit.
- Warmed regression (private isolated session, 2026-11-12, verified-id
  broker 0.1.1, `live2.py`): genuine first `Alt+o` with no priming
  navigated in 0.566s; suffix rename (`tdnext-header:settings@1:terminal_9`,
  hybrid mouse form) open 0.015s + back ok; `move-tab right` reorder open
  0.070s + back ok (live probe position); rename to unrelated `Other`
  then `Alt+o` stayed on Files (no blind switch, no false success);
  rename back recovered in 0.015s + back ok; 3-press repeat 0.181s /
  0.236s / 0.233s. No shortcut changed; no user/VM sessions touched.

## Session evidence (private isolated sessions, 2026-11-11)

- Prototype control (`direct-navigator-proof.wasm`, no config): M-o flips
  status to `MISSING DESTINATION` — key delivery and runtime reconfigure
  work in this harness.
- Broker, single client, tab ids skewed from positions (Files id=1 pos=0,
  Scratch id=2 pos=1, dest id=3 pos=2): M-o records `OPEN client=1 tab=1
  pane=terminal_1` — stable id, not position. `list-clients` shows client 1
  on the dest shell. `Esc` on the destination changes nothing.
- `back` via CLI pipe: exactly one stdout line per call, exit 0 —
  `ok:RETURN client=1 pane=terminal_1` (client home on origin shell),
  `err:SOURCE MISMATCH expected=terminal_9 focused=terminal_3` (no move),
  `err:NO ORIGIN FOR CLIENT=2` after the origin is consumed.
- Claim for nonexistent client 9: empty stdout, no state change; the
  subsequent valid back still succeeds (no wrong-client mutation).
- Two clients: each M-o records its own origin; client 1 unaffected by
  client 2's open/back and vice versa; per-instance renders show each
  client's own result (`RETURN client=1 ...` / `NO ORIGIN FOR CLIENT=2`).
- Source-only (implicit) back with a single owning instance: exactly one
  `ok:` response; the non-owning instance stays silent.
- Content-gated READY observed (`READY client=1 header=tdnext-header:shell`)
  after the host delivered confirming keybinds; sessions where the host
  does not deliver stay at BINDING while M-o demonstrably works.
