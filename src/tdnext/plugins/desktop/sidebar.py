"""Plain-text labels shared by native and fallback navigation."""

from rich.cells import cell_len

from tdnext.core.models import PluginManifest
from tdnext.core.session import SessionSnapshot


def sidebar_label(manifest: PluginManifest, session: SessionSnapshot | None = None) -> str:
    icon = (session.icon_for(manifest.id) if session is not None else None) or manifest.icon or "[]"
    return f"{icon}{' ' * max(0, 4 - cell_len(icon))} {manifest.name}"
