import json
from pathlib import Path

import pytest

from tidalidarr.tidal.models import (
    TidalAlbum,
    TidalArtist,
    TidalModel,
    TidalSearchResult,
    TidalTrack,
)


@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("tests/tidal/data/album.json", TidalAlbum),
        ("tests/tidal/data/artist.json", TidalArtist),
        ("tests/tidal/data/track.json", TidalTrack),
        ("tests/tidal/data/search.json", TidalSearchResult),
    ],
)
def test_parse(path: str, model: type[TidalModel]) -> None:
    with Path(path).open(mode="r", encoding="utf-8") as p:
        content = json.load(p)
    parsed = model(**content)
    assert isinstance(parsed, model)
