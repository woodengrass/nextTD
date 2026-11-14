//! Navigator broker: persistent per-client open/back for the header tab.
//!
//! Each client runs its own plugin instance with its own origin map, so a
//! broadcast CLI pipe reaches every instance and exactly the selected target
//! responds and acts; all other instances stay silent (no output, no
//! mutation). Escape is never bound.

mod state;

use std::collections::BTreeMap;
use zellij_tile::prelude::*;
use zellij_tile::prelude::actions::Action;

use state::{
    BackClaim, BackReject, Binding, ClientId, Focused, ForgetReject, OpenReject,
    PaneRef,
};

/// Configuration key carrying the header tab name, e.g. `tdnext-header:shell`.
const HEADER_ID_KEY: &str = "header_id";
/// Configuration key marking a warmed session. Only a session launched
/// after a prior explicit grant may rebind Alt+o; a cold session that gets
/// granted mid-session keeps the legacy route for its whole lifetime and
/// reports restart-required instead of READY.
const ACTIVE_KEY: &str = "active";

#[derive(Default)]
struct Broker {
    header: Option<String>,
    active: bool,
    origins: BTreeMap<ClientId, OriginState>,
    tabs: Vec<TabInfo>,
    /// Last-known destination (stable tab id plus last-seen 1-based
    /// position). Refreshed from every cache match; the id survives renames
    /// (including the mouse `@suffix` form) and re-verifies by probe when
    /// the cache cannot resolve the header.
    last_dest: Option<(usize, u32)>,
    status: String,
    binding: Binding,
    /// Whether the host granted the requested permissions. Pipe handlers
    /// must not issue gated host calls while this is false.
    granted: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct OriginState {
    tab: usize,
    pane: PaneRef,
}

register_plugin!(Broker);

/// Runtime-only keybinds for this plugin id. `shared_except "locked"` mirrors
/// the application `zellij.kdl`; `save_configuration_file` is always false so
/// nothing is written to disk. Escape is deliberately never bound here: return
/// is served only through the `back` CLI pipe, which rechecks the focused
/// pane before acting.
#[must_use]
fn keybind_kdl(plugin_id: u32) -> String {
    format!(
        "keybinds {{\n shared_except \"locked\" {{\n  bind \"Alt o\" {{\n   MessagePluginId {plugin_id} {{\n    name \"navigate\"\n   }}\n  }}\n }}\n}}\n"
    )
}

fn tab_views(tabs: &[TabInfo]) -> Vec<state::TabView> {
    tabs.iter()
        .map(|tab| state::TabView {
            id: tab.tab_id,
            position: tab.position,
            name: tab.name.clone(),
        })
        .collect()
}

const fn pane_ref_of(pane: PaneId) -> PaneRef {
    match pane {
        PaneId::Terminal(id) => PaneRef::Terminal(id),
        PaneId::Plugin(id) => PaneRef::Plugin(id),
    }
}

const fn pane_id_of(pane: PaneRef) -> PaneId {
    match pane {
        PaneRef::Terminal(id) => PaneId::Terminal(id),
        PaneRef::Plugin(id) => PaneId::Plugin(id),
    }
}

fn pane_exists(pane: PaneRef) -> bool {
    get_pane_info(pane_id_of(pane)).is_some()
}

/// Normalize the configured header id. Layout plugin properties arrive
/// with KDL quoting preserved (e.g. `"tdnext-header:shell"` with literal
/// quotes); child-node configuration arrives bare. Accept both, reject
/// empties.
#[must_use]
fn normalize_header_id(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    let unquoted = trimmed
        .strip_prefix('"')
        .and_then(|rest| rest.strip_suffix('"'))
        .unwrap_or(trimmed);
    let unquoted = unquoted.trim();
    if unquoted.is_empty() {
        None
    } else {
        Some(unquoted.to_string())
    }
}

/// Whether this session launched warmed: the launcher passes
/// `active "true"` only when the permission cache already held a prior
/// explicit grant for this exact binary. Anything else (missing, empty,
/// malformed) means a cold session that must never rebind, even if the
/// user grants mid-session: the legacy route stays authoritative until a
/// warmed restart.
#[must_use]
fn normalize_active(raw: Option<&str>) -> bool {
    matches!(raw.map(str::trim).map(|value| {
        value
            .strip_prefix('"')
            .and_then(|rest| rest.strip_suffix('"'))
            .unwrap_or(value)
            .trim()
    }), Some("true"))
}

/// Whether the host-sent keybinds contain our runtime binding: Alt+o in
/// Normal mode routed to a pipe message. READY is claimed only on this
/// content, never on event ordering: a keybinds event that predates our
/// `reconfigure` carries the old map and must not confirm anything.
/// Normal mode is required because stock binds Alt+o elsewhere (`MoveTab`);
/// stock never binds it in Normal, so its presence there is ours.
/// NOTE: the plugin API drops `KeybindPipe` fields (name, plugin id) in
/// transit, so the check cannot verify the target beyond the action variant.
/// This is a host API limitation, not a shortcut.
#[must_use]
fn keybinds_include_our_bind(keybinds: &KeybindsVec) -> bool {
    keybinds.iter().any(|(mode, keys)| {
        matches!(mode, InputMode::Normal)
            && keys.iter().any(|(key, actions)| {
                matches!(
                    key,
                    KeyWithModifier {
                        bare_key: BareKey::Char('o'),
                        ..
                    }
                ) && key.key_modifiers.iter().any(|modifier| {
                    matches!(modifier, KeyModifier::Alt)
                }) && actions
                    .iter()
                    .any(|action| matches!(action, Action::KeybindPipe { .. }))
            })
    })
}

/// Machine-readable pipe response: `ok:<detail>` or `err:<detail>`, where
/// `<detail>` mirrors the rendered status line.
fn cli_response_line(ok: bool, status: &str) -> String {
    format!("{}:{status}", if ok { "ok" } else { "err" })
}

impl ZellijPlugin for Broker {
    fn load(&mut self, configuration: BTreeMap<String, String>) {
        subscribe(&[
            EventType::PermissionRequestResult,
            EventType::TabUpdate,
            EventType::InitialKeybinds,
        ]);
        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::ChangeApplicationState,
            PermissionType::Reconfigure,
            PermissionType::ReadCliPipes,
        ]);
        self.header = configuration
            .get(HEADER_ID_KEY)
            .and_then(|value| normalize_header_id(value));
        self.active = normalize_active(configuration.get(ACTIVE_KEY).map(String::as_str));
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::PermissionRequestResult(PermissionStatus::Granted) => {
                self.granted = true;
                if !self.active {
                    // Closing the requester also removes its status pane; the
                    // launcher must explain that a restart is required.
                    close_self();
                    return false;
                }
                set_selectable(false);
                self.binding.on_permission_granted();
                self.refresh_last_dest();
                self.binding.on_tabs_updated(self.cache_has_destination());
                // The tab inventory may not have resolved the header yet:
                // the rebind is issued only once a TabUpdate proves the
                // destination id. Until then the legacy shortcut keeps
                // working (see `maybe_try_rebind`).
                self.maybe_try_rebind();
                // The reconfigure call is fire-and-forget: READY is claimed
                // only after the host sends the keybinds back (see below).
                let ids = get_plugin_ids();
                self.status = match &self.header {
                    Some(header) => {
                        format!("BINDING client={} header={header}", ids.client_id)
                    }
                    None => format!("BINDING client={} NO HEADER", ids.client_id),
                };
                true
            }
            Event::PermissionRequestResult(PermissionStatus::Denied) => {
                if !self.active {
                    close_self();
                    return false;
                }
                set_selectable(false);
                self.granted = false;
                self.status = "DENIED".to_string();
                true
            }
            Event::TabUpdate(tabs) => {
                self.tabs = tabs;
                self.refresh_last_dest();
                self.binding.on_tabs_updated(self.cache_has_destination());
                // A warm grant that arrived before the resolving inventory
                // issues its deferred rebind here: the first Alt+o after
                // this point resolves through a verified id instead of
                // dying on an empty cache.
                self.maybe_try_rebind();
                false
            }
            Event::InitialKeybinds(keybinds) => {
                self.binding.on_keybinds_received();
                let ids = get_plugin_ids();
                if self.binding.ready() && keybinds_include_our_bind(&keybinds) {
                    self.status = match &self.header {
                        Some(header) => {
                            format!("READY client={} header={header}", ids.client_id)
                        }
                        None => format!("READY client={} NO HEADER", ids.client_id),
                    };
                    true
                } else {
                    false
                }
            }
            _ => false,
        }
    }

    fn pipe(&mut self, message: PipeMessage) -> bool {
        // End-of-pipe marker: only CLI pipes use an empty message as the
        // terminator (a piped invocation sends one after its payload lines).
        // Keybind and plugin triggers never carry payload/args and must
        // always be served: filtering them here would silently drop every
        // keybinding message.
        if matches!(message.source, PipeSource::Cli(_))
            && message.payload.is_none()
            && message.args.is_empty()
        {
            return false;
        }
        match message.name.as_str() {
            "navigate" => self.on_navigate(),
            "back" => self.on_back(&message),
            "forget" => self.on_forget(&message),
            _ => false,
        }
    }

    fn render(&mut self, _rows: usize, _cols: usize) {
        print!("{}", self.status);
    }
}

impl Broker {
    /// Issue the deferred runtime rebind once the gate holds: a warm grant
    /// plus a `TabUpdate` inventory that resolves the configured header.
    /// Before this point `Alt+o` stays with the legacy route, so an early
    /// press is served there instead of landing here with an unresolvable
    /// destination.
    fn maybe_try_rebind(&mut self) {
        if self.binding.should_rebind() {
            let ids = get_plugin_ids();
            reconfigure(keybind_kdl(ids.plugin_id), false);
            self.binding.on_rebind_issued();
        }
    }

    /// Whether the cache currently resolves the configured header to a tab.
    /// The rebind gate reads this: a merely non-empty cache without the
    /// header must not steal the shortcut.
    fn cache_has_destination(&self) -> bool {
        self.header.as_deref().is_some_and(|header| {
            state::find_destination(&tab_views(&self.tabs), header).is_some()
        })
    }

    /// Refresh the last-known destination from the cache match, if any.
    fn refresh_last_dest(&mut self) {
        if let Some(header) = self.header.clone() {
            let views = tab_views(&self.tabs);
            if let Some((id, position)) = state::find_destination(&views, &header) {
                if let Ok(switch_to_1based) = u32::try_from(position.saturating_add(1)) {
                    self.last_dest = Some((id, switch_to_1based));
                }
            }
        }
    }

    /// Resolve the destination through the stable tab id, verified by a
    /// live probe. Prefers the cache match and falls back to the last
    /// known id; either candidate counts only when a `get_tab_info` probe
    /// finds the tab with a still-matching name (suffix form included).
    /// Returns the id plus the probe's live 1-based position and refreshes
    /// the last-known record. `None` means disappeared or renamed-away:
    /// the caller fails visibly with no mutation and no navigation call.
    ///
    /// Identity is always the stable id, never the name: the server
    /// matches `go_to_tab_name` by exact name only, so a bare-header name
    /// switch would silently no-op on a suffixed or renamed tab after this
    /// decision already reported success. No such call is issued anywhere.
    fn resolve_dest(&mut self, header: &str) -> Option<(usize, u32)> {
        let views = tab_views(&self.tabs);
        let cached = state::find_destination(&views, header).map(|(id, _)| id);
        if let Some(id) = cached {
            if let Some(verified) = self.verify_dest(header, id) {
                return Some(verified);
            }
        }
        match self.last_dest {
            Some((id, _)) if Some(id) != cached => self.verify_dest(header, id),
            _ => None,
        }
    }

    /// Verify one destination candidate by stable id. The probe both proves
    /// existence and supplies the live position (fresh across a reorder no
    /// `TabUpdate` has reported yet); the name must still match the header.
    fn verify_dest(&mut self, header: &str, id: usize) -> Option<(usize, u32)> {
        let info = get_tab_info(id)?;
        if !state::matches_destination(&info.name, header) {
            return None;
        }
        let switch_to_1based = u32::try_from(info.position.saturating_add(1)).ok()?;
        self.last_dest = Some((id, switch_to_1based));
        Some((id, switch_to_1based))
    }

    fn on_navigate(&mut self) -> bool {
        let Some(header) = self.header.clone() else {
            self.status = "NO HEADER CONFIGURED".to_string();
            return true;
        };
        if !(self.granted && self.active) {
            self.status = "NOT READY".to_string();
            return true;
        }
        let client = get_plugin_ids().client_id;
        // The first field is the stable tab id (server `active_tab_ids`);
        // it must not be mapped through tab positions.
        let Ok((focused_tab, focused_pane)) = get_focused_pane_info() else {
            self.status = "FOCUS QUERY FAILED".to_string();
            return true;
        };
        let focused = Focused {
            tab: focused_tab,
            pane: pane_ref_of(focused_pane),
        };
        // The only navigating path: a probe-verified stable id, or a
        // visible failure with no mutation and no navigation call.
        let dest = self.resolve_dest(&header);
        match state::decide_open_resolved(dest, &focused) {
            Ok(state::OpenAction::Open {
                origin,
                switch_to_1based,
            }) => {
                let _ = self.origins.insert(
                    client,
                    OriginState {
                        tab: origin.tab,
                        pane: origin.pane,
                    },
                );
                self.status = format!(
                    "OPEN client={client} tab={} pane={}",
                    origin.tab, origin.pane
                );
                switch_tab_to(switch_to_1based);
            }
            Ok(state::OpenAction::AlreadyThere) => {
                self.status = format!("ALREADY THERE client={client}");
            }
            Err(OpenReject::MissingDestination) => {
                // Disappeared or renamed-away: report, mutate nothing,
                // switch nowhere. The legacy route keeps working.
                self.status = "MISSING DESTINATION".to_string();
            }
        }
        true
    }

    fn on_back(&mut self, message: &PipeMessage) -> bool {
        let Ok((explicit, source)) =
            state::parse_back_claim(&message.args, message.payload.as_deref())
        else {
            // No usable claim material names no target; answering from
            // every instance would duplicate responses, so stay silent.
            return false;
        };
        if !self.granted || explicit.is_none() {
            return false;
        }
        let acting = get_plugin_ids().client_id;
        if state::authorize_back(acting, explicit, false).is_err() {
            return false;
        }
        if !self.active {
            return explicit.is_some_and(|client| {
                let status = state::cold_back_status(client);
                self.respond(message, false, &status)
            });
        }
        let has_own_origin = self.origins.contains_key(&acting);
        let client = match state::authorize_back(acting, explicit, has_own_origin) {
            Ok(client) => client,
            // Not the selected target: stay silent (no output, no unblock,
            // no mutation) so the broadcast yields exactly one response.
            Err(BackReject::ActingClientMismatch { .. } | BackReject::NotSelected) => {
                return false;
            }
            Err(_) => return false,
        };
        let recorded = self.origins.get(&client).copied();
        let Ok((focused_tab, focused_pane)) = get_focused_pane_info() else {
            return self.respond(message, false, "FOCUS QUERY FAILED");
        };
        let Some(header) = self.header.clone() else {
            return self.respond(message, false, "NO HEADER CONFIGURED");
        };
        let Some(source) = source else {
            return self.respond(message, false, "BAD BACK CLAIM");
        };
        let claim = BackClaim { client, source };
        self.back_resolved(
                message,
                &header,
                &claim,
                recorded,
                focused_tab,
                pane_ref_of(focused_pane),
            )
    }

    /// Serve back using live probes, regardless of tab cache freshness.
    fn back_resolved(
        &mut self,
        message: &PipeMessage,
        header: &str,
        claim: &BackClaim,
        recorded: Option<OriginState>,
        focused_tab: usize,
        focused_pane: PaneRef,
    ) -> bool {
        let dest = self.resolve_dest(header).map(|(id, _)| id);
        let inputs = state::ResolvedBackInputs {
            claim: *claim,
            recorded: recorded.map(|origin| state::Origin {
                tab: origin.tab,
                pane: origin.pane,
            }),
            focused: Focused {
                tab: focused_tab,
                pane: focused_pane,
            },
            dest,
            origin_tab_exists: recorded.is_some_and(|origin| get_tab_info(origin.tab).is_some()),
            origin_pane_exists: recorded.is_some_and(|origin| pane_exists(origin.pane)),
        };
        match state::decide_back_resolved(&inputs) {
            Ok(action) => {
                let _ = self.origins.remove(&claim.client);
                focus_pane_with_id(pane_id_of(action.focus), false, false);
                let status = format!("RETURN client={} pane={}", claim.client, action.focus);
                self.respond(message, true, &status)
            }
            Err(BackReject::NoOriginForClient { client }) => {
                let status = format!("NO ORIGIN FOR CLIENT={client}");
                self.respond(message, false, &status)
            }
            Err(BackReject::SourceMismatch { expected, focused }) => {
                let status = format!("SOURCE MISMATCH expected={expected} focused={focused}");
                self.respond(message, false, &status)
            }
            Err(BackReject::MissingDestination) => self.respond(message, false, "DESTINATION GONE"),
            Err(BackReject::NotOnDestination { .. }) => {
                self.respond(message, false, "NOT ON DESTINATION")
            }
            Err(BackReject::OriginGone { tab }) => {
                let status = format!("ORIGIN GONE tab={tab}");
                self.respond(message, false, &status)
            }
            Err(BackReject::OriginPaneGone { pane }) => {
                let status = format!("ORIGIN PANE GONE pane={pane}");
                self.respond(message, false, &status)
            }
            // Unreachable on this path (no cache scan, claim pre-validated);
            // the arms exist for exhaustiveness only.
            Err(
                BackReject::BadClaim
                | BackReject::ActingClientMismatch { .. }
                | BackReject::NotSelected,
            ) => false,
        }
    }

    fn on_forget(&mut self, message: &PipeMessage) -> bool {
        let Ok(claimed) =
            state::parse_forget_claim(&message.args, message.payload.as_deref())
        else {
            // No usable client id names no target; answering from every
            // instance would duplicate responses, so stay silent.
            return false;
        };
        if !self.granted {
            return false;
        }
        let acting = get_plugin_ids().client_id;
        if !self.active {
            if state::authorize_forget(acting, Some(claimed)).is_err() {
                return false;
            }
            let status = state::cold_forget_status(claimed);
            return self.respond(message, true, &status);
        }
        let client = match state::authorize_forget(acting, Some(claimed)) {
            Ok(client) => client,
            // Not the selected target: stay silent (no output, no unblock,
            // no mutation) so the broadcast yields exactly one response.
            Err(ForgetReject::ActingClientMismatch { .. }
            | ForgetReject::NotSelected
            | ForgetReject::BadClaim) => return false,
        };
        let action = state::decide_forget(client);
        // Idempotent: clearing is the acknowledgment the mouse legacy path
        // waits for, so success holds with or without a recorded origin.
        // Only the selected client's origin is touched.
        let _ = self.origins.remove(&action.client);
        let status = format!("FORGOT CLIENT={}", action.client);
        self.respond(message, true, &status)
    }

    /// Record the status line and, for CLI pipe sources, emit exactly one
    /// scoped response plus unblock so the caller exits with the output.
    /// Returns true (render the new status).
    fn respond(&mut self, message: &PipeMessage, ok: bool, status: &str) -> bool {
        self.status = status.to_string();
        if let PipeSource::Cli(pipe_id) = &message.source {
            cli_pipe_output(pipe_id, &cli_response_line(ok, status));
            unblock_cli_pipe_input(pipe_id);
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::{
        cli_response_line, keybind_kdl, keybinds_include_our_bind, normalize_active, normalize_header_id, Broker, OriginState,
    };
    use super::state::PaneRef;
    use std::collections::BTreeSet;
    use zellij_tile::prelude::*;
    use zellij_tile::prelude::actions::Action;

    #[test]
    fn ungranted_broadcasts_leave_each_instance_untouched() {
        let mut brokers = [Broker::default(), Broker::default()];
        for broker in &mut brokers {
            broker.origins.insert(1, OriginState { tab: 4, pane: PaneRef::Terminal(8) });
        }
        for name in ["back", "forget"] {
            let message = PipeMessage::new(PipeSource::Cli("test".into()), name,
                &Some("client=1 source=terminal_8".into()), &None, false);
            for broker in &mut brokers {
                assert!(!broker.pipe(message.clone()));
                assert_eq!(broker.origins.len(), 1);
                assert!(broker.status.is_empty());
            }
        }
    }

    #[test]
    fn implicit_back_does_not_select_multiple_origin_owners() {
        let mut brokers = [Broker::default(), Broker::default()];
        for (id, broker) in brokers.iter_mut().enumerate() {
            broker.granted = true;
            broker.active = true;
            let Ok(client) = u16::try_from(id + 1) else {
                panic!("test client id out of range for id {id}");
            };
            broker.origins.insert(client, OriginState { tab: 4, pane: PaneRef::Terminal(8) });
        }
        let message = PipeMessage::new(PipeSource::Cli("test".into()), "back",
            &Some("source=terminal_8".into()), &None, false);
        for broker in &mut brokers {
            assert!(!broker.pipe(message.clone()));
            assert_eq!(broker.origins.len(), 1);
            assert!(broker.status.is_empty());
        }
    }

    // Given the runtime keybind KDL for any plugin id
    // When it is installed via runtime-only reconfigure
    // Then it binds Alt+o across shared_except locked and never binds Escape.
    #[test]
    fn keybind_kdl_binds_alt_o_without_escape() {
        let kdl = keybind_kdl(7);

        assert!(kdl.contains("shared_except \"locked\""), "{kdl}");
        assert!(kdl.contains("Alt o"), "{kdl}");
        assert!(kdl.contains("MessagePluginId 7"), "{kdl}");
        assert!(kdl.contains("navigate"), "{kdl}");
        assert!(!kdl.contains("Esc"), "{kdl}");
        assert!(!kdl.contains("back"), "{kdl}");
    }

    // Given a header id in layout-property form (KDL-quoted) or bare form
    // When it is normalized
    // Then quotes are stripped and empties rejected.
    #[test]
    fn normalize_header_id_strips_kdl_quotes() {
        assert_eq!(
            normalize_header_id("\"tdnext-header:shell\""),
            Some("tdnext-header:shell".to_string())
        );
        assert_eq!(
            normalize_header_id("tdnext-header:shell"),
            Some("tdnext-header:shell".to_string())
        );
        assert_eq!(normalize_header_id(""), None);
        assert_eq!(normalize_header_id("\"\""), None);
        assert_eq!(normalize_header_id("   "), None);
    }

    // Given an active flag in layout-property form (KDL-quoted) or bare form
    // When it is normalized
    // Then only an explicit true warms the session; missing, empty, and
    // any other value stay cold so a mid-session grant can never rebind.
    #[test]
    fn normalize_active_only_explicit_true_warms() {
        assert!(normalize_active(Some("\"true\"")));
        assert!(normalize_active(Some("true")));
        assert!(normalize_active(Some("  \"true\"  ")));
        assert!(!normalize_active(None));
        assert!(!normalize_active(Some("")));
        assert!(!normalize_active(Some("\"\"")));
        assert!(!normalize_active(Some("false")));
        assert!(!normalize_active(Some("\"false\"")));
        assert!(!normalize_active(Some("1")));
        assert!(!normalize_active(Some("yes")));
        assert!(!normalize_active(Some("TRUE")));
    }

    fn our_bind_keybinds() -> KeybindsVec {
        vec![(InputMode::Normal, vec![(
            KeyWithModifier {
                bare_key: BareKey::Char('o'),
                key_modifiers: BTreeSet::from([KeyModifier::Alt]),
            },
            vec![Action::KeybindPipe {
                name: None,
                payload: None,
                args: None,
                plugin: None,
                plugin_id: None,
                configuration: None,
                launch_new: false,
                skip_cache: false,
                floating: None,
                in_place: None,
                cwd: None,
                pane_title: None,
            }],
        )])]
    }

    // Given host-sent keybinds containing Alt+o in Normal mode
    // When they are checked
    // Then they confirm the binding (the API drops pipe fields, so the
    // fixture mirrors the wire form with empty fields).
    #[test]
    fn keybinds_with_our_bind_confirm() {
        assert!(keybinds_include_our_bind(&our_bind_keybinds()));
    }

    // Given a status line and an outcome
    // When the CLI response line is built
    // Then it carries a machine-readable ok/err prefix plus the status.
    #[test]
    fn cli_response_line_prefixes_outcome() {
        assert_eq!(
            cli_response_line(true, "RETURN client=1 pane=terminal_3"),
            "ok:RETURN client=1 pane=terminal_3"
        );
        assert_eq!(
            cli_response_line(false, "SOURCE MISMATCH expected=a focused=b"),
            "err:SOURCE MISMATCH expected=a focused=b"
        );
    }

    // Given host-sent keybinds without Alt+o in Normal mode (other key,
    // Alt+o only outside Normal, or empty)
    // When they are checked
    // Then they do not confirm. Stock binds Alt+o outside Normal, so the
    // Normal scope is what makes the signal ours.
    #[test]
    fn keybinds_without_our_bind_reject() {
        assert!(!keybinds_include_our_bind(&KeybindsVec::new()));
        assert!(!keybinds_include_our_bind(
            &vec![(InputMode::Normal, vec![])],
        ));
        assert!(!keybinds_include_our_bind(&vec![(
            InputMode::Locked,
            vec![(
                KeyWithModifier {
                    bare_key: BareKey::Char('o'),
                    key_modifiers: BTreeSet::from([KeyModifier::Alt]),
                },
                vec![],
            )],
        )]));
    }
}
