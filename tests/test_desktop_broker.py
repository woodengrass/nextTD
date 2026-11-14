"""Desktop native launcher broker integration.

Contract (navigation.py and broker Rust sources untouched):

- New native sessions with a primary header preload exactly one logical
  broker instance: one ``plugin location="file:...navigator-broker.wasm"``
  pane in the primary header tab with unique config
  ``header_id "tdnext-header:<primary-id>"``.
- The session env enables the broker only for full cached grants.
- Static config keeps the Python Run binding until the broker takes over.
- Absent-header sessions keep the unbind with no env and no broker pane;
  fallback sessions keep the Python Run binding with no env.
"""

from pathlib import Path

import pytest
from test_fast_header_shortcut import capture_launch

from tdnext.plugins.desktop import plugin as desktop_plugin

FULL_PERMS = (
    "    ReadApplicationState\n"
    "    ChangeApplicationState\n"
    "    Reconfigure\n"
    "    ReadCliPipes\n"
)

_PARTIAL_PERMS = "    ReadApplicationState\n"


def _host_entry(key: str, perms: str) -> str:
    """Render one cache entry the way PermissionCache::to_string emits it."""
    return f'"{key}" {{\n{perms}}}\n'


def _write_permissions_cache(tmp_path: Path, text: str) -> None:
    cache = tmp_path / "state" / "zellij-cache" / "zellij"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "permissions.kdl").write_text(text, encoding="utf-8")


def _grants_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> bool:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    _write_permissions_cache(tmp_path, text)
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(tmp_path / "state"))
    return desktop_plugin.broker_grants_cached(asset)


def test_broker_asset_is_usable_wasm() -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    assert asset.name == "navigator-broker.wasm"
    assert asset.read_bytes()[:4] == b"\0asm"


def test_primary_header_session_preloads_one_broker_instance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, layout, _env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences", "settings"))

    assert layout.count("navigator-broker.wasm") == 1
    assert layout.count('plugin location="file:') == 1
    assert layout.count('header_id "tdnext-header:preferences"') == 1
    assert layout.count('header_id "tdnext-header:') == 1


def test_broker_env_set_only_when_grants_cached(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness is proven, never assumed: env needs cached prior grants."""
    _config, _layout, cold_env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert "TDNEXT_NAVIGATOR_BROKER" not in cold_env
    assert "TDNEXT_NAVIGATOR_HEADER" not in cold_env

    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    cache = tmp_path / "state" / "zellij-cache" / "zellij"
    cache.mkdir(parents=True)
    (cache / "permissions.kdl").write_text(
        f'"{asset}" {{\n'
        "    ReadApplicationState\n"
        "    ChangeApplicationState\n"
        "    Reconfigure\n"
        "    ReadCliPipes\n"
        "}\n",
        encoding="utf-8",
    )
    _config, _layout, warm_env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert warm_env["TDNEXT_NAVIGATOR_BROKER"] == "1"
    assert warm_env["TDNEXT_NAVIGATOR_HEADER"] == "preferences"


def test_layout_active_flag_matches_warmed_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pane always preloads, but only a warmed session is marked active."""
    _config, cold_layout, _env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert 'active "false"' in cold_layout
    assert 'active "true"' not in cold_layout

    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    cache = tmp_path / "state" / "zellij-cache" / "zellij"
    cache.mkdir(parents=True)
    (cache / "permissions.kdl").write_text(
        f'"{asset}" {{\n'
        "    ReadApplicationState\n"
        "    ChangeApplicationState\n"
        "    Reconfigure\n"
        "    ReadCliPipes\n"
        "}\n",
        encoding="utf-8",
    )
    _config, warm_layout, _env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert 'active "true"' in warm_layout
    assert warm_layout.count('active "true"') == 1


def test_cold_launch_preserves_existing_permission_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    original = _host_entry(str(asset), _PARTIAL_PERMS) + _host_entry("other", "    OpenFiles\n")
    _write_permissions_cache(tmp_path, original)

    _config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    cache = tmp_path / "state" / "zellij-cache" / "zellij" / "permissions.kdl"
    assert cache.read_text(encoding="utf-8") == original
    assert 'active "false"' in layout
    assert "TDNEXT_NAVIGATOR_BROKER" not in env


def test_grant_parsing_last_entry_wins(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror the host: a later entry overwrites an earlier one."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    cache = tmp_path / "state" / "zellij-cache" / "zellij"
    cache.mkdir(parents=True)
    full = (
        f'"{asset}" {{\n'
        "    ReadApplicationState\n"
        "    ChangeApplicationState\n"
        "    Reconfigure\n"
        "    ReadCliPipes\n"
        "}\n"
    )
    partial = f'"{asset}" {{\n    ReadApplicationState\n}}\n'

    (cache / "permissions.kdl").write_text(partial + full, encoding="utf-8")
    _, _, env, _ = capture_launch(tmp_path, monkeypatch, ("preferences",))
    assert env.get("TDNEXT_NAVIGATOR_BROKER") == "1"

    (cache / "permissions.kdl").write_text(full + partial, encoding="utf-8")
    _, _, env, _ = capture_launch(tmp_path, monkeypatch, ("preferences",))
    assert "TDNEXT_NAVIGATOR_BROKER" not in env


@pytest.mark.parametrize("body", [
    "",
    '"{}" {{\n    ReadApplicationState\n}}\n',
    '"{}" {{\n    ReadApplicationState\n    ChangeApplicationState\n    Reconfigure\n}}\n',
    "not kdl at all",
])
def test_broker_env_off_without_full_grants(
    tmp_path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """Fresh, partial, denied, or corrupt caches all stay on legacy."""
    asset = desktop_plugin.broker_asset_path()
    cache = tmp_path / "state" / "zellij-cache" / "zellij"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "permissions.kdl").write_text(body.format(asset), encoding="utf-8")
    _config, _layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert "TDNEXT_NAVIGATOR_BROKER" not in env
    assert "TDNEXT_NAVIGATOR_HEADER" not in env


def test_secondary_header_has_no_broker_pane(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, layout, _env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences", "settings"))

    assert layout.count("navigator-broker.wasm") == 1


def test_primary_header_config_keeps_python_run_as_initial_binding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a primary header, the static Alt+o stays a Python Run binding.

    The broker takes over only via its own runtime reconfigure after the
    user explicitly grants it (Zellij overlays replace per key), so the
    shortcut works with zero grants and nothing is dead while cold.
    """
    config, _layout, _env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences", "settings"))

    assert 'bind "Alt o" {' in config
    assert "tdnext.plugins.desktop.shortcut" in config


def test_absent_header_preserves_unbind_without_broker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ())

    bindings = [line.strip() for line in config.splitlines()]
    assert 'unbind "Alt o"' in bindings
    assert 'bind "Alt o" {' not in bindings
    assert "navigator-broker.wasm" not in layout
    assert "TDNEXT_NAVIGATOR_BROKER" not in env
    assert "TDNEXT_NAVIGATOR_HEADER" not in env


def test_missing_asset_preserves_python_run_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop_plugin, "broker_asset_path", lambda: None)
    config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert 'bind "Alt o" {' in config
    assert "tdnext.plugins.desktop.shortcut" in config
    assert "navigator-broker.wasm" not in layout
    assert "TDNEXT_NAVIGATOR_BROKER" not in env
    assert "TDNEXT_NAVIGATOR_HEADER" not in env


def test_corrupt_asset_preserves_python_run_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop_plugin, "broker_asset_path", lambda: None)
    config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert 'bind "Alt o" {' in config
    assert "navigator-broker.wasm" not in layout
    assert "TDNEXT_NAVIGATOR_BROKER" not in env


def test_primary_header_id_picks_first_header() -> None:
    assert desktop_plugin.primary_header_id([]) is None


def test_host_generated_cache_with_sibling_entries_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling plugin entries and shuffled permission order still warm the broker."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = (
        '"some-other-plugin" {\n'
        "    OpenFiles\n"
        "}\n"
        f'"{asset}" {{\n'
        "    Reconfigure\n"
        "    ReadCliPipes\n"
        "    ReadApplicationState\n"
        "    ChangeApplicationState\n"
        "}\n"
    )
    assert _grants_cached(tmp_path, monkeypatch, text) is True


def test_line_commented_block_does_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `//`-commented block is not a node, even when it names the exact path."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = (
        f'// "{asset}" {{\n'
        "//     ReadApplicationState\n"
        "//     ChangeApplicationState\n"
        "//     Reconfigure\n"
        "//     ReadCliPipes\n"
        "// }\n"
    )
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_block_comment_does_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = f'/* "{asset}" {{\n{FULL_PERMS}}}\n*/\n'
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_nested_key_does_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only top-level nodes count: the path nested under another key is ignored."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = f'"other" {{\n    "{asset}" {{\n{FULL_PERMS}    }}\n}}\n'
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_nested_grandchildren_do_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Permissions hidden under a child node are not direct grants."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = f'"{asset}" {{\n    wrapper {{\n{FULL_PERMS}    }}\n}}\n'
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_unclosed_brace_does_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = f'"{asset}" {{\n{FULL_PERMS}'
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_garbage_before_valid_block_does_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stray text poisons the whole document, exactly like the host KDL parse."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = f'@@@ not kdl\n"{asset}" {{\n{FULL_PERMS}}}\n'
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_permission_names_only_in_comments_do_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = (
        f'"{asset}" {{\n'
        "    // ReadApplicationState\n"
        "    // ChangeApplicationState\n"
        "    // Reconfigure\n"
        "    // ReadCliPipes\n"
        "}\n"
    )
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_commented_duplicate_does_not_override_last_real_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only real duplicate entries participate in last-wins; commented ones are skipped."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    full = f'"{asset}" {{\n{FULL_PERMS}}}\n'
    partial = f'"{asset}" {{\n    ReadApplicationState\n}}\n'
    commented_full = "".join(f"// {line}\n" if line else "\n" for line in full.splitlines())
    commented_partial = "".join(f"// {line}\n" if line else "\n" for line in partial.splitlines())

    assert _grants_cached(tmp_path, monkeypatch, full + commented_partial) is True
    assert _grants_cached(tmp_path, monkeypatch, partial + commented_full) is False


def test_similar_path_with_suffix_does_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A longer key that merely contains the asset path must not warm it."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = f'"{asset}.bak" {{\n{FULL_PERMS}}}\n"{asset}" {{\n    ReadApplicationState\n}}\n'
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_env_and_layout_share_snapshot_when_cache_cools_mid_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warm-then-cold: the layout active flag keeps the startup grant."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    _write_permissions_cache(tmp_path, f'"{asset}" {{\n{FULL_PERMS}}}\n')
    real_grants_cached = desktop_plugin.broker_grants_cached

    def cool_after_first_read(wasm: Path) -> bool:
        result = real_grants_cached(wasm)
        _write_permissions_cache(tmp_path, "")
        return result

    monkeypatch.setattr(desktop_plugin, "broker_grants_cached", cool_after_first_read)
    _config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert env["TDNEXT_NAVIGATOR_BROKER"] == "1"
    assert env["TDNEXT_NAVIGATOR_HEADER"] == "preferences"
    assert 'active "true"' in layout


def test_env_and_layout_share_snapshot_when_cache_warms_mid_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold-then-warm: a grant landing mid-launch must not flip the session on."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    _write_permissions_cache(tmp_path, f'"{asset}" {{\n    ReadApplicationState\n}}\n')
    real_grants_cached = desktop_plugin.broker_grants_cached

    def warm_after_first_read(wasm: Path) -> bool:
        result = real_grants_cached(wasm)
        _write_permissions_cache(tmp_path, f'"{asset}" {{\n{FULL_PERMS}}}\n')
        return result

    monkeypatch.setattr(desktop_plugin, "broker_grants_cached", warm_after_first_read)
    _config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert "TDNEXT_NAVIGATOR_BROKER" not in env
    assert "TDNEXT_NAVIGATOR_HEADER" not in env
    assert 'active "false"' in layout
    assert 'active "true"' not in layout


def test_env_and_layout_share_snapshot_when_asset_vanishes_mid_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pane and flag keep the startup asset even if it disappears mid-launch."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    _write_permissions_cache(tmp_path, f'"{asset}" {{\n{FULL_PERMS}}}\n')
    real_asset_path = desktop_plugin.broker_asset_path
    calls = 0

    def vanish_after_first_snapshot() -> Path | None:
        nonlocal calls
        calls += 1
        return real_asset_path() if calls == 1 else None

    monkeypatch.setattr(desktop_plugin, "broker_asset_path", vanish_after_first_snapshot)
    _config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert env["TDNEXT_NAVIGATOR_BROKER"] == "1"
    assert layout.count("navigator-broker.wasm") == 1
    assert 'active "true"' in layout


def test_host_emitted_bare_key_warms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact bare-path entry the host writes on a real grant warms the broker."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = _host_entry(str(asset), FULL_PERMS) + _host_entry("some-other-plugin", "    OpenFiles\n")
    assert _grants_cached(tmp_path, monkeypatch, text) is True


def test_file_prefixed_key_does_not_warm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = _host_entry("some-other-plugin", "    OpenFiles\n") + _host_entry(f"file:{asset}", FULL_PERMS)
    assert _grants_cached(tmp_path, monkeypatch, text) is False


@pytest.mark.parametrize(
    ("bare_perms", "located_perms", "expected"),
    [
        (FULL_PERMS, None, True),
        (None, FULL_PERMS, False),
        (FULL_PERMS, FULL_PERMS, True),
        (FULL_PERMS, _PARTIAL_PERMS, True),
        (_PARTIAL_PERMS, FULL_PERMS, False),
        (_PARTIAL_PERMS, _PARTIAL_PERMS, False),
        (_PARTIAL_PERMS, None, False),
        (None, _PARTIAL_PERMS, False),
        (None, None, False),
    ],
)
def test_bare_and_file_prefixed_key_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bare_perms: str | None, located_perms: str | None, expected: bool
) -> None:
    """Only a full bare-path grant warms, regardless of the file: entry."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = ""
    if bare_perms is not None:
        text += _host_entry(str(asset), bare_perms)
    if located_perms is not None:
        text += _host_entry(f"file:{asset}", located_perms)
    assert _grants_cached(tmp_path, monkeypatch, text) is expected


def test_file_prefixed_key_requires_exact_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Near-miss location keys never warm, even with full permissions."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    for key in (
        f"{asset}.bak",
        f"file:{asset}.bak",
        f"file:{asset} ",
        f"file:file:{asset}",
        f"FILE:{asset}",
        f"file:{asset.parent}/./{asset.name}",
    ):
        assert _grants_cached(tmp_path, monkeypatch, _host_entry(key, FULL_PERMS)) is False


@pytest.mark.parametrize(
    "body",
    [
        "commented-block",
        "nested-key",
        "perms-in-comments",
    ],
)
def test_file_prefixed_malformed_blocks_stay_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """The strict parser guards the file:-prefixed form exactly like the bare form."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    located = f"file:{asset}"
    if body == "commented-block":
        text = (
            f'// "{located}" {{\n'
            "//     ReadApplicationState\n"
            "//     ChangeApplicationState\n"
            "//     Reconfigure\n"
            "//     ReadCliPipes\n"
            "// }\n"
        )
    elif body == "nested-key":
        text = f'"other" {{\n    "{located}" {{\n{FULL_PERMS}    }}\n}}\n'
    else:
        text = (
            f'"{located}" {{\n'
            "    // ReadApplicationState\n"
            "    // ChangeApplicationState\n"
            "    // Reconfigure\n"
            "    // ReadCliPipes\n"
            "}\n"
        )
    assert _grants_cached(tmp_path, monkeypatch, text) is False


def test_file_prefixed_duplicate_last_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate file:-prefixed entries follow host last-wins per key."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    full = _host_entry(f"file:{asset}", FULL_PERMS)
    partial = _host_entry(f"file:{asset}", _PARTIAL_PERMS)
    assert desktop_plugin._parse_permission_cache(partial + full) == {
        f"file:{asset}": list(desktop_plugin.BROKER_PERMISSIONS)
    }
    assert _grants_cached(tmp_path, monkeypatch, partial + full) is False
    assert _grants_cached(tmp_path, monkeypatch, full + partial) is False


def test_cache_key_escapes_decode_exactly() -> None:
    """KDL string escapes in keys decode before the exact-path comparison."""
    parse = desktop_plugin._parse_permission_cache
    assert parse('"a\\"b" {\n    ReadApplicationState\n}\n') == {'a"b': ["ReadApplicationState"]}
    assert parse('"a\\\\b" {\n    ReadApplicationState\n}\n') == {"a\\b": ["ReadApplicationState"]}
    assert parse('"a\\u{62}c" {\n    ReadApplicationState\n}\n') == {"abc": ["ReadApplicationState"]}
    assert parse('"a b" {\n    ReadApplicationState\n}\n') == {"a b": ["ReadApplicationState"]}


def test_cache_key_bad_escapes_fail_closed() -> None:
    """Unsupported escapes and unterminated keys fail the whole document."""
    parse = desktop_plugin._parse_permission_cache
    assert parse('"a\\qb" {\n    ReadApplicationState\n}\n') is None
    assert parse('"abc {\n    ReadApplicationState\n}\n') is None
    assert parse('"a\\u{110000}b" {\n    ReadApplicationState\n}\n') is None


@pytest.mark.parametrize("bad", [r'"\u{-1}"', r'"\u{D800}"', r'"\u{DFFF}"'])
def test_invalid_unicode_escape_poisons_full_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    assert not _grants_cached(tmp_path, monkeypatch, _host_entry(str(asset), FULL_PERMS) + f"{bad} {{}}\n")


def test_invalid_utf8_cache_stays_cold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    _write_permissions_cache(tmp_path, _host_entry(str(asset), FULL_PERMS))
    cache = tmp_path / "state" / "zellij-cache" / "zellij" / "permissions.kdl"
    cache.write_bytes(cache.read_bytes() + b"\xff")
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(tmp_path / "state"))
    assert not desktop_plugin.broker_grants_cached(asset)


def test_adjacent_blocks_without_terminator_poison_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    text = _host_entry(str(asset), FULL_PERMS).rstrip() + ' "other" {}\n'
    assert not _grants_cached(tmp_path, monkeypatch, text)


def test_escaped_duplicate_key_last_wins() -> None:
    """Duplicates match on the decoded key, so an escaped repeat overwrites."""
    parse = desktop_plugin._parse_permission_cache
    text = (
        '"ab" {\n    ReadApplicationState\n}\n'
        '"a\\u{62}" {\n    ReadApplicationState\n    ChangeApplicationState\n    Reconfigure\n    ReadCliPipes\n}\n'
    )
    assert parse(text) == {"ab": ["ReadApplicationState", "ChangeApplicationState", "Reconfigure", "ReadCliPipes"]}


def test_env_and_layout_share_snapshot_with_file_prefixed_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file:-only grant remains cold throughout the launch snapshot."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    _write_permissions_cache(tmp_path, _host_entry(f"file:{asset}", FULL_PERMS))
    real_grants_cached = desktop_plugin.broker_grants_cached

    def cool_after_first_read(wasm: Path) -> bool:
        result = real_grants_cached(wasm)
        _write_permissions_cache(tmp_path, "")
        return result

    monkeypatch.setattr(desktop_plugin, "broker_grants_cached", cool_after_first_read)
    _config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert "TDNEXT_NAVIGATOR_BROKER" not in env
    assert 'active "false"' in layout


def test_env_and_layout_share_snapshot_when_asset_appears_mid_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An asset landing mid-launch must not sneak a broker pane into a legacy session."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    _write_permissions_cache(tmp_path, f'"{asset}" {{\n{FULL_PERMS}}}\n')
    real_asset_path = desktop_plugin.broker_asset_path
    calls = 0

    def appear_after_first_snapshot() -> Path | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else real_asset_path()

    monkeypatch.setattr(desktop_plugin, "broker_asset_path", appear_after_first_snapshot)
    _config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("preferences",))

    assert "TDNEXT_NAVIGATOR_BROKER" not in env
    assert "TDNEXT_NAVIGATOR_HEADER" not in env
    assert "navigator-broker.wasm" not in layout
