import logging
import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from random import randrange
from typing import Any

from pydantic import HttpUrl
from requests import Response, Session
from requests.auth import AuthBase
from requests.exceptions import HTTPError

from tidalidarr.tidal.auth import TidalAuth
from tidalidarr.tidal.models import (
    USER_AGENT,
    AssetPresentation,
    AudioQuality,
    PlaybackMode,
    TidalAlbum,
    TidalConfig,
    TidalSearchResult,
    TidalStream,
    TidalTrack,
)

logger = logging.getLogger(__name__)


class TidalClient:
    def __init__(self, config: TidalConfig, session: Session | None = None) -> None:
        self._config = config
        self._session = session or Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._auth = TidalAuth(self._config, self._session)
        self._not_found: dict[str, float] = {}

    def _request(
        self, url: HttpUrl | str, params: dict[str, Any] | None = None, auth: AuthBase | None = None
    ) -> Response:
        response = self._session.get(str(url), params=params, auth=auth, timeout=10)
        response.raise_for_status()
        time.sleep(1)  # TODO: cheap rate limit to avoid 429
        return response

    def _authenticated_request(self, url: HttpUrl | str, params: dict[str, Any] | None = None) -> Response:
        return self._request(
            url,
            params={"countryCode": self._config.country_code} | (params or {}),
            auth=self._auth,
        )

    def _search(self, query: str) -> TidalSearchResult:
        """
        Trigger a search to Tidal's API using a query string built with Artist + release name
        If found, we return the search model with all the parsed fields.
        """
        logger.info(f"Searching for: {query}")
        params = {"query": query, "countryCode": self._config.country_code}
        url = f"{self._config.api_hifi_url}/search"
        content = self._authenticated_request(url, params=params).json()
        result = TidalSearchResult(**content)
        logger.info(
            f"Found: {len(result.artists)} artists and {len(result.albums)} albums "
            f"{'with' if result.top_hit else 'without'} a top hit."
        )
        return result

    def search(self, query: str) -> Path | None:
        """
        This function pilots the Tidal logic:
        - Search for a query string (filtering recent queries that failed)
        - If found an Album in the search result we try to download it
        - If successful, return the path where the Album was saved
        """
        if query in self._not_found and (time.time() - self._not_found[query]) < self._config.check_interval:
            return None
        search_result = self._search(query)
        if not (album_id := search_result.top_hit_id):
            logging.warn(f"Could not find an album for: {query}")
            self._not_found[query] = time.time()
            return None
        if query in self._not_found:
            del self._not_found[query]
        album = next(a for a in search_result.albums if a.id == album_id)
        self.download_album(album)
        return album.folder

    def download_album(self, album: TidalAlbum) -> None:
        """
        Download an album from Tidal:
        - Download once the album cover (written to each track)
        - Get the list of tracks and their stream URL
        - Download each track
        """
        logger.info(f"Downloading album: {album.title}")
        album.cover_bytes = self.get_album_cover(album)
        track_list = self.get_track_list(album)
        for track in track_list:
            track_stream = self.get_track_stream(track.id)
            self.download_track(album, track, track_stream)
        logger.info(f"Finished downloading album: {album.title}")

    def get_track_list(self, album: TidalAlbum) -> list[TidalTrack]:
        url = f"{self._config.api_hifi_url}/albums/{album.id}/items"
        content = self._authenticated_request(url, {"limit": 100}).json()["items"]
        track_list = [TidalTrack(**item["item"]) for item in content]
        logger.info(f"Album {album.title} ({album.id}) has {len(track_list)} tracks")
        return track_list

    def get_album_cover(self, album: TidalAlbum) -> bytes | None:
        cover_bytes = None
        logger.info(f"Downloading a cover for: {album.title}")
        for cover_url in album.cover_urls:
            with suppress(HTTPError):
                cover_bytes = self._request(cover_url).content
                logger.info(f"Finished downloading a cover for: {album.title}")
                return cover_bytes
        logger.info(f"Could not find a cover for: {album.title}")
        return None

    def get_track_stream(self, track_id: int) -> TidalStream:
        params = {
            "audioquality": AudioQuality.LOSSLESS,
            "playbackmode": PlaybackMode.STREAM,
            "assetpresentation": AssetPresentation.FULL,
        }
        url = f"{self._config.api_hifi_url}/tracks/{track_id}/playbackinfopostpaywall"
        content = self._authenticated_request(url, params=params).json()
        return TidalStream(**content)

    def get_track_lyrics(self, track_id: int) -> str | None:
        lyrics = None
        with suppress(HTTPError):
            lyrics_url = f"{self._config.lyrics_v1_url}/tracks/{track_id}/lyrics"
            params = {"locale": "en_US", "deviceType": "BROWSER"}
            lyrics = self._authenticated_request(lyrics_url, params).json()["lyrics"]
        return lyrics

    def download_track(self, album: TidalAlbum, track: TidalTrack, track_stream: TidalStream) -> None:
        """
        Download logic:
        - Prepare the path (noop if exists)
        - Skip existing files
        - Download the track to a temporary file to avoid writing incomplete files
        - Write the metadata to the file: cover, lyrics, tags
        - Finally move the temporary file to the final path once complete
        """

        folder = self._config.download_path / album.folder
        folder.mkdir(parents=True, exist_ok=True)

        file_path = folder / track.name
        if file_path.exists():
            logger.info(f"File exists: {file_path}")
            return
        file_path.relative_to(folder)

        logger.info(f"Now downloading: {track.name}")
        track_bytes = self._request(track_stream.url).content
        lyrics: str | None = self.get_track_lyrics(track.id)

        with tempfile.NamedTemporaryFile() as temp_file:
            temp_file.write(track_bytes)
            temp_file.flush()
            track.save_metadata(album, track_stream, temp_file.name, album.cover_bytes, lyrics)
            shutil.move(temp_file.name, file_path)
            logger.info(f"Saved {file_path}")

        if self._config.sleep_between_downloads:
            random_time = randrange(1000, 5000) / 1000
            logger.info(f"Sleeping {random_time:.2f} seconds")
        time.sleep(random_time)
