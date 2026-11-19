import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import InstallationConsentRequired, PluginError
from .models import PluginManifest
from .plugin_imports import RUNTIME_ENV, ImportRoutes


class ValidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifests: tuple[PluginManifest, ...]
    order: tuple[str, ...]
    state_path: Path
    local_bin: Path
    approved: tuple[tuple[str, str], ...] = ()


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    error: str | None = None
    actions: tuple[tuple[str, str], ...] = ()


def validate_install(request: ValidationRequest) -> None:
    routes = ImportRoutes(packages={
        item.code.package: item.code for item in request.manifests if item.code is not None
    })
    env = {**os.environ, RUNTIME_ENV: routes.model_dump_json(), "TDNEXT_STATE_DIR": str(request.state_path.parent)}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tdnext.core.plugin_validation"], input=request.model_dump_json(),
            text=True, capture_output=True, check=False, timeout=180, env=env,
        )
        if result.returncode:
            raise PluginError(f"Plugin validation failed: {result.stderr.strip()}")
        response = ValidationResult.model_validate_json(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValidationError) as exc:
        raise PluginError(f"Cannot validate local plugin installation: {exc}") from exc
    if response.actions:
        raise InstallationConsentRequired(response.actions)
    if response.error is not None:
        raise PluginError(response.error)


def main() -> None:
    from .local_env import bootstrap
    from .manager import PluginManager
    from .state import StateStore

    request = ValidationRequest.model_validate_json(sys.stdin.read())
    manager = PluginManager(
        {item.id: item for item in request.manifests}, StateStore(request.state_path), request.local_bin,
    )
    bootstrap(request.state_path.parent)
    try:
        with redirect_stdout(sys.stderr):
            manager.check_dependencies(request.order, fetch=True, approved=request.approved)
        response = ValidationResult()
    except InstallationConsentRequired as exc:
        response = ValidationResult(actions=exc.actions)
    except Exception as exc:  # noqa: BLE001 - subprocess boundary reports trusted plugin failures to the parent
        response = ValidationResult(error=str(exc))
    print(response.model_dump_json())


if __name__ == "__main__":
    main()
