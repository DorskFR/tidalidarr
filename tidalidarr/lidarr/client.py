import logging
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import HttpUrl
from requests import HTTPError, Response, Session
from tenacity import (
    after_log,
    retry,
    retry_if_exception_type,
    stop_after_delay,
    wait_fixed,
)

from tidalidarr.lidarr.models import LidarrConfig, LidarrMissingTrack

logger = logging.getLogger(__name__)


class LidarrClient:
    def __init__(self, config: LidarrConfig, session: Session) -> None:
        self._config = config
        self._session = session

    @retry(
        wait=wait_fixed(30),
        retry=retry_if_exception_type(HTTPError),
        stop=stop_after_delay(300),
        after=after_log(logger, logging.WARNING),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        url: HttpUrl | str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Response:
        response = self._session.request(method, str(url), params=params, json=payload, timeout=10)
        response.raise_for_status()
        return response

    def _get(self, url: HttpUrl | str, params: dict[str, Any] | None = None) -> Response:
        return self._request("GET", url, params={"apikey": self._config.api_key} | (params or {}))

    def _post(
        self, url: HttpUrl | str, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None
    ) -> Response:
        return self._request(
            "POST",
            url,
            params={"apikey": self._config.api_key} | (params or {}),
            payload=payload,
        )

    def get_missing_albums(self) -> Iterator[str]:
        page_size = 10
        url = f"{self._config.api_url}/wanted/missing"
        params: dict[str, Any] = {
            "page": 1,
            "pagesize": page_size,
            "sortKey": "releaseDate",
            "sortDirection": "descending",
        }
        while True:
            content = self._get(url, params=params).json()
            records = content["records"]
            for record in records:
                if record["albumType"] != "Album":
                    continue
                if record["grabbed"]:
                    continue
                yield f'{record["artist"]["artistName"]} {record["title"]}'
            if len(records) < page_size:
                break
            params["page"] += 1

    def trigger_import(self, folder: Path) -> None:
        url = f"{self._config.api_url}/command"
        path = self._config.download_path / folder
        payload = {"name": "DownloadedAlbumsScan", "path": path.as_posix()}
        self._post(url, payload=payload)

    def manual_import(self, folder: Path) -> None:
        url = f"{self._config.api_url}/manualimport"
        path = self._config.download_path / folder
        params = {
            "artistId": 0,
            "folder": path.as_posix(),
            "filterExistingFiles": True,
            "replaceExistingFiles": False,
        }
        content = self._get(url, params=params).json()

        missing_tracks: list[LidarrMissingTrack] = []
        for track in content:
            if track["rejections"]:
                logger.warn(f"Not importing {path}, tracks have rejections")
                return
            try:
                missing_tracks.append(LidarrMissingTrack(**track))
            except ValueError:
                logger.warn(f"Not importing {path}, tracks have missing fields")
                return
        self._manual_import(missing_tracks)

    def _manual_import(self, missing_tracks: list[LidarrMissingTrack]) -> None:
        url = f"{self._config.api_url}/command"
        payload = {
            "name": "ManualImport",
            "files": [mt.model_dump(by_alias=True) for mt in missing_tracks],
            "importMode": "move",
            "replaceExistingFiles": False,
        }
        self._post(url, payload=payload)

    def cleanup_download_folder(self) -> None:
        paths = [(root / name) for root, dirs, _ in self._config.download_path.walk(top_down=False) for name in dirs]
        for path in paths:
            with suppress(OSError):
                path.rmdir()
                logger.info(f"Deleted empty directory: {path}")
