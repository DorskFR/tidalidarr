import asyncio
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from random import randrange

from aiohttp import ClientError, ClientSession

from tidalidarr.tidal.base_client import TidalBaseClient
from tidalidarr.tidal.models import (
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


class TidalClient(TidalBaseClient):
    def __init__(self, config: TidalConfig, session: ClientSession) -> None:
        super().__init__(config, session)
        self._not_found: dict[str, float] = {}
        self._download_queue: asyncio.Queue[TidalAlbum] = asyncio.Queue()
        self._ready_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._enqueued: set[int] = set()

    async def process_queue(self) -> None:
        while True:
            album = await self._download_queue.get()
            await self._download_album(album)
            await self._ready_queue.put(album.folder)

    async def get_ready_paths(self) -> AsyncIterator[Path]:
        while True:
            try:
                yield self._ready_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def search(self, query: str) -> Path | None:
        """
        This function pilots the Tidal logic:
        - Search for a query string (filtering recent queries that failed)
        - If found an Album in the search result we try to download it
        - If successful, return the path where the Album was saved
        """
        if query in self._not_found and (time.time() - self._not_found[query]) < self._config.check_interval:
            return None

        search_result = await self._search(query)
        if not (album_id := search_result.top_hit_id):
            logging.warn(f"Could not find an album for: {query}")
            self._not_found[query] = time.time()
            return None

        if query in self._not_found:
            del self._not_found[query]

        album = next(a for a in search_result.albums if a.id == album_id)
        await self.enqueue_album(album)
        return album.folder

    async def find_album(self, album_id: int) -> TidalAlbum:
        params = {"countryCode": self._config.country_code}
        url = f"{self._config.api_hifi_url}/albums/{album_id}"
        response = await self._request("GET", url, params=params, is_authenticated=True)
        content = await response.json()
        return TidalAlbum(**content)

    async def enqueue_album(self, album: TidalAlbum) -> str:
        if album.id not in self._enqueued:
            await self._download_queue.put(album)
            self._enqueued.add(album.id)
            result = f"Album {album.id} - {album.title} added to download queue"
        else:
            result = f"Album {album.id} - {album.title} is already in queue"
        logger.info(result)
        return result

    async def _search(self, query: str) -> TidalSearchResult:
        """
        Trigger a search to Tidal's API using a query string built with Artist + release name
        If found, we return the search model with all the parsed fields.
        """
        logger.info(f"Searching for: {query}")
        params = {"query": query, "countryCode": self._config.country_code}
        url = f"{self._config.api_hifi_url}/search"
        resp = await self._request("GET", url, params=params, is_authenticated=True)
        content = await resp.json()
        result = TidalSearchResult(**content)
        self._log_search_result(result)
        return result

    @staticmethod
    def _log_search_result(result: TidalSearchResult) -> None:
        logger.info(
            f"Found: {len(result.artists)} artists and {len(result.albums)} albums "
            f"{'with' if result.top_hit else 'without'} a top hit."
        )
        if len(result.artists):
            logger.info("Artists:")
            for artist in result.artists:
                logger.info(f"- {artist.name}")
        if len(result.albums):
            logger.info("Albums:")
            for album in result.albums:
                logger.info(f"- {album.title}")
        if result.top_hit and result.top_hit_id:
            result_name = result.top_hit["value"].get("name") or result.top_hit["value"].get("title")
            logger.info(f'Got a top hit of type {result.top_hit["type"]} with name {result_name}')

    async def _download_album(self, album: TidalAlbum) -> None:
        """
        Download an album from Tidal:
        - Download once the album cover (written to each track)
        - Get the list of tracks and their stream URL
        - Download each track
        """
        logger.info(f"Downloading album: {album.title}")
        album.cover_bytes = await self._get_album_cover(album)
        track_list = await self._get_track_list(album)
        for track in track_list:
            track_stream = await self._get_track_stream(track.id)
            await self._download_track(album, track, track_stream)
        logger.info(f"Finished downloading album: {album.title}")

    async def _get_track_list(self, album: TidalAlbum) -> list[TidalTrack]:
        url = f"{self._config.api_hifi_url}/albums/{album.id}/items"
        response = await self._request("GET", url, params={"limit": 100}, is_authenticated=True)
        content = await response.json()
        track_list = [TidalTrack(**item["item"]) for item in content["items"]]
        logger.info(f"Album {album.title} ({album.id}) has {len(track_list)} tracks")
        return track_list

    async def _get_album_cover(self, album: TidalAlbum) -> bytes | None:
        cover_bytes = None
        logger.info(f"Downloading a cover for: {album.title}")
        for cover_url in album.cover_urls:
            with suppress(ClientError):
                response = await self._request("GET", cover_url)
                cover_bytes = await response.content.read()
                logger.info(f"Finished downloading a cover for: {album.title}")
                return cover_bytes
        logger.info(f"Could not find a cover for: {album.title}")
        return None

    async def _get_track_stream(self, track_id: int) -> TidalStream:
        params = {
            "audioquality": AudioQuality.LOSSLESS,
            "playbackmode": PlaybackMode.STREAM,
            "assetpresentation": AssetPresentation.FULL,
        }
        url = f"{self._config.api_hifi_url}/tracks/{track_id}/playbackinfopostpaywall"
        response = await self._request("GET", url, params=params, is_authenticated=True)
        content = await response.json()
        return TidalStream(**content)

    async def _get_track_lyrics(self, track_id: int) -> str | None:
        lyrics = None
        with suppress(ClientError):
            lyrics_url = f"{self._config.lyrics_v1_url}/tracks/{track_id}/lyrics"
            params = {"locale": "en_US", "deviceType": "BROWSER"}
            response = await self._request("GET", lyrics_url, params=params, is_authenticated=True)
            content = await response.json()
            lyrics = content["lyrics"]
        return lyrics

    async def _download_track(self, album: TidalAlbum, track: TidalTrack, track_stream: TidalStream) -> None:
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
        resp = await self._request("GET", track_stream.url)
        track_bytes = await resp.content.read()
        lyrics: str | None = await self._get_track_lyrics(track.id)

        with tempfile.NamedTemporaryFile() as temp_file:
            temp_file.write(track_bytes)
            temp_file.flush()
            track.save_metadata(album, track_stream, temp_file.name, album.cover_bytes, lyrics)
            shutil.move(temp_file.name, file_path)
            logger.info(f"Saved {file_path}")

        if self._config.sleep_between_downloads:
            random_time = randrange(1000, 5000) / 1000
            logger.info(f"Sleeping {random_time:.2f} seconds")
        await asyncio.sleep(random_time)
