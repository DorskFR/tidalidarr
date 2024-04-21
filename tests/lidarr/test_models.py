import json
from pathlib import Path

from tidalidarr.lidarr.models import LidarrMissingTrack


def test_parse_missing_tracks() -> None:
    with Path("tests/lidarr/data/missing_tracks.json").open(mode="r", encoding="utf-8") as p:
        content = json.load(p)
    parsed = [LidarrMissingTrack(**item) for item in content]
    assert isinstance(parsed, list)
