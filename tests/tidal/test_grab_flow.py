import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession

from tidalidarr.grabs import GrabStatus, GrabStore
from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import TidalAlbum, TidalConfig, TidalSearchResult, TidalStream, TidalTrack


def _load(name: str) -> dict[str, Any]:
    with Path(f"tests/tidal/data/{name}.json").open(mode="r", encoding="utf-8") as p:
        return json.load(p)


def _album(**fields: Any) -> TidalAlbum:
    return TidalAlbum(**(_load("album") | fields))


def _run(test: Callable[[TidalClient], Awaitable[None]], tmp_path: Path, **config: Any) -> None:
    async def run() -> None:
        async with ClientSession() as session:
            settings = TidalConfig(
                client_id="id",
                client_secret="secret",  # noqa: S106
                download_path=tmp_path,
                sleep_between_downloads=0,
                **config,
            )
            await test(TidalClient(settings, session, GrabStore(tmp_path / "grabs.json")))

    asyncio.run(run())


async def _until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=2)


def _status(client: TidalClient, job_id: str) -> GrabStatus | None:
    job = client.grabs.get(job_id)
    return job.status if job else None


def test_grab_is_idempotent_while_active(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()
        first = client.grab(album)
        second = client.grab(album)
        assert first.id == second.id == str(album.id)
        assert first.status is GrabStatus.QUEUED
        assert first.tracks_total == album.number_of_tracks
        assert client.get_queue_information().albums_count == 1

    _run(test, tmp_path)


def test_completed_grab_is_kept_while_its_folder_exists(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()
        client.grab(album)
        client.grabs.update(str(album.id), status=GrabStatus.COMPLETED)
        (tmp_path / album.folder).mkdir(parents=True)
        assert client.grab(album).status is GrabStatus.COMPLETED

        (tmp_path / album.folder).rmdir()
        assert client.grab(album).status is GrabStatus.QUEUED

    _run(test, tmp_path)


def test_queue_completes_a_grab_with_its_output_path(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()

        async def download(_album: TidalAlbum) -> None:
            return None

        client._download_album = download  # type: ignore[method-assign, assignment] # noqa: SLF001
        client.grab(album)
        worker = asyncio.create_task(client.process_queue())
        await _until(lambda: _status(client, str(album.id)) is GrabStatus.COMPLETED)
        worker.cancel()

        job = client.grabs.get(str(album.id))
        assert job is not None
        assert job.output_path == (tmp_path / album.folder).as_posix()
        assert job.attempts == 1
        assert [p async for p in client.get_ready_paths()] == [album.folder]

    _run(test, tmp_path)


def test_failing_downloads_stop_after_max_attempts(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()
        calls: list[int] = []

        async def download(_album: TidalAlbum) -> None:
            calls.append(1)
            raise RuntimeError("not lossless")

        client._download_album = download  # type: ignore[method-assign, assignment] # noqa: SLF001
        client.grab(album)
        worker = asyncio.create_task(client.process_queue())
        await _until(lambda: _status(client, str(album.id)) is GrabStatus.FAILED)
        worker.cancel()

        job = client.grabs.get(str(album.id))
        assert job is not None
        assert job.error == "not lossless"
        assert job.attempts == 2
        assert len(calls) == 2

    _run(test, tmp_path, max_attempts=2)


def test_cancelled_queued_grab_is_skipped(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()

        async def download(_album: TidalAlbum) -> None:
            pytest.fail("cancelled grab must not download")

        client._download_album = download  # type: ignore[method-assign, assignment] # noqa: SLF001
        client.grab(album)
        cancelled = client.cancel_grab(str(album.id))
        assert cancelled is not None
        assert cancelled.status is GrabStatus.CANCELLED
        worker = asyncio.create_task(client.process_queue())
        await _until(lambda: client.get_queue_information().albums_count == 0)
        await asyncio.sleep(0.05)
        worker.cancel()
        assert _status(client, str(album.id)) is GrabStatus.CANCELLED

    _run(test, tmp_path)


def test_cancelling_a_finished_grab_forgets_it(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()
        client.grab(album)
        client.grabs.update(str(album.id), status=GrabStatus.FAILED)
        assert client.cancel_grab(str(album.id)) is not None
        assert client.grabs.get(str(album.id)) is None
        assert client.cancel_grab("404") is None

    _run(test, tmp_path)


def test_cancel_mid_download_removes_written_tracks(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()
        tracks = [TidalTrack(**(_load("track") | {"id": i, "trackNumber": i, "title": f"t{i}"})) for i in (1, 2, 3)]
        stream = TidalStream(**_load("stream"))

        async def no_cover(_album: TidalAlbum) -> None:
            return None

        async def track_list(_album: TidalAlbum) -> list[TidalTrack]:
            return tracks

        async def get_stream(_track_id: int) -> TidalStream:
            return stream

        async def write_track(album: TidalAlbum, track: TidalTrack, _stream: TidalStream) -> Path:
            folder = tmp_path / album.folder
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / track.name
            path.write_bytes(b"fLaC")
            if track.id == 2:
                client.cancel_grab(str(album.id))
            return path

        client._get_album_cover = no_cover  # type: ignore[method-assign, assignment] # noqa: SLF001
        client._get_track_list = track_list  # type: ignore[method-assign, assignment] # noqa: SLF001
        client._get_track_stream = get_stream  # type: ignore[method-assign, assignment] # noqa: SLF001
        client._download_track = write_track  # type: ignore[method-assign, assignment] # noqa: SLF001
        client.grab(album)
        worker = asyncio.create_task(client.process_queue())
        await _until(lambda: client.get_queue_information().albums_count == 0)
        await asyncio.sleep(0.05)
        worker.cancel()

        job = client.grabs.get(str(album.id))
        assert job is not None
        assert job.status is GrabStatus.CANCELLED
        assert job.tracks_done == 2
        assert not (tmp_path / album.folder).exists()

    _run(test, tmp_path)


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return self._payload


def test_track_list_paginates_and_skips_videos(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        track = _load("track")
        items = [{"type": "track", "item": track | {"id": i}} for i in range(150)]
        items.insert(120, {"type": "video", "item": {"id": 999}})
        offsets: list[int] = []

        async def request(_method: str, _url: str, params: dict[str, Any], **_kwargs: Any) -> _Response:
            offsets.append(params["offset"])
            page = items[params["offset"] : params["offset"] + params["limit"]]
            return _Response({"items": page, "totalNumberOfItems": len(items)})

        client._request = request  # type: ignore[method-assign, assignment] # noqa: SLF001
        tracks = await client._get_track_list(_album())  # noqa: SLF001

        assert offsets == [0, 100]
        assert len(tracks) == 150
        assert 999 not in {t.id for t in tracks}

    _run(test, tmp_path)


def test_search_albums_ranks_the_matching_album_first(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        payload = _load("album")
        albums = [
            payload | {"id": 1, "title": "Something Else", "artists": [{"id": 9, "name": "Other Band"}]},
            payload | {"id": 2},
            payload | {"id": 3, "releaseDate": "2019-01-01"},
            payload | {"id": 4, "allowStreaming": False},
        ]
        queries: list[tuple[str, str | None, int | None]] = []

        async def fake_search(query: str, types: str | None = None, limit: int | None = None) -> TidalSearchResult:
            queries.append((query, types, limit))
            return TidalSearchResult(albums={"items": albums})

        client._search = fake_search  # type: ignore[method-assign, assignment] # noqa: SLF001
        results = await client.search_albums("SEKAI NO OWARI", "Scent of Memory", 2021)

        assert queries == [("SEKAI NO OWARI Scent of Memory", "ALBUMS", 25)]
        assert [r.id for r in results] == ["2", "3", "1"]
        assert results[0].confidence > 0.9
        assert results[0].upc == "00602438470143"
        assert results[0].year == 2021
        assert results[-1].confidence < 0.5

    _run(test, tmp_path)


def test_search_albums_retries_japanese_queries_romanized(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        queries: list[str] = []

        async def fake_search(query: str, types: str | None = None, limit: int | None = None) -> TidalSearchResult:  # noqa: ARG001
            queries.append(query)
            return TidalSearchResult(albums={"items": []})

        client._search = fake_search  # type: ignore[method-assign, assignment] # noqa: SLF001
        assert await client.search_albums("テスト", "") == []
        assert queries == ["テスト", "tesuto"]

    _run(test, tmp_path)


def test_a_regrabbed_album_is_downloaded_once(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()
        calls: list[int] = []

        async def download(_album: TidalAlbum) -> None:
            calls.append(1)

        client._download_album = download  # type: ignore[method-assign, assignment] # noqa: SLF001
        client.grab(album)
        client.cancel_grab(str(album.id))
        client.grab(album)
        worker = asyncio.create_task(client.process_queue())
        await _until(lambda: client.get_queue_information().albums_count == 0)
        await asyncio.sleep(0.05)
        worker.cancel()

        assert _status(client, str(album.id)) is GrabStatus.COMPLETED
        assert calls == [1]

    _run(test, tmp_path)
