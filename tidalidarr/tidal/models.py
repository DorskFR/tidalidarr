import base64
import json
from datetime import date
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any, Generic, Literal, TypedDict, TypeVar
from uuid import UUID

import mutagen
import mutagen.flac
import mutagen.id3
from pydantic import BaseModel, ConfigDict, EmailStr, HttpUrl, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict

#
# Constants
#

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#
# Types
#
T = TypeVar("T")

#
# Errors
#


class TidalLoginFailedError(Exception):
    """Raised when logging in failed"""


#
# Enums
#


class AudioQuality(StrEnum):
    HI_RES = "HI_RES"
    HIGH = "HIGH"
    LOSSLESS = "LOSSLESS"
    LOW = "LOW"


class PlaybackMode(StrEnum):
    STREAM = "STREAM"
    OFFLINE = "OFFLINE"


class AssetPresentation(StrEnum):
    FULL = "FULL"
    PREVIEW = "PREVIEW"


class AudioMode(StrEnum):
    STEREO = "STEREO"
    DOLBY_ATMOS = "DOLBY_ATMOS"


#
# Models
#


class TidalConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="tidal_")

    api_hifi_url: HttpUrl = HttpUrl("https://api.tidalhifi.com/v1")
    api_v1_url: HttpUrl = HttpUrl("https://api.tidal.com/v1")
    auth_url: HttpUrl = HttpUrl("https://auth.tidal.com/v1/oauth2")
    check_interval: int = 3600
    client_id: str = "zU4XHVVkc2tDPo4t"
    client_secret: str = "VJKhDFqJPqvsPVNBV6ukXTJmwlvbttP7wlMlrc72se4="
    country_code: str = "US"
    download_path: Path = Path("/downloads")
    lyrics_v1_url: HttpUrl = HttpUrl("https://listen.tidal.com/v1")
    sleep_between_downloads: bool = True
    token_path: Path = Path("token.json")


class TidalModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TidalDeviceAuth(TidalModel):
    device_code: str
    expires_in: int
    interval: int
    user_code: str
    verification_uri_complete: str
    verification_uri: str


class TidalUser(TidalModel):
    country_code: str
    email: EmailStr
    user_id: int
    username: str


class TidalToken(TidalModel):
    access_token: str
    client_name: str
    expires_in: int
    refresh_token: str
    scope: str
    token_type: str
    user_id: int
    user: TidalUser


class TidalArtistStub(TidalModel):
    id: int
    name: str
    picture: UUID | None


class TidalArtist(TidalArtistStub):
    url: HttpUrl


class TidalAlbumStub(TidalModel):
    id: int
    title: str
    cover: UUID


class TidalAlbum(TidalAlbumStub):
    duration: int
    allow_streaming: bool
    number_of_tracks: int
    url: HttpUrl
    audio_quality: AudioQuality
    audio_modes: list[AudioMode]
    artists: list[TidalArtistStub]
    release_date: date | None
    cover_bytes: bytes | None = None

    @cached_property
    def release_date_str(self) -> str:
        return self.release_date.strftime("%Y-%m-%d") if self.release_date else ""

    @cached_property
    def folder(self) -> Path:
        return Path(next(iter(self.artists)).name) / self.title

    @cached_property
    def cover_urls(self) -> list[HttpUrl]:
        cover_path = str(self.cover).replace("-", "/")
        return [
            HttpUrl(f"https://resources.tidal.com/images/{cover_path}/{size}x{size}.jpg") for size in [640, 320, 160]
        ]


class TidalTrack(TidalModel):
    id: int
    title: str
    duration: int
    replay_gain: float
    peak: float
    allow_streaming: bool
    track_number: int
    volume_number: int
    bpm: int | None
    url: str
    isrc: str
    audio_quality: AudioQuality
    audio_modes: list[AudioMode]
    media_metadata: dict[Literal["tags"], list[str]]
    artists: list[TidalArtistStub]
    album: TidalAlbumStub

    @cached_property
    def name(self) -> str:
        # TODO: we might consider another extension if we are not using flac
        # But for this first version we only want to consider LOSSLESS quality
        return f"{self.track_number:02d} - {self.title}.flac".replace("/", "-")

    @cached_property
    def artist(self) -> str:
        return next(iter(self.artists)).name

    def save_metadata(
        self,
        album: TidalAlbum,
        filename: str,
        cover_bytes: bytes | None = None,
        lyrics: str | None = None,
    ) -> None:
        metadata = mutagen.File(filename)

        if not metadata.tags:
            metadata.add_tags()

        metadata.tags["title"] = self.title
        metadata.tags["album"] = album.title
        metadata.tags["albumartist"] = self.artist
        metadata.tags["artist"] = self.artist
        metadata.tags["tracknumber"] = str(self.track_number)
        metadata.tags["tracktotal"] = str(album.number_of_tracks)
        metadata.tags["date"] = album.release_date_str
        metadata.tags["isrc"] = self.isrc
        metadata.tags["replaygain_track_gain"] = f"{self.replay_gain:.8f} dB"
        metadata.tags["replaygain_track_peak"] = f"{self.peak:.8f}"

        if cover_bytes:
            flac_cover = mutagen.flac.Picture()
            flac_cover.type = mutagen.id3.PictureType.COVER_FRONT
            flac_cover.data = cover_bytes
            flac_cover.mime = "image/jpeg"
            metadata.clear_pictures()
            metadata.add_picture(flac_cover)

        if lyrics:
            metadata.tags["lyrics"] = lyrics

        metadata.save()


class TidalStreamManifest(TidalModel):
    mime_type: str
    codecs: str
    encryption_type: str
    urls: list[HttpUrl]


class TidalStream(TidalModel):
    track_id: int
    asset_presentation: AssetPresentation
    audio_mode: AudioMode
    audio_quality: AudioQuality
    manifest_mime_type: str
    manifest_hash: str
    manifest: str
    album_replay_gain: float
    album_peak_amplitude: float
    track_replay_gain: float
    track_peak_amplitude: float
    bit_depth: int
    sample_rate: int

    @cached_property
    def decoded_manifest(self) -> TidalStreamManifest:
        return TidalStreamManifest(**json.loads(base64.b64decode(self.manifest).decode("utf-8")))

    @cached_property
    def url(self) -> HttpUrl:
        return next(iter(self.decoded_manifest.urls))


class SearchCategory(TypedDict, Generic[T]):
    limit: int
    offset: int
    total_number_of_items: int
    items: list[T]


class TidalSearchResult(TidalModel):
    artists: list[TidalArtist]
    albums: list[TidalAlbum]
    tracks: list[TidalTrack]
    top_hit: dict[str, Any] | None

    @model_validator(mode="before")
    @classmethod
    def extract_items(cls, values: dict[str, Any]) -> dict[str, Any]:
        values["artists"] = values.get("artists", {}).get("items", [])
        values["albums"] = values.get("albums", {}).get("items", [])
        values["tracks"] = values.get("tracks", {}).get("items", [])
        return values

    @property
    def top_hit_id(self) -> int | None:
        if not self.top_hit:
            return None
        if self.top_hit.get("type") != "ALBUMS":
            return None
        return self.top_hit.get("value", {}).get("id", None)
