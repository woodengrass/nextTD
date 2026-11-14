//! Pure, host-independent open/back decisions for the navigator broker.
//!
//! Everything in this module is deterministic and unit-tested on the host
//! (`cargo test`). It performs no Zellij host calls. The WASM entry point in
//! `main.rs` only maps live Zellij queries into these inputs and turns the
//! returned actions into host calls (`switch_tab_to`, `focus_pane_with_id`).
//!
//! Decision summary:
//! - Origins are scoped per client: at most one recorded origin per client.
//! - `decide_open_resolved` takes a probe-verified destination (stable id +
//!   live position) and is a no-op on the destination tab (`AlreadyThere`);
//!   a keyboard open from outside the destination records the latest origin,
//!   replacing any stale previous one for that client (hybrid handoff: the
//!   mouse legacy path runs only after an acknowledged `forget` clears it).
//!   An unverifiable destination (`None`) fails as `MissingDestination`
//!   with no mutation and no host navigation call.
//! - `decide_back_resolved` takes a verified destination id; identity is by
//!   stable id, so a mouse-suffixed destination behaves like the bare name.
//!   Check order is fixed: ownership, live source recheck, destination
//!   existence, destination membership, origin validity.
//! - `parse_back_claim` + `authorize_back` resolve the serving target: an
//!   explicit claim serves only its own instance, an omitted client serves
//!   only when exactly one origin owner exists, and ambiguity and every
//!   validation failure reject explicitly. Rejections never consume the
//!   recorded origin (an invalid origin stays for retry; only a successful
//!   return or an explicit `forget` clears it).
//! - `forget` requires an explicit `client` id, serves only the matching
//!   instance, and always succeeds (idempotent: repeated forgets succeed).

use std::collections::BTreeMap;
use std::fmt;

/// Stable client identifier (`zellij_utils::data::ClientId` at 0.45.1).
pub type ClientId = u16;
/// Stable tab identifier (`TabInfo::tab_id`).
pub type TabId = usize;

/// A pane reference that survives a round trip through CLI pipe args/payload.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PaneRef {
    Terminal(u32),
    Plugin(u32),
}

impl fmt::Display for PaneRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Terminal(id) => write!(f, "terminal_{id}"),
            Self::Plugin(id) => write!(f, "plugin_{id}"),
        }
    }
}

impl PaneRef {
    /// Parse the `terminal_<id>` / `plugin_<id>` wire form.
    #[must_use]
    pub fn parse(raw: &str) -> Option<Self> {
        let (kind, number) = raw.split_once('_')?;
        let id: u32 = number.parse().ok()?;
        match kind {
            "terminal" => Some(Self::Terminal(id)),
            "plugin" => Some(Self::Plugin(id)),
            _ => None,
        }
    }
}

/// Where a client was before opening the header destination.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Origin {
    pub tab: TabId,
    pub pane: PaneRef,
}

/// Minimal tab snapshot; mapped from `TabInfo` by the caller.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TabView {
    pub id: TabId,
    pub position: usize,
    pub name: String,
}

/// Live focused-pane snapshot. `tab` is the stable tab id reported by the
/// host (`active_tab_ids`, despite the `tab_index` field name); callers must
/// not map it through tab positions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Focused {
    pub tab: TabId,
    pub pane: PaneRef,
}

/// Destination matching mirrors `navigation.matches_tab`: the exact header
/// name, or the header name with an `@origin` suffix appended.
#[must_use]
pub fn matches_destination(tab_name: &str, header: &str) -> bool {
    tab_name == header
        || tab_name
            .strip_prefix(header)
            .is_some_and(|rest| rest.starts_with('@'))
}

/// First tab (in tab order) matching the configured header.
#[must_use]
pub fn find_destination(tabs: &[TabView], header: &str) -> Option<(TabId, usize)> {
    tabs.iter()
        .find(|tab| matches_destination(&tab.name, header))
        .map(|tab| (tab.id, tab.position))
}

// ---------------------------------------------------------------------------
// binding readiness (reconfigure is asynchronous)
// ---------------------------------------------------------------------------

/// Tracks whether the runtime keybinding may be issued and whether the host
/// confirmed it. `READY` is reported only when the rebind was both issued
/// and confirmed: requesting `reconfigure` is fire-and-forget, so claiming
/// readiness on the permission grant alone would lie about the binding.
///
/// The rebind is issued only after a `TabUpdate` inventory proves the
/// configured header actually resolves to a tab. The host offers no
/// synchronous list-tabs call, so the destination id is unknowable before
/// that; a merely non-empty cache (tabs that never include the header)
/// must not trigger the rebind either. Issuing earlier steals `Alt+o` from
/// the legacy route before this broker can serve it. Deferring keeps the
/// fallback shortcut functional until the broker is genuinely ready.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Binding {
    flags: u8,
}

impl Binding {
    const GRANT: u8 = 1 << 0;
    const ISSUED: u8 = 1 << 1;
    const CONFIRMED: u8 = 1 << 2;
    const TABS_READY: u8 = 1 << 3;

    /// Record the warm permission grant. This alone never issues the rebind:
    /// the tab inventory must first resolve the header (see
    /// `on_tabs_updated`).
    pub const fn on_permission_granted(&mut self) {
        self.flags = self.flags | Self::GRANT;
    }

    /// Record whether the latest `TabUpdate` inventory resolves the
    /// configured header to a tab. Only a genuine match counts: an empty
    /// cache, or tabs without the header, carry no destination id.
    pub const fn on_tabs_updated(&mut self, has_destination: bool) {
        if has_destination {
            self.flags = self.flags | Self::TABS_READY;
        } else {
            self.flags = self.flags & !Self::TABS_READY;
        }
    }

    /// Whether `reconfigure` should be issued now: granted, destination
    /// proven, and not yet issued.
    #[must_use]
    pub const fn should_rebind(self) -> bool {
        self.flags & (Self::GRANT | Self::TABS_READY) == (Self::GRANT | Self::TABS_READY)
            && self.flags & Self::ISSUED == 0
    }

    /// Record that `reconfigure` was issued.
    pub const fn on_rebind_issued(&mut self) {
        self.flags = self.flags | Self::ISSUED;
    }

    /// Record host-sent keybinds. Only keybinds arriving after our rebind
    /// count as confirmation; earlier ones (e.g. from plugin load) do not.
    pub const fn on_keybinds_received(&mut self) {
        if self.flags & Self::ISSUED != 0 {
            self.flags = self.flags | Self::CONFIRMED;
        }
    }

    /// Whether the binding may be claimed ready.
    #[must_use]
    pub const fn ready(self) -> bool {
        self.flags & (Self::ISSUED | Self::CONFIRMED) == (Self::ISSUED | Self::CONFIRMED)
    }
}

// ---------------------------------------------------------------------------
// open ("navigate")
// ---------------------------------------------------------------------------

/// Outcome of an open request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OpenAction {
    /// Record `origin` (replacing any stale previous origin for this client),
    /// then switch to the 1-based tab index.
    Open {
        origin: Origin,
        switch_to_1based: u32,
    },
    /// Already on the destination tab: no state change, no host call.
    AlreadyThere,
}

/// Rejection of an open request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OpenReject {
    MissingDestination,
}

/// A probe-verified destination: the stable tab id plus the live 1-based
/// position from the verifying `get_tab_info` probe (fresh even across a
/// tab reorder that no `TabUpdate` has reported yet).
pub type VerifiedDest = (TabId, u32);

/// Pure open decision over a verified destination.
///
/// `dest` is `Some` only when the caller proved the destination through the
/// stable tab id: a cache match confirmed by a `get_tab_info` probe whose
/// name still matches the header (suffix form included), or the last known
/// destination id re-verified the same way. `None` means disappeared or
/// renamed-away: the press fails visibly with no mutation and no host
/// navigation call. Identity is by stable id, never by name: the server
/// switches by exact name only, so a bare-header name switch would silently
/// no-op on a suffixed tab while this decision already reported success.
/// A keyboard open from outside the destination always records the latest
/// origin, replacing any stale previous one for the client; reentry on the
/// destination is a no-op. (`Result` is already `#[must_use]`, so no extra
/// attribute is added.)
pub fn decide_open_resolved(
    dest: Option<VerifiedDest>,
    focused: &Focused,
) -> Result<OpenAction, OpenReject> {
    let (destination_id, switch_to_1based) = dest.ok_or(OpenReject::MissingDestination)?;
    if focused.tab == destination_id {
        return Ok(OpenAction::AlreadyThere);
    }
    Ok(OpenAction::Open {
        origin: Origin {
            tab: focused.tab,
            pane: focused.pane,
        },
        switch_to_1based,
    })
}

// ---------------------------------------------------------------------------
// back (CLI pipe)
// ---------------------------------------------------------------------------

/// Validated back claim: which client is returning, from which source pane.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BackClaim {
    pub client: ClientId,
    pub source: PaneRef,
}

/// Rejection of a back request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BackReject {
    /// No usable `client`/`source` material in args or payload.
    BadClaim,
    /// The claim names a different client than the one serving this message.
    /// The serving instance must stay silent (no output, no mutation) so a
    /// broadcast pipe produces exactly one response from the selected target.
    ActingClientMismatch { acting: ClientId, claimed: ClientId },
    /// No client was claimed and this instance owns no origin. Another
    /// instance may serve the message; stay silent.
    NotSelected,
    /// The claimed client owns no recorded origin (covers client mismatch
    /// against recorded state, e.g. after the origin was consumed).
    NoOriginForClient { client: ClientId },
    MissingDestination,
    /// The live focused pane is not the claimed source pane.
    SourceMismatch { expected: PaneRef, focused: PaneRef },
    /// The focused tab is not the destination tab.
    NotOnDestination { focused_tab: TabId },
    /// The recorded origin tab has closed.
    OriginGone { tab: TabId },
    /// The recorded origin pane no longer exists.
    OriginPaneGone { pane: PaneRef },
}

/// Extract the raw claim material. Payload pairs (`client=1 source=terminal_2`,
/// whitespace separated) are the base; explicit args override them.
pub fn parse_back_claim(
    args: &BTreeMap<String, String>,
    payload: Option<&str>,
) -> Result<(Option<ClientId>, Option<PaneRef>), BackReject> {
    let mut pairs: BTreeMap<&str, &str> = BTreeMap::new();
    if let Some(text) = payload {
        for token in text.split_whitespace() {
            if let Some((key, value)) = token.split_once('=') {
                pairs.insert(key, value);
            }
        }
    }
    for (key, value) in args {
        pairs.insert(key.as_str(), value.as_str());
    }
    let client = match pairs.get("client") {
        None => None,
        Some(raw) => Some(raw.parse::<ClientId>().map_err(|_| BackReject::BadClaim)?),
    };
    let source = match pairs.get("source") {
        None => None,
        Some(raw) => Some(PaneRef::parse(raw).ok_or(BackReject::BadClaim)?),
    };
    if source.is_none() {
        // The source recheck is mandatory, so a claim without it is useless.
        return Err(BackReject::BadClaim);
    }
    Ok((client, source))
}

/// Resolve the serving target: an explicit claim serves only its own
/// instance; an omitted client serves only the instance that owns an origin.
/// Anything else stays silent so a broadcast pipe yields exactly one
/// response from the selected target and no wrong client mutates.
/// (`Result` is already `#[must_use]`, so no extra attribute is added.)
pub const fn authorize_back(
    acting: ClientId,
    explicit: Option<ClientId>,
    has_own_origin: bool,
) -> Result<ClientId, BackReject> {
    match explicit {
        Some(claimed) if claimed == acting => Ok(acting),
        Some(claimed) => Err(BackReject::ActingClientMismatch { acting, claimed }),
        None if has_own_origin => Ok(acting),
        None => Err(BackReject::NotSelected),
    }
}

/// Successful back: focus `focus` by stable pane id. The host switches to
/// the pane's tab as part of focusing, so no separate tab switch (and no
/// potentially stale position index) is needed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReturnAction {
    pub focus: PaneRef,
}

// ---------------------------------------------------------------------------
// back over a verified destination (cache miss or stale: host probes)
// ---------------------------------------------------------------------------

/// Inputs for a back decision over a verified destination id. `dest` is
/// `Some` only when the caller proved the destination through the stable
/// tab id (cache match confirmed by probe, or the last known destination
/// id re-verified); `origin_tab_exists` is whether `get_tab_info` finds the
/// recorded origin tab. Identity is by stable id throughout: the focused
/// tab either is the destination id or it is not — no name comparison, so
/// a mouse-suffixed destination (`header@origin:pane`) behaves exactly
/// like the bare name.
pub struct ResolvedBackInputs {
    pub claim: BackClaim,
    pub recorded: Option<Origin>,
    pub focused: Focused,
    pub dest: Option<TabId>,
    pub origin_tab_exists: bool,
    pub origin_pane_exists: bool,
}

/// Pure back decision over a verified destination. Check order is fixed:
/// ownership, live source recheck, destination existence,
/// destination membership, origin validity. (`Result` is already
/// `#[must_use]`, so no extra attribute is added.)
pub fn decide_back_resolved(inputs: &ResolvedBackInputs) -> Result<ReturnAction, BackReject> {
    let recorded = inputs.recorded.ok_or(BackReject::NoOriginForClient {
        client: inputs.claim.client,
    })?;
    if inputs.focused.pane != inputs.claim.source {
        return Err(BackReject::SourceMismatch {
            expected: inputs.claim.source,
            focused: inputs.focused.pane,
        });
    }
    let dest = inputs.dest.ok_or(BackReject::MissingDestination)?;
    if inputs.focused.tab != dest {
        return Err(BackReject::NotOnDestination {
            focused_tab: inputs.focused.tab,
        });
    }
    if !inputs.origin_tab_exists {
        return Err(BackReject::OriginGone { tab: recorded.tab });
    }
    if !inputs.origin_pane_exists {
        return Err(BackReject::OriginPaneGone { pane: recorded.pane });
    }
    Ok(ReturnAction {
        focus: recorded.pane,
    })
}

// ---------------------------------------------------------------------------
// forget (CLI pipe, explicit client acknowledgment)
// ---------------------------------------------------------------------------

/// Rejection of a forget request. Mismatches stay silent at the call site so
/// a broadcast pipe produces exactly one response from the selected target.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ForgetReject {
    /// No usable `client` material in args or payload; the id is required.
    BadClaim,
    /// The claim names a different client than the one serving this message.
    ActingClientMismatch { acting: ClientId, claimed: ClientId },
    /// No client was claimed. Unlike back there is no implicit owner
    /// fallback: forget is explicit-only, so stay silent.
    NotSelected,
}

/// Extract the required client id. Payload pairs (`client=1`, whitespace
/// separated) are the base; explicit args override them.
pub fn parse_forget_claim(
    args: &BTreeMap<String, String>,
    payload: Option<&str>,
) -> Result<ClientId, ForgetReject> {
    let mut pairs: BTreeMap<&str, &str> = BTreeMap::new();
    if let Some(text) = payload {
        for token in text.split_whitespace() {
            if let Some((key, value)) = token.split_once('=') {
                pairs.insert(key, value);
            }
        }
    }
    for (key, value) in args {
        pairs.insert(key.as_str(), value.as_str());
    }
    pairs.get("client").map_or(Err(ForgetReject::BadClaim), |raw| {
        raw.parse::<ClientId>().map_err(|_| ForgetReject::BadClaim)
    })
}

/// Resolve the serving target: an explicit claim serves only its own
/// instance. Anything else stays silent so a broadcast pipe yields exactly
/// one response from the selected target and no wrong client mutates.
/// (`Result` is already `#[must_use]`, so no extra attribute is needed.)
pub const fn authorize_forget(
    acting: ClientId,
    explicit: Option<ClientId>,
) -> Result<ClientId, ForgetReject> {
    match explicit {
        Some(claimed) if claimed == acting => Ok(acting),
        Some(claimed) => Err(ForgetReject::ActingClientMismatch { acting, claimed }),
        None => Err(ForgetReject::NotSelected),
    }
}

/// Successful forget: clear the recorded origin for `client`. Idempotent: a
/// forget with no recorded origin still succeeds so callers can retry and the
/// mouse legacy path can proceed after acknowledgment.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ForgetAction {
    pub client: ClientId,
}

/// Pure forget decision. Always succeeds once authorized.
#[must_use]
pub const fn decide_forget(client: ClientId) -> ForgetAction {
    ForgetAction { client }
}

// ---------------------------------------------------------------------------
// cold-granted pipe answers
// ---------------------------------------------------------------------------

/// Cold-granted back answer. The caller must first verify the claimed client
/// matches the acting instance. Ungranted instances cannot answer pipes.
#[must_use]
pub fn cold_back_status(client: ClientId) -> String {
    format!("NO ORIGIN FOR CLIENT={client}")
}

/// Cold forget answer: idempotent acknowledgment for the explicitly claimed
/// client, after the caller verifies the acting instance. Cold sessions do
/// not record origins.
#[must_use]
pub fn cold_forget_status(client: ClientId) -> String {
    format!("FORGOT CLIENT={client}")
}

// ---------------------------------------------------------------------------
// tests (deterministic, host-independent)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const HEADER: &str = "tdnext-header:shell";

    // Given a verified destination and a client focused on a workspace tab
    // When it opens
    // Then the origin is recorded and the verified 1-based index is returned.
    #[test]
    fn open_records_origin_and_switches_to_verified_position() {
        let focused = Focused {
            tab: 11,
            pane: PaneRef::Terminal(3),
        };

        let action = decide_open_resolved(Some((22, 2)), &focused);

        assert_eq!(
            action,
            Ok(OpenAction::Open {
                origin: Origin {
                    tab: 11,
                    pane: PaneRef::Terminal(3),
                },
                switch_to_1based: 2,
            })
        );
    }

    // Given a first press with an empty cache but a verified last-known id
    // When it opens
    // Then the origin is recorded and the press is served: no priming needed.
    #[test]
    fn open_first_press_served_from_verified_last_known() {
        let focused = Focused {
            tab: 11,
            pane: PaneRef::Terminal(3),
        };

        let action = decide_open_resolved(Some((22, 2)), &focused);

        assert_eq!(
            action,
            Ok(OpenAction::Open {
                origin: Origin {
                    tab: 11,
                    pane: PaneRef::Terminal(3),
                },
                switch_to_1based: 2,
            })
        );
    }

    // Given a tab reorder the cache has not reported yet
    // When it opens with the probe's live position
    // Then the switch follows the live position, not the stale cache index.
    #[test]
    fn open_follows_live_position_across_reorder() {
        let focused = Focused {
            tab: 11,
            pane: PaneRef::Terminal(3),
        };

        let action = decide_open_resolved(Some((22, 1)), &focused);

        assert_eq!(
            action,
            Ok(OpenAction::Open {
                origin: Origin {
                    tab: 11,
                    pane: PaneRef::Terminal(3),
                },
                switch_to_1based: 1,
            })
        );
    }

    // Given no verified destination (disappeared or renamed-away tab)
    // When a client opens
    // Then the request is rejected as missing destination, with no mutation
    // and no host navigation call at the call site.
    #[test]
    fn open_missing_destination_rejected() {
        let focused = Focused {
            tab: 11,
            pane: PaneRef::Terminal(3),
        };

        let action = decide_open_resolved(None, &focused);

        assert_eq!(action, Err(OpenReject::MissingDestination));
    }

    // Given a client already focused on the destination tab
    // When it opens again (repeated open)
    // Then the request is a no-op, with or without a recorded origin.
    // Identity is the stable id, so a mouse-suffixed destination tab
    // (`header@origin:pane`) is recognized without any name comparison.
    #[test]
    fn open_repeated_on_destination_is_noop() {
        let on_destination = Focused {
            tab: 22,
            pane: PaneRef::Terminal(9),
        };

        assert_eq!(
            decide_open_resolved(Some((22, 2)), &on_destination),
            Ok(OpenAction::AlreadyThere)
        );
    }

    // Given a client with a stale recorded origin focused on another
    // workspace tab
    // When it opens again via keyboard
    // Then the latest origin replaces the stale one and a switch is issued.
    #[test]
    fn open_from_other_tab_replaces_stale_origin() {
        let elsewhere = Focused {
            tab: 11,
            pane: PaneRef::Terminal(5),
        };

        let action = decide_open_resolved(Some((22, 2)), &elsewhere);

        assert_eq!(
            action,
            Ok(OpenAction::Open {
                origin: Origin {
                    tab: 11,
                    pane: PaneRef::Terminal(5),
                },
                switch_to_1based: 2,
            })
        );
    }

    // Given a valid claim, a live source recheck on the destination, and an
    // existing origin
    // When the client goes back over the verified destination id
    // Then it returns to the origin pane.
    #[test]
    fn back_returns_to_origin_tab_and_pane() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        let action = decide_back_resolved(&inputs);

        assert_eq!(
            action,
            Ok(ReturnAction {
                focus: PaneRef::Terminal(3),
            })
        );
    }

    // Given a back claim naming this instance's client
    // When the claim is authorized
    // Then this instance serves it.
    #[test]
    fn back_authorize_explicit_match_serves() {
        assert_eq!(authorize_back(1, Some(1), true), Ok(1));
        assert_eq!(authorize_back(1, Some(1), false), Ok(1));
    }

    // Given a back claim naming a different client
    // When the claim is authorized
    // Then this instance stays silent so no wrong client mutates.
    #[test]
    fn back_authorize_mismatch_stays_silent() {
        assert_eq!(
            authorize_back(1, Some(2), true),
            Err(BackReject::ActingClientMismatch {
                acting: 1,
                claimed: 2,
            })
        );
        assert_eq!(
            authorize_back(1, Some(2), false),
            Err(BackReject::ActingClientMismatch {
                acting: 1,
                claimed: 2,
            })
        );
    }

    // Given no explicit client claim
    // When this instance owns an origin
    // Then it serves as the unique plausible target.
    #[test]
    fn back_authorize_implicit_with_own_origin_serves() {
        assert_eq!(authorize_back(1, None, true), Ok(1));
    }

    // Given no explicit client claim and no own origin
    // When the claim is authorized
    // Then this instance stays silent; another instance may serve.
    #[test]
    fn back_authorize_implicit_without_origin_not_selected() {
        assert_eq!(authorize_back(1, None, false), Err(BackReject::NotSelected));
    }

    // Given the permission grant was followed by host-sent keybinds
    // When the binding transitions run
    // Then readiness holds only after issue plus confirm.
    #[test]
    fn binding_ready_only_after_request_and_confirm() {
        let mut binding = Binding::default();
        assert!(!binding.ready());
        binding.on_keybinds_received();
        assert!(!binding.ready());

        binding.on_permission_granted();
        binding.on_tabs_updated(true);
        assert!(binding.should_rebind());
        binding.on_rebind_issued();
        assert!(!binding.ready());

        binding.on_keybinds_received();
        assert!(binding.ready());
    }

    // Given a warm grant with no tab inventory yet
    // When the rebind gate is consulted
    // Then no rebind issues: the legacy shortcut stays owner until a
    // TabUpdate proves the header resolves to a tab.
    #[test]
    fn binding_defers_rebind_until_tabs_ready() {
        let mut binding = Binding::default();
        binding.on_permission_granted();
        binding.on_tabs_updated(false);
        assert!(!binding.should_rebind());

        binding.on_tabs_updated(true);
        assert!(binding.should_rebind());

        binding.on_rebind_issued();
        assert!(!binding.should_rebind());
    }

    // Given a warm grant and a non-empty cache that never includes the header
    // When the rebind gate is consulted
    // Then no rebind issues: stealing the shortcut without a resolvable
    // destination would strand the first press. The old gate (non-empty
    // alone) issues here; the match gate holds the legacy route.
    #[test]
    fn binding_requires_matching_header_not_merely_nonempty() {
        let mut binding = Binding::default();
        binding.on_permission_granted();
        binding.on_tabs_updated(false);
        assert!(!binding.should_rebind());
        binding.on_keybinds_received();
        assert!(!binding.ready());
    }

    // Given host-sent keybinds that predate our rebind (e.g. from load)
    // When they are received
    // Then they never confirm: only post-issue keybinds count, so a stale
    // pre-request map cannot fake READY.
    #[test]
    fn binding_ignores_keybinds_before_issue() {
        let mut binding = Binding::default();
        binding.on_permission_granted();
        binding.on_tabs_updated(true);
        binding.on_keybinds_received();
        assert!(!binding.ready());

        binding.on_rebind_issued();
        binding.on_keybinds_received();
        assert!(binding.ready());
    }

    // Given a verified destination and a valid claim, with the live source
    // recheck on the destination and an existing origin
    // When the resolved back runs (hybrid keyboard open, then return)
    // Then it returns to the origin pane.
    #[test]
    fn back_resolved_returns_to_origin() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        assert_eq!(
            decide_back_resolved(&inputs),
            Ok(ReturnAction {
                focus: PaneRef::Terminal(3),
            })
        );
    }

    // Given a mouse-suffixed destination tab (hybrid handoff renamed it)
    // When the resolved back runs from the suffixed tab
    // Then identity by stable id returns to the origin: suffix needs no
    // name comparison anywhere on this path.
    #[test]
    fn back_resolved_returns_from_suffixed_destination() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        assert_eq!(
            decide_back_resolved(&inputs),
            Ok(ReturnAction {
                focus: PaneRef::Terminal(3),
            })
        );
    }

    // Given a verified destination with focus outside it
    // When the resolved back runs
    // Then the request names the focused tab explicitly instead of silence.
    #[test]
    fn back_resolved_not_on_destination_names_focused_tab() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(3),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 11,
                pane: PaneRef::Terminal(3),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        assert_eq!(
            decide_back_resolved(&inputs),
            Err(BackReject::NotOnDestination { focused_tab: 11 })
        );
    }

    // Given a disappeared destination (closed or renamed-away tab)
    // When the resolved back runs
    // Then the request reports the destination gone: visible, never silent,
    // and the origin is kept for retry rather than consumed.
    #[test]
    fn back_resolved_destination_gone_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: None,
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        assert_eq!(
            decide_back_resolved(&inputs),
            Err(BackReject::MissingDestination)
        );
    }

    // Given a verified destination whose origin tab has closed
    // When the resolved back runs
    // Then the request reports origin gone.
    #[test]
    fn back_resolved_origin_tab_gone_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 99,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: false,
            origin_pane_exists: true,
        };

        assert_eq!(
            decide_back_resolved(&inputs),
            Err(BackReject::OriginGone { tab: 99 })
        );
    }

    // Given a verified destination with a live source mismatch
    // When the resolved back runs
    // Then the request reports the mismatch before touching anything.
    #[test]
    fn back_resolved_source_mismatch_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Plugin(4),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        assert_eq!(
            decide_back_resolved(&inputs),
            Err(BackReject::SourceMismatch {
                expected: PaneRef::Terminal(7),
                focused: PaneRef::Plugin(4),
            })
        );
    }

    // Given a verified destination but no recorded origin for the client
    // When the resolved back runs
    // Then the request reports no origin for that client.
    #[test]
    fn back_resolved_no_origin_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 2,
                source: PaneRef::Terminal(7),
            },
            recorded: None,
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        assert_eq!(
            decide_back_resolved(&inputs),
            Err(BackReject::NoOriginForClient { client: 2 })
        );
    }

    // Given the origin tab has closed since the open
    // When the client goes back over the verified destination id
    // Then the request is rejected as origin gone.
    #[test]
    fn back_origin_tab_gone_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 99,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: false,
            origin_pane_exists: true,
        };

        let action = decide_back_resolved(&inputs);

        assert_eq!(action, Err(BackReject::OriginGone { tab: 99 }));
    }

    // Given the origin pane no longer exists
    // When the client goes back over the verified destination id
    // Then the request is rejected as origin pane gone.
    #[test]
    fn back_origin_pane_gone_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: false,
        };

        let action = decide_back_resolved(&inputs);

        assert_eq!(
            action,
            Err(BackReject::OriginPaneGone {
                pane: PaneRef::Terminal(3),
            })
        );
    }

    // Given the live focused pane differs from the claimed source pane
    // When the client goes back over the verified destination id
    // Then the request is rejected as source mismatch.
    #[test]
    fn back_source_mismatch_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Plugin(4),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        let action = decide_back_resolved(&inputs);

        assert_eq!(
            action,
            Err(BackReject::SourceMismatch {
                expected: PaneRef::Terminal(7),
                focused: PaneRef::Plugin(4),
            })
        );
    }

    // Given an origin recorded for client 1
    // When client 2 claims the back over the verified destination id
    // Then the request is rejected: no origin for the claiming client.
    #[test]
    fn back_client_mismatch_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 2,
                source: PaneRef::Terminal(7),
            },
            recorded: None,
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        let action = decide_back_resolved(&inputs);

        assert_eq!(action, Err(BackReject::NoOriginForClient { client: 2 }));
    }

    // Given the destination tab has closed since the open
    // When the client goes back over the verified destination id
    // Then the request is rejected as missing destination.
    #[test]
    fn back_destination_gone_rejected() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 11,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: None,
            origin_tab_exists: true,
            origin_pane_exists: true,
        };

        let action = decide_back_resolved(&inputs);

        assert_eq!(action, Err(BackReject::MissingDestination));
    }

    // Given a payload with no usable claim material
    // When the back claim is parsed
    // Then the request is rejected as a bad claim.
    #[test]
    fn back_bad_claim_rejected() {
        let args = BTreeMap::new();

        assert_eq!(
            parse_back_claim(&args, Some("hello world")),
            Err(BackReject::BadClaim)
        );
        assert_eq!(parse_back_claim(&args, None), Err(BackReject::BadClaim));
    }

    // Given a payload claim overridden by explicit args
    // When the back claim is parsed
    // Then args win and both wire forms parse.
    #[test]
    fn back_claim_args_override_payload() {
        let mut args = BTreeMap::new();
        args.insert("source".to_string(), "plugin_4".to_string());

        let (client, source) =
            parse_back_claim(&args, Some("client=1 source=terminal_7")).unwrap_or((None, None));

        assert_eq!(client, Some(1));
        assert_eq!(source, Some(PaneRef::Plugin(4)));
    }

    // Given an explicit forget claim for this instance's client
    // When the claim is parsed and authorized
    // Then this instance serves it; payload is the base and args override.
    #[test]
    fn forget_explicit_match_serves() {
        let mut args = BTreeMap::new();
        args.insert("client".to_string(), "2".to_string());

        assert_eq!(parse_forget_claim(&args, Some("client=1")), Ok(2));
        assert_eq!(parse_forget_claim(&BTreeMap::new(), Some("client=1")), Ok(1));
        assert_eq!(authorize_forget(1, Some(1)), Ok(1));
    }

    // Given a forget claim naming a different client, or no client at all
    // When the claim is parsed or authorized
    // Then this instance stays silent so no wrong client mutates; the id is
    // required, so there is no implicit owner fallback.
    #[test]
    fn forget_mismatch_and_missing_stay_silent() {
        assert_eq!(
            authorize_forget(1, Some(2)),
            Err(ForgetReject::ActingClientMismatch {
                acting: 1,
                claimed: 2,
            })
        );
        assert_eq!(authorize_forget(1, None), Err(ForgetReject::NotSelected));
        assert_eq!(
            parse_forget_claim(&BTreeMap::new(), None),
            Err(ForgetReject::BadClaim)
        );
        assert_eq!(
            parse_forget_claim(&BTreeMap::new(), Some("source=terminal_7")),
            Err(ForgetReject::BadClaim)
        );
        assert_eq!(
            parse_forget_claim(&BTreeMap::new(), Some("client=abc")),
            Err(ForgetReject::BadClaim)
        );
    }

    // Given an authorized forget, with or without a recorded origin
    // When the forget decision runs
    // Then it succeeds either way, so repeated forgets succeed.
    #[test]
    fn forget_succeeds_with_or_without_recorded_origin() {
        assert_eq!(decide_forget(1), ForgetAction { client: 1 });
    }

    // Given an ungranted broker asked for back with an explicit client
    // When the cold answer is computed
    // Then it carries the exact legacy-fallback line (no origin can exist
    // without granted host calls), so the caller falls back instead of
    // hanging on a gated host query.
    #[test]
    fn cold_back_names_exact_no_origin_for_explicit_client() {
        assert_eq!(
            cold_back_status(1),
            "NO ORIGIN FOR CLIENT=1".to_string()
        );
        assert_eq!(None::<ClientId>.map(cold_back_status), None);
    }

    // Given an ungranted broker asked for forget with an explicit client
    // When the cold answer is computed
    // Then it acknowledges idempotently; without a client it stays silent.
    #[test]
    fn cold_forget_acknowledges_explicit_client() {
        assert_eq!(cold_forget_status(2), "FORGOT CLIENT=2".to_string());
        assert_eq!(None::<ClientId>.map(cold_forget_status), None);
    }

    // Given a recorded origin whose tab has closed
    // When back is rejected and retried over the verified destination id
    // without any state change
    // Then the rejection repeats identically: errors never consume the
    // origin, so the caller can retry (or explicitly forget) instead of
    // seeing the error turn into an absence.
    #[test]
    fn back_invalid_origin_retry_repeats_rejection() {
        let inputs = ResolvedBackInputs {
            claim: BackClaim {
                client: 1,
                source: PaneRef::Terminal(7),
            },
            recorded: Some(Origin {
                tab: 99,
                pane: PaneRef::Terminal(3),
            }),
            focused: Focused {
                tab: 22,
                pane: PaneRef::Terminal(7),
            },
            dest: Some(22),
            origin_tab_exists: false,
            origin_pane_exists: true,
        };

        assert_eq!(
            decide_back_resolved(&inputs),
            Err(BackReject::OriginGone { tab: 99 })
        );
        assert_eq!(
            decide_back_resolved(&inputs),
            Err(BackReject::OriginGone { tab: 99 })
        );
    }

    // Given a destination tab renamed with the @origin suffix
    // When tabs are scanned
    // Then the suffixed tab still matches the configured header.
    #[test]
    fn destination_suffix_still_matches() {
        assert!(matches_destination("tdnext-header:shell", HEADER));
        assert!(matches_destination("tdnext-header:shell@11:terminal_3", HEADER));
        assert!(!matches_destination("tdnext-header:shellfish", HEADER));
        assert!(!matches_destination("Files", HEADER));
    }
}
