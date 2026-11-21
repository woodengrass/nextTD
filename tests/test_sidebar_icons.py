from pathlib import Path

import pytest
from pydantic import ValidationError
from test_settings_ui import make_app

from tdnext.core.models import PluginManifest


def test_manifest_icon_is_optional_and_safe(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    base = manager.manifests["files"].model_dump(exclude={"icon"})
    assert PluginManifest.model_validate(base).icon is None
    for invalid in ("", " ", "a\nb", "\x1b[31m", "💻", "\u200b", "abcde"):
        with pytest.raises(ValidationError):
            PluginManifest.model_validate({**base, "icon": invalid})
    assert PluginManifest.model_validate({**base, "icon": ">_"}).icon == ">_"
