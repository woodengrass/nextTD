"""Validated plugin manifests and persisted state."""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .plugin_code import InstalledCode

PluginRole = Literal["desktop.host", "desktop.view", "icons.provider"]
ROLES: tuple[PluginRole, ...] = ("desktop.host", "desktop.view", "icons.provider")


class PluginManifest(BaseModel):
    """One trusted local plugin, read from its TOML manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    entrypoint: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
    placement: Literal["sidebar", "header"] = "sidebar"
    icon: str | None = Field(default=None, min_length=1, max_length=4)
    icon_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]*$")
    shortcuts: str | None = None
    settings_contributor: bool | None = None
    capabilities: tuple[str, ...]
    requires_plugins: tuple[str, ...] = ()
    requires_executables: tuple[str, ...] = ()
    manifest_version: Literal[1]
    api_version: Literal[1]
    host_interfaces: tuple[str, ...] = ()
    view_interfaces: tuple[str, ...] = ()
    dependency_hook: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
    source_dir: Path | None = Field(default=None, exclude=True)
    code: InstalledCode | None = None

    @field_validator("host_interfaces", "view_interfaces")
    @classmethod
    def interfaces_are_unique_names(cls, interfaces: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(interfaces)) != len(interfaces) or any(
            not re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+", interface)
            for interface in interfaces
        ):
            raise ValueError("interfaces must be unique dotted names")
        return interfaces

    @field_validator("icon")
    @classmethod
    def icon_is_terminal_text(cls, icon: str | None) -> str | None:
        if icon is not None and any(
            not ("!" <= char <= "~" or "\u2500" <= char <= "\u259f" or char in "▤▥") for char in icon
        ):
            raise ValueError("icon must use printable ASCII or standard terminal drawing glyphs")
        return icon

    @field_validator("capabilities")
    @classmethod
    def capabilities_have_one_role(cls, capabilities: tuple[str, ...]) -> tuple[str, ...]:
        if not capabilities or len(set(capabilities)) != len(capabilities):
            raise ValueError("capabilities must be nonempty and unique")
        for capability in capabilities:
            if not re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+", capability):
                raise ValueError(f"invalid capability: {capability}")
        if sum(role in capabilities for role in ROLES) != 1:
            raise ValueError("capabilities must declare exactly one structural role")
        return capabilities

    @property
    def role(self) -> PluginRole:
        for role in ROLES:
            if role in self.capabilities:
                return role
        raise AssertionError("validated manifest has no structural role")


class PluginState(BaseModel):
    """Immutable snapshot: enabled plugins must also be installed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installed: frozenset[str] = frozenset()
    enabled: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def enabled_are_installed(self) -> "PluginState":
        if not self.enabled <= self.installed:
            msg = "enabled plugins must be installed"
            raise ValueError(msg)
        return self
