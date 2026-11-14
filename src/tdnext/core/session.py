"""Immutable view and icon snapshot inherited by one native desktop session."""

import base64
import binascii
import os

from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import PluginError
from .models import PluginManifest

SESSION_ENV = "TDNEXT_SESSION_SNAPSHOT"


class SessionView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: PluginManifest
    icon: str


class SessionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    views: tuple[SessionView, ...]

    def icon_for(self, plugin_id: str) -> str | None:
        return next((view.icon for view in self.views if view.manifest.id == plugin_id), None)


def session_token(session: SessionSnapshot) -> str:
    return base64.urlsafe_b64encode(session.model_dump_json().encode("utf-8")).decode("ascii")


def current_session(token: str | None = None) -> SessionSnapshot | None:
    if token is None:
        raw = os.environ.get(SESSION_ENV)
    else:
        try:
            raw = base64.b64decode(token, altchars=b"-_", validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError, ValueError) as exc:
            raise PluginError(f"Invalid desktop session snapshot: {exc}") from exc
    if raw is None:
        return None
    try:
        return SessionSnapshot.model_validate_json(raw)
    except ValidationError as exc:
        raise PluginError(f"Invalid desktop session snapshot: {exc}") from exc
