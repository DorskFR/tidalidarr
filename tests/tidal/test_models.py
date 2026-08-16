import base64
import json
from pathlib import Path

import pytest

from tidalidarr.tidal.models import (
    AudioQuality,
    TidalAlbum,
    TidalArtist,
    TidalInvalidStreamError,
    TidalModel,
    TidalSearchResult,
    TidalStream,
    TidalTrack,
    check_flac_magic,
)


@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("tests/tidal/data/album.json", TidalAlbum),
        ("tests/tidal/data/artist.json", TidalArtist),
        ("tests/tidal/data/track.json", TidalTrack),
        ("tests/tidal/data/search.json", TidalSearchResult),
        ("tests/tidal/data/stream.json", TidalStream),
    ],
)
def test_parse(path: str, model: type[TidalModel]) -> None:
    with Path(path).open(mode="r", encoding="utf-8") as p:
        content = json.load(p)
    parsed = model(**content)
    assert isinstance(parsed, model)


def _stream(quality: AudioQuality, codecs: str, encryption_type: str = "NONE") -> TidalStream:
    with Path("tests/tidal/data/stream.json").open(mode="r", encoding="utf-8") as p:
        content = json.load(p)
    manifest = json.loads(base64.b64decode(content["manifest"]))
    manifest |= {"codecs": codecs, "encryptionType": encryption_type}
    content |= {"audioQuality": quality, "manifest": base64.b64encode(json.dumps(manifest).encode()).decode()}
    return TidalStream(**content)


@pytest.mark.parametrize("quality", [AudioQuality.LOSSLESS, AudioQuality.HI_RES_LOSSLESS])
def test_check_flac_codec_accepts_lossless_flac(quality: AudioQuality) -> None:
    _stream(quality, "flac").check_flac_codec()


@pytest.mark.parametrize(
    ("quality", "codecs", "encryption_type"),
    [
        (AudioQuality.HIGH, "mp4a.40.2", "NONE"),
        (AudioQuality.LOW, "mp4a.40.5", "NONE"),
        (AudioQuality.LOSSLESS, "mp4a.40.2", "NONE"),
        (AudioQuality.LOSSLESS, "flac", "OLD_AES"),
    ],
)
def test_check_flac_codec_rejects_downgraded_streams(quality: AudioQuality, codecs: str, encryption_type: str) -> None:
    with pytest.raises(TidalInvalidStreamError):
        _stream(quality, codecs, encryption_type).check_flac_codec()


def test_check_flac_magic() -> None:
    check_flac_magic(b"fLaC\x00\x00\x00\x22", track_id=1)
    with pytest.raises(TidalInvalidStreamError):
        check_flac_magic(b"\x00\x00\x00\x1cftypisom", track_id=1)


def test_parse_search_result_with_empty_image_fields() -> None:
    with Path("tests/tidal/data/search.json").open(mode="r", encoding="utf-8") as p:
        content = json.load(p)
    content["artists"]["items"][0]["picture"] = ""
    content["albums"]["items"][0]["cover"] = ""

    parsed = TidalSearchResult(**content)

    assert parsed.artists[0].picture is None
    assert parsed.albums[0].cover is None


def test_album_without_cover_has_no_cover_urls() -> None:
    with Path("tests/tidal/data/album.json").open(mode="r", encoding="utf-8") as p:
        content = json.load(p)
    content["cover"] = ""

    assert TidalAlbum(**content).cover_urls == []
