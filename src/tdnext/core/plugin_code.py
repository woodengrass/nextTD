from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class InstalledCode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    package: str = Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
    path: Path
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")
    is_package: bool
