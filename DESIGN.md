# Terminal Desktop Next: design contract

## 0. Research log

- The existing `/tmp/opencode/terminal-desktop-build/DESIGN.md`, layout,
  Textual theme, launcher and app components establish a keyboard-first,
  terminal-native workbench, a compact scale and an 80 x 24 baseline. This
  is a separate implementation, not a change to that prototype.
- Reference shortlist: Apple for Finder-like hierarchy, Warp for a terminal
  workbench, and Notion for a restrained sidebar. Selected Apple plus the
  operational taste reference. Adapted its quiet neutral surfaces and clear
  action hierarchy to terminal cells; do not imitate Apple branding.
- Spatial references: StyleGallery `fixed-sidenav-shell` and `list-detail`.
  The sidebar and footer are fixed; the file list is the primary scroll owner.
- For the global header, StyleGallery's `scroll-body-shell` pattern
  (https://github.com/changeroa/StyleGallery/blob/main/patterns/viewport-shell/scroll-body-shell.md)
  keeps the header and footer fixed while the active content owns its scroll.
- Interaction reference: beui.dev `shared-layout-bg`: selection follows
  keyboard focus as well as the pointer. In a terminal this is an immediate
  row wash plus a text marker, not a spring or animated pill.
- Appearance selectors follow beui.dev `select`'s labeled single-selection
  and keyboard-focus model, adapted to Textual's immediate, non-animated
  controls. Settings section navigation adapts beui.dev `tabs`: both choices
  remain labeled and keyboard-reachable, and the active choice has a text
  marker. StyleGallery's `tab-strip`
  (https://github.com/changeroa/StyleGallery/blob/main/patterns/in-line-grouping/tab-strip.md)
  keeps the two sections in a fixed row without its own scroll container.
- Plugin confirmations adapt StyleGallery's `cluster`
  (https://github.com/changeroa/StyleGallery/blob/main/patterns/in-line-grouping/cluster.md):
  the action group has no independent scroll and changes to a vertical stack
  when both complete button hit areas cannot fit on one row.
- Files redesign: the supplied Finder/Yazi/Superfile-style reference is a
  directional contract, not a pixel target. A breadcrumb, parent-and-sibling
  directories, a Name/Size/Modified list, and real numbered text preview sit
  inside the existing desktop shell. StyleGallery's
  `patterns/split-sidebar/list-detail.md` supplies the adaptive list/detail
  relationship; the two scrollable panes below intentionally own their scroll.
- Lazyweb and image generation: no product screenshots or image drafts were
  used. Neither is a terminal design reference; an image-generation tool is
  not available in this environment. The actual TUI is the visual source.

## 1. Atmosphere and identity

A quiet, practical desktop inside the user's terminal. The recognizable
feature is the stable source rail and the changing content area, not a logo,
background effect, or simulated desktop. Files and their paths are real.

## 2. Color

Use Textual's semantic terminal theme roles, not component-specific colors:
`$background` for the canvas, `$surface` for panels, `$boost` for hover and
selection, `$text` for content, `$text-muted` for secondary information,
`$primary` for genuine actions, and `$error` / `$warning` for failures.
Each theme supplies readable light and dark values. Selection also uses a
text marker or focus cursor. The user's terminal controls rendering and font.

## 3. Typography

One terminal monospace font at the user's configured size. Titles are bold;
body and metadata have the same cell size. Sidebar labels are visible when expanded.
Compatibility mode uses plain terminal text; the optional, manually selected
Codicons mode uses single-cell private-use glyphs and requires a compatible
Nerd Font on the local terminal computer. Never infer font availability. Paths
and filenames are rendered as plain text.

## 4. Spacing and layout

The base unit is one terminal cell. Compact spacing is 0-1 cells; standard
horizontal insets are 1-2 cells. The source rail is 22 columns in normal
layouts and contracts to an icon rail on request; main content flexes. A one-row desktop-owned global header spans the
entire terminal above both rail and workspace, and the two-row contextual
footer stays fixed. Native Zellij reserves a second physical row as the pane
separator so that the header retains one usable content row. The file table and the Settings plugin list each own their
vertical scroll; the shell itself does not scroll. Detail and
preview content may scroll independently when present. 80 x 24 is the target;
at smaller sizes preserve keyboard access and prioritize the file list.

Layout: Desktop rail - preserve navigation while reclaiming workspace width -> fixed-sidenav-shell (https://github.com/changeroa/StyleGallery/blob/main/patterns/viewport-shell/fixed-sidenav-shell.md); scroll owner: destination list; avoid scrolling the entire shell or replacing the active workspace.

## 5. Primitives and states

- **Global header:** desktop title at the leading edge, enabled `header`-placed
  plugin actions at the trailing edge. Each action is a labeled, keyboard- and
  mouse-operable control; its shortcut is visible. No Settings destination in
  the source rail. Disabled or absent plugins have no action.
- **Source rail:** title, category labels, focusable destinations. Default,
  focused, selected and unavailable states have distinct labels or markers.
  A short monochrome icon precedes each destination label. Views declare a
  semantic icon key, resolved by a replaceable provider in the selected mode.
  If no enabled provider is selected, the legacy manifest's plain-text icon
  or `[]` is used; icon text is safe text. The rail can collapse to individual
  icons and expand again with a visible control or F7 (in native mode, F7 is
  available while the sidebar pane has focus). Collapsed destinations
  retain a selection marker, distinct fallbacks and discoverable names; expanding
  restores all labels without replacing the workspace.
- **Shell workspace:** the central region is a real interactive terminal,
  bounded by the existing desktop chrome. Its own terminal viewport and
  scrollback own shell output. An exited or unavailable shell is explained in
  place without replacing the desktop or silently opening a fullscreen view.
- **Content header:** view title, current path and compact status, clipped or
  wrapped by the toolkit when space is constrained.
- **File list:** focusable, scrollable rows, directories before files, clear
  empty and error messages. Selection is a neutral highlight plus cursor.
- **Files browser:** one fixed breadcrumb row above a bounded pane group. At
  96 or more available content cells show parent/sibling folders, the file
  table, and preview side by side; from 58 through 95 show the table and
  preview; below 58 show the table or preview, switchable with `v`. The
  thresholds measure the Files view, not the entire terminal, because native
  and fallback hosts already own their outer rails. The folder list marks the
  current folder with `>` and provides an explicit parent. At filesystem root
  it represents `/` rather than inventing a parent. The file table remains
  the default focus and its scroll owner; folder and preview scrolling are
  independent. A static, `$foreground-muted` single-cell horizontal rule under each pane
  heading separates its content; focus and selection never recolor the rule.
  The Name column fills the table viewport after padding and scrollbar space;
  Size and Modified use 9 and 16 cells when shown. Large sizes use compact
  binary units to keep the unit visible; the preview retains the exact byte count.
  Compact table columns may omit secondary metadata when it cannot fit; selected
  metadata remains available in preview. A background worker scans the full
  directory without blocking input, then publishes the rows together. A newer
  navigation invalidates results from an older scan.
- **File preview:** show the selected name, type, size and modified time above
  independently scrollable, numbered text lines. Show an inline loading,
  empty, binary, special-file, symlink, unreadable or truncation explanation
  when appropriate. A preview never follows a symlink: show its target instead.
  Bound file reads to 64 KiB, 300 lines and 512 characters per rendered line;
  selection changes replace pending work and stale results never repaint.
  Directory scans and preview reads run away from the UI event loop with at
  most one active and one latest pending request per kind.
- **Context footer:** two fixed, single-line rows: `Tool (name)` identifies
  the active plugin and its highest-priority keys; `Global` identifies desktop
  keys available in the current runtime. The native row advertises only enabled
  header and numbered sidebar actions plus pane mode and quit; the fallback
  advertises focus, theme, quit, and the header action only when available.
  At 80 x 24 keep both labels and priority keys visible, truncating trailing
  hints with an ellipsis rather than wrapping. The Monitor fallback describes
  launching btop, whose full-screen keys apply only after it takes over.
  Shortcuts are not the only indication of an error.
- **Dialog:** labeled input or an explicit destructive confirmation, Escape
  cancels, destructive action is not the initial focus. If Delete is requested
  while the current folder is still loading, show a cancellable loading state;
  keep Delete disabled until the current, named selection is available. Never
  confirm a selection from an obsolete folder or delete without confirmation.
  Plugin confirmations retain Cancel as the initial focus and display Cancel
  before the destructive action. Their buttons sit side by side when both fit;
  otherwise they stack without clipping or scrolling, including at 35 x 24.
  Each whole button is a mouse target; its fixed neutral frame never changes
  with focus or hover, while the destructive action retains its error surface.
  The title and complete dependency explanation occupy a bounded message
  viewport: long content scrolls by keyboard or wheel, with a visible focus
  treatment and scrollbar, while both actions stay fully visible. Escape still
  cancels even while the message has focus.
- **Settings sections:** the fixed Settings heading and Back action precede a
  row of individually framed, focusable General / Plugins controls. General opens first and
  contains separate Icon appearance and File editor groups; Plugins contains the plugin list and
  its actions alone. `>` marks the active section, independent of color. Tab and
  Shift+Tab reach section choices and controls; activating a choice moves focus
  into its content. The plugin list owns its own scroll; section navigation,
  Back, and feedback stay fixed. General is the sole vertical scroll owner for
  its groups: headings, controls, and help move together inside the bounded
  body. Feedback grows to four rows at most and owns
  a separate, keyboard- and wheel-scrollable region for longer errors. The
  one-cell solid `$foreground-muted` frame is functional, fixed, and identical
  in default, hover, focus, pressed, selected, and disabled states: it never
  follows focus or changes the desktop pane separators. Focus uses a background
  wash and bold text; selection also has the `>` marker. Every cell inside a
  section control, including its frame, activates that control. Use the one-cell
  spacing scale and existing semantic theme roles; no decorative motion.
  Back uses the same fixed neutral frame and focus/hover wash; its complete
  frame remains a mouse target and returns to the originating workspace.
- **Plugin manager:** a neutrally framed scrollable list of discovered plugin names and their
  `Enabled`, `Installed`, or `Available` state. A focused row identifies the
  target; labeled Install, Enable, Disable, and Uninstall actions invoke real
  plugin lifecycle operations. Selection is a neutral wash, not color alone.
  The list, target detail, framed action row, and bounded feedback are visually
  grouped in that order. Every action frame is part of its click target; disabled
  actions do not respond. The list owns vertical scrolling; if the action row
  cannot fit in the available pane, it owns horizontal overflow and keyboard
  focus reveals its controls rather than clipping them. An installed ID with a missing manifest remains visible as missing; Disable
  and Uninstall provide recovery without suggesting it can be installed.
  Dependency cascades and errors are explained in text; uninstall asks for
  confirmation. An available Uninstall action uses bold `$error` text without
  recoloring its border; disabled actions retain the normal disabled treatment.
  While an operation runs, show progress and prevent concurrent
  operations. A persistent notice explains that changes take effect after a
  desktop restart. The running desktop and Settings cannot be disabled or
  uninstalled from Settings, including via a dependent cascade.
- **Appearance controls:** provider and icon-style selectors in General each
  occupy a three-row, neutral framed control with their label and value on the
  middle row. The label, value, arrow, and frame activate the same selector;
  focus is a visible wash, never a recolored frame. At insufficient pane width,
  the controls stack and the General content scrolls vertically if needed.
  Choosing an option by mouse, including the already-selected option, closes
  its menu and returns focus to that selector; Tab continues in reading order.
  Compatibility is the default; Codicons is
  an explicit choice, never a font-detection result. Selection persists in an
  atomic appearance file and shows restart feedback. If a provider is absent
  or disabled, text navigation still works. The current session keeps its
  original icons and views even when Settings changes later choices.
  Reopening Settings refreshes both selectors from saved appearance, and an
  edit preserves the other selector's latest saved value. Native tab focus
  refreshes only changed selector data and does not steal focus or poll, when
  the terminal and multiplexer deliver focus reports.
- **General setting groups:** place Icon appearance first, retaining the
  provider and style IDs and initial provider focus, followed by File editor.
  Each group has a bold heading, its related framed controls and short help;
  one blank terminal row separates groups so further settings can be added.
  At narrow widths appearance controls stack; at 35 x 24, 80 x 24 and 120 x 36
  every setting remains reachable, with focus revealing its complete frame in
  the General scroll body. Retain the existing Tab path from style to the fixed
  feedback region; Tab then reaches the editor, and Shift+Tab reverses this
  route. Each whole editor frame, including label and border, opens the same
  selector; focus uses the established wash and bold text, not a colored frame.
  Menus remain keyboard- and mouse-operable, and choosing the current value
  closes the menu without rewriting the preference.
- **Expanded editor menu:** expand the editor's neutral frame in the General
  scroll body and place its three options on separate rows below the current
  value. The complete frame and choices scroll into view when opened; help
  follows the frame in normal flow, while the fixed feedback region remains
  unobscured. No option shares a row with a clipped border or unrelated text.
  Closing the menu restores the compact frame and leaves help and feedback
  available. The icon appearance selectors retain their existing behavior.
- **File editor:** choices are Environment, Neovim (`nvim`), and Vi (`vi`).
  Environment uses `$EDITOR` and falls back to `vi` when unset. A saved choice
  applies on the next file open, without restarting or modifying the current
  editor; no editor is installed or configured automatically. If a selected
  executable is unavailable, report that when opening a file. The help and
  save feedback distinguish this immediate-next-open behavior from the
   appearance restart requirement. Re-entry, native activation, and selector
   focus refresh saved values without writing or stealing focus; an error saving
   is explained and the control returns to the actual saved selection.
  If the preference cannot be read, preserve the file unchanged and show no
  selected editor: disable the selector and explain the failure, the path,
  and how to repair it in General's fixed, scrollable feedback. Files and Back
  remain usable. Re-entry, native activation, or focus on another General
  selector retries the read and restores the saved choice after external repair;
  these retries never write a preference.

## 6. Motion and interaction

No decorative animation. Keyboard and mouse operate the same destinations.
Arrow keys navigate, Enter opens, Backspace goes to the parent; actions show
immediate text feedback. Delete requires a separate confirmation. External
full-screen programs, when used, suspend and return to the desktop.
In Files, `h`/`l` also navigate back/open and `j`/`k` move the file cursor;
`v` focuses the preview (and switches panes in narrow mode), and `b` focuses
the folder list when wide. Tab and Shift+Tab leave the Files view for the
desktop's existing focus sequence; a standalone Files pane uses Tab locally.
The folder list supports pointer and keyboard activation; preview scrolling
supports keyboard and mouse wheel, with horizontal scrolling for long lines.
File actions do not target a separate table selection while the folder list is
focused; explain that the user must focus the file list first. Resizing keeps
focus on a visible Files pane when the previously focused pane is hidden.
Settings is a separate view compatible with the selected host interface. Opening it retains
the original native Zellij tab; Back / Escape returns
to that exact view and its contextual footer. Plugin operation shortcuts are
available only in the Plugins section, never while using General. Native navigation records the
originating stable tab ID per session, never the positional previous tab. A
native shell tracks the originating client's stable tab and focused pane IDs.
The custom Zellij core stores these IDs in its session and restores focus after
a header click. Independent sessions never share return state. Slow installs
run outside the UI event loop.

Historically, the fallback retained the mounted view, encoded return IDs in
the header tab's name, and yielded the full terminal to btop until it exited.
Alt+o and Ctrl+q took priority over focused-plugin bindings; Alt+o did
nothing in a modal, Escape stayed modal-local, and Ctrl+q exited even from
a modal. F6 focused the sidebar, including from Shell; Tab and ordinary
editing keys remained available to the interactive shell. Native
shortcuts are intercepted by Zellij outside locked mode;
they cannot inspect a plugin's modal state. Unassigned numeric shortcuts never
open a header tab.

## 7. Depth and surface

Tonal shifts and the terminal's own text styles distinguish the rail,
content and dialogs. Settings uses fixed neutral outlines only for interactive
hit areas and the plugin list, never as focus-driven pane separators. No colored side borders or decorative shadows. Sidebar
glyphs are monochrome and supplement text labels in the expanded rail.
Components follow Textual's terminal-native primitives.

## 8. Accessibility constraints and known limitations

Keyboard operation is primary, mouse is additive, and labels do not depend on
color. Keep useful text readable at 80 x 24. Terminals cannot reliably report
system light/dark preferences or provide browser accessibility semantics.
Some external terminal programs have their own size and contrast limits. This
is a first-version terminal application, not a system file manager or sandbox.
Appearance and plugin changes are not hot-loaded into an already-running shell;
restart is required. Editor preferences instead apply on the next file open.
Font availability and private-use glyph width cannot be
reliably detected; Compatibility is the safe manual alternative. Plugin
lifecycle writes from the CLI and Settings share one mutation lock; a
contending operation reports that the state is locked.
Native re-entry refresh depends on terminal focus reporting; if focus reports
are unavailable, focusing a General selector refreshes the saved values.
