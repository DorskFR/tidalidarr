from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict


class LidarrConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="lidarr_")

    api_url: HttpUrl = HttpUrl("http://127.0.0.1:8686/api/v1")
    api_key: str
    download_path: Path = Path("/downloads")


class LidarrModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class LidarrMissingTrack(LidarrModel):
    path: str
    artist_id: int
    album_id: int
    album_release_id: int
    track_ids: list[int]
    quality: dict[str, Any]
    disable_release_switching: bool = False

    @model_validator(mode="before")
    @staticmethod
    def extract_items(values: dict[str, Any]) -> dict[str, Any]:
        values["artistId"] = values["artist"]["id"]
        values["albumId"] = values["album"]["id"]
        values["trackIds"] = [track["id"] for track in values["tracks"]]
        return values
