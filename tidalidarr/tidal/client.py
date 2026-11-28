import asyncio
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

from aiohttp import ClientError, ClientSession

from tidalidarr.grabs import GrabJob, GrabStatus, GrabStore
from tidalidarr.tidal.base_client import TidalBaseClient
from tidalidarr.tidal.models import (
    AlbumCandidate,
    AssetPresentation,
    AudioQuality,
    PlaybackMode,
    TidalAlbum,
    TidalConfig,
    TidalGrabCancelledError,
    TidalQueueInformation,
    TidalSearchResult,
    TidalStream,
    TidalTrack,
    check_flac_magic,
)
from tidalidarr.utils import contains_japanese, jitter_sleep, romanize

logger = logging.getLogger(__name__)


ALBUM_ITEMS_PAGE = 100


class TidalClient(TidalBaseClient):
    def __init__(
        self,
        config: TidalConfig,
        session: ClientSession,
        grabs: GrabStore | None = None,
        *,
        track_ready: bool = True,
    ) -> None:
        super().__init__(config, session)
        self._not_found: dict[str, float] = {}
        self._download_queue: asyncio.Queue[TidalAlbum] = asyncio.Queue()
        self._ready_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._enqueued: set[int] = set()
        self._track_ready = track_ready
        self.grabs = grabs or GrabStore()

    def get_queue_information(self) -> TidalQueueInformation:
        albums = list(self._download_queue._queue)  # type: ignore[attr-defined] # noqa: SLF001
        ready = list(self._ready_queue._queue)  # type: ignore[attr-defined] # noqa: SLF001
        not_found = sorted(self._not_found.keys())

        return TidalQueueInformation(
            albums=albums,
            albums_count=len(albums),
            ready=ready,
            ready_count=len(ready),
            not_found=not_found,
            not_found_count=len(not_found),
        )

    async def login(self) -> None:
        async with self._auth_lock:
            await self._ensure_authenticated()

    async def resume_pending(self) -> None:
        for job in self.grabs.pending():
            try:
                album = await self.find_album(job.album_id)
            except ClientError as error:
                logger.warning(f"Could not resume grab {job.id}: {error}")
                self.grabs.update(job.id, status=GrabStatus.FAILED, error=str(error))
                continue
            self.grabs.update(job.id, status=GrabStatus.QUEUED)
            await self._download_queue.put(album)
            logger.info(f"Resumed grab {job.id} - {job.title}")

    async def process_queue(self) -> None:
        while True:
            album = await self._download_queue.get()
            job_id = str(album.id)
            job = self.grabs.get(job_id)
            if not job or job.status is not GrabStatus.QUEUED:
                continue
            attempts = job.attempts + 1
            self.grabs.update(job_id, status=GrabStatus.DOWNLOADING, attempts=attempts, error=None)
            try:
                await self._download_album(album)
            except TidalGrabCancelledError:
                logger.info(f"Grab {job_id} - {album.title} cancelled")
                continue
            except Exception as error:
                logger.exception(f"Error while downloading {album.title} (attempt {attempts})")
                if attempts < self._config.max_attempts:
                    self.grabs.update(job_id, status=GrabStatus.QUEUED, error=str(error))
                    self._download_queue.put_nowait(album)
                else:
                    self.grabs.update(job_id, status=GrabStatus.FAILED, error=str(error))
                continue
            output = self._config.download_path / album.folder
            self.grabs.update(job_id, status=GrabStatus.COMPLETED, output_path=output.as_posix())
            if self._track_ready:
                await self._ready_queue.put(album.folder)

    def grab(self, album: TidalAlbum) -> GrabJob:
        """Queue a download of `album` unless one is running or already landed on disk."""
        job_id = str(album.id)
        existing = self.grabs.get(job_id)
        if existing and existing.status.is_active:
            return existing
        if (
            existing
            and existing.status is GrabStatus.COMPLETED
            and (self._config.download_path / album.folder).exists()
        ):
            return existing
        job = self.grabs.put(
            GrabJob(
                id=job_id,
                album_id=album.id,
                title=album.full_title,
                artist=album.artist,
                tracks_total=album.number_of_tracks,
            )
        )
        self._download_queue.put_nowait(album)
        logger.info(f"Grab {job_id} - {album.title} added to download queue")
        return job

    def cancel_grab(self, job_id: str) -> GrabJob | None:
        """Cancel an active grab; forget a finished one."""
        job = self.grabs.get(job_id)
        if job is None:
            return None
        if job.status.is_active:
            self._enqueued.discard(job.album_id)
            return self.grabs.update(job_id, status=GrabStatus.CANCELLED)
        self.grabs.remove(job_id)
        return job

    def _is_cancelled(self, album_id: int) -> bool:
        job = self.grabs.get(str(album_id))
        return job is not None and job.status is GrabStatus.CANCELLED

    async def search_albums(self, artist: str, title: str, year: int | None = None) -> list[AlbumCandidate]:
        query = f"{artist} {title}".strip()
        result = await self._search(query, types="ALBUMS", limit=25)
        if not result.albums and contains_japanese(query):
            result = await self._search(romanize(query), types="ALBUMS", limit=25)
        candidates = [
            AlbumCandidate.score(album, artist, title, year) for album in result.albums if album.allow_streaming
        ]
        return sorted(candidates, key=lambda c: c.confidence, reverse=True)

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
            logging.warning(f"Could not find an album for: {query}")
            self._not_found[query] = time.time()
            return None

        if query in self._not_found:
            del self._not_found[query]

        album = next((a for a in search_result.albums if a.id == album_id), None) or await self.find_album(album_id)
        await self.enqueue_album(album)
        return album.folder

    async def find_album(self, album_id: int) -> TidalAlbum:
        params = {"countryCode": self._config.country_code}
        url = f"{self._config.api_hifi_url}/albums/{album_id}"
        response = await self._request("GET", url, params=params, with_auth_headers=True)
        content = await response.json()
        return TidalAlbum(**content)

    async def enqueue_album(self, album: TidalAlbum) -> str:
        if album.id not in self._enqueued:
            self._enqueued.add(album.id)
            self.grab(album)
            result = f"Album {album.id} - {album.title} added to download queue"
        else:
            result = f"Album {album.id} - {album.title} is already in queue"
        logger.info(result)
        return result

    async def _search(self, query: str, types: str | None = None, limit: int | None = None) -> TidalSearchResult:
        """
        Trigger a search to Tidal's API using a query string built with Artist + release name
        If found, we return the search model with all the parsed fields.
        """
        logger.info(f"🔍 Searching for: {query}")
        params: dict[str, str | int] = {"query": query, "countryCode": self._config.country_code}
        if types:
            params["types"] = types
        if limit:
            params["limit"] = limit
        url = f"{self._config.api_hifi_url}/search"
        resp = await self._request("GET", url, params=params, with_auth_headers=True)
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
            logger.info(f"Got a top hit of type {result.top_hit['type']} with name {result_name}")

    async def _download_album(self, album: TidalAlbum) -> None:
        """
        Download an album from Tidal:
        - Download once the album cover (written to each track)
        - Get the list of tracks and their stream URL
        - Download each track
        """
        logger.info(f"💿 Downloading album: {album.title}")
        album.cover_bytes = await self._get_album_cover(album)
        track_list = await self._get_track_list(album)
        job_id = str(album.id)
        self.grabs.update(job_id, tracks_total=len(track_list), tracks_done=0)
        written: list[Path] = []
        for done, track in enumerate(track_list, start=1):
            if self._is_cancelled(album.id):
                self._discard(written)
                raise TidalGrabCancelledError(job_id)
            track_stream = await self._get_track_stream(track.id)
            if path := await self._download_track(album, track, track_stream):
                written.append(path)
            self.grabs.update(job_id, tracks_done=done)
        logger.info(f"Finished downloading album: {album.title}")

    @staticmethod
    def _discard(paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)
        if paths:
            with suppress(OSError):
                paths[0].parent.rmdir()

    async def _get_track_list(self, album: TidalAlbum) -> list[TidalTrack]:
        url = f"{self._config.api_hifi_url}/albums/{album.id}/items"
        track_list: list[TidalTrack] = []
        offset = 0
        while True:
            params = {"limit": ALBUM_ITEMS_PAGE, "offset": offset}
            response = await self._request("GET", url, params=params, with_auth_headers=True)
            content = await response.json()
            items = content["items"]
            track_list.extend(TidalTrack(**item["item"]) for item in items if item.get("type", "track") == "track")
            offset += len(items)
            if not items or offset >= content.get("totalNumberOfItems", 0):
                break
        logger.info(f"Album {album.title} ({album.id}) has {len(track_list)} tracks")
        return track_list

    async def _get_album_cover(self, album: TidalAlbum) -> bytes | None:
        cover_bytes = None
        logger.info(f"🌆 Downloading a cover for: {album.title}")
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
        response = await self._request("GET", url, params=params, with_auth_headers=True)
        content = await response.json()
        return TidalStream(**content)

    async def _get_track_lyrics(self, track_id: int) -> str | None:
        lyrics = None
        with suppress(ClientError):
            lyrics_url = f"{self._config.lyrics_v1_url}/tracks/{track_id}/lyrics"
            params = {"locale": "en_US", "deviceType": "BROWSER"}
            response = await self._request("GET", lyrics_url, params=params, with_auth_headers=True)
            content = await response.json()
            lyrics = content["lyrics"]
        return lyrics

    async def _download_track(self, album: TidalAlbum, track: TidalTrack, track_stream: TidalStream) -> Path | None:
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
            logger.info(f"👯‍♀️ File exists: {file_path}")
            return None
        file_path.relative_to(folder)

        track_stream.check_flac_codec()

        logger.info(f"⚡️ Now downloading: {track.name}")
        resp = await self._request("GET", track_stream.url)
        track_bytes = await resp.content.read()
        check_flac_magic(track_bytes, track.id)
        lyrics: str | None = await self._get_track_lyrics(track.id)

        with tempfile.NamedTemporaryFile() as temp_file:
            temp_file.write(track_bytes)
            temp_file.flush()
            track.save_metadata(album, track_stream, temp_file.name, album.cover_bytes, lyrics)
            shutil.move(temp_file.name, file_path)
            logger.info(f"📥 Saved {file_path}")

        await jitter_sleep(self._config.sleep_between_downloads)
        return file_path
