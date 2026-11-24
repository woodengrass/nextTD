import pytest
from golden_desktop import SCENES, TERMINALS, scene


@pytest.mark.parametrize('term', TERMINALS)
@pytest.mark.parametrize('name', SCENES)
def test_desktop_matches_frozen_golden_through_archive(name: str, term: str) -> None:
    scene(name, term)
