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
    def __init__(self, config: LidarrConfig, session: Session | None = None) -> None:
        self._config = config
        self._session = session or Session()

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
        """
        Call Lidarr API to obtain all the missing releases.
        While paginating, we yield one string at a time to query Tidal.
        The query string is composed of the artist and the release title.
        """
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
        """
        This is the automatic scanning feature of Lidarr.
        However it does not seem to trigger imports most of the time...
        """
        url = f"{self._config.api_url}/command"
        path = self._config.download_path / folder
        payload = {"name": "DownloadedAlbumsScan", "path": path.as_posix()}
        self._post(url, payload=payload)

    def manual_import(self, folder: Path) -> None:
        """
        The first call is the same as clicking "manual import" in Lidarr's web UI
        We use it by passing a specific release path so that we only scan the tracks of a single release at at time.
        With this call, Lidarr does some scanning of the files and returns its best guess based on the folder name.
        If properly recognized, we trigger the actual import.
        """
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
        """
        This triggers the actual import of tracks by Lidarr.
        We use the output of the previous command and return to Lidarr the list of tracks validated for import.
        """
        url = f"{self._config.api_url}/command"
        payload = {
            "name": "ManualImport",
            "files": [mt.model_dump(by_alias=True) for mt in missing_tracks],
            "importMode": "move",
            "replaceExistingFiles": False,
        }
        self._post(url, payload=payload)

    def cleanup_download_folder(self) -> None:
        """
        Clean up the download folder after import has completed.
        Using rmdir only empty folders are deleted.
        """
        paths = [(root / name) for root, dirs, _ in self._config.download_path.walk(top_down=False) for name in dirs]
        for path in paths:
            with suppress(OSError):
                path.rmdir()
                logger.info(f"Deleted empty directory: {path}")
