import base64
import json
from datetime import date
from enum import Enum, StrEnum, auto
from functools import cached_property
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import mutagen
import mutagen.flac
import mutagen.id3
from pydantic import BaseModel, ConfigDict, EmailStr, HttpUrl, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict

from tidalidarr.utils import sanitize

#
# Errors
#


class TidalAuthenticationError(Exception):
    """Raised when an authentication method has failed"""


class TidalLoginWithDeviceFailedError(TidalAuthenticationError):
    """Raised when logging in failed"""


class TidalAllAuthenticationFailedError(TidalAuthenticationError):
    """Raised when all authentication methods have failed"""


#
# Enums
#


class AuthState(Enum):
    ACCESS_TOKEN_EXPIRED = auto()
    ACCESS_TOKEN_REFRESHED = auto()
    LOGGED_IN = auto()
    REFRESH_TOKEN_EXPIRED = auto()
    SUBSCRIPTION_EXPIRED = auto()
    TOKEN_PRESENT = auto()
    UNAUTHENTICATED = auto()


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
    sleep_between_downloads: float = 5
    sleep_between_requests: float = 2
    token_path: Path = Path("token.json")
    test_track_id: int = 286926336


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
    picture: UUID | None = None


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
    release_date: date | None = None
    cover_bytes: bytes | None = None

    @cached_property
    def release_date_str(self) -> str:
        return self.release_date.strftime("%Y-%m-%d") if self.release_date else ""

    @cached_property
    def folder(self) -> Path:
        return Path(sanitize(next(iter(self.artists)).name)) / sanitize(self.title)

    @cached_property
    def cover_urls(self) -> list[HttpUrl]:
        cover_path = str(self.cover).replace("-", "/")
        return [
            HttpUrl(f"https://resources.tidal.com/images/{cover_path}/{size}x{size}.jpg") for size in [640, 320, 160]
        ]


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
    album_replay_gain: float | None = None
    album_peak_amplitude: float | None = None
    track_replay_gain: float | None = None
    track_peak_amplitude: float | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None

    @cached_property
    def decoded_manifest(self) -> TidalStreamManifest:
        return TidalStreamManifest(**json.loads(base64.b64decode(self.manifest).decode("utf-8")))

    @cached_property
    def url(self) -> HttpUrl:
        return next(iter(self.decoded_manifest.urls))


class TidalTrack(TidalModel):
    id: int
    title: str
    duration: int
    replay_gain: float
    peak: float
    allow_streaming: bool
    track_number: int
    volume_number: int
    bpm: int | None = None
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
        stream: TidalStream,
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
        metadata.tags["replaygain_track_gain"] = f"{(self.replay_gain or stream.track_replay_gain):.8f} dB"
        metadata.tags["replaygain_track_peak"] = f"{self.peak or stream.track_peak_amplitude:.8f}"
        if stream.album_replay_gain:
            metadata.tags["replaygain_album_gain"] = f"{stream.album_replay_gain:.8f} dB"
        if stream.album_peak_amplitude:
            metadata.tags["replaygain_album_peak"] = f"{stream.album_peak_amplitude:.8f}"

        if cover_bytes and isinstance(metadata, mutagen.flac.FLAC):
            flac_cover = mutagen.flac.Picture()
            flac_cover.type = mutagen.id3.PictureType.COVER_FRONT
            flac_cover.data = cover_bytes
            flac_cover.mime = "image/jpeg"
            metadata.clear_pictures()
            metadata.add_picture(flac_cover)

        if lyrics:
            metadata.tags["lyrics"] = lyrics

        metadata.save()


class TidalSearchResult(TidalModel):
    artists: list[TidalArtist]
    albums: list[TidalAlbum]
    tracks: list[TidalTrack]
    top_hit: dict[str, Any] | None = None

    @model_validator(mode="before")
    @staticmethod
    def extract_items(values: dict[str, Any]) -> dict[str, Any]:
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


class TidalQueueInformation(BaseModel):
    albums: list[TidalAlbum]
    albums_count: int
    ready: list[Path]
    ready_count: int
    not_found: list[str]
    not_found_count: int
