import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession

from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import TidalAlbum, TidalConfig, TidalSearchResult


def _album_payload() -> dict[str, Any]:
    with Path("tests/tidal/data/album.json").open(mode="r", encoding="utf-8") as p:
        return json.load(p)


def _search_result(top_hit_id: int, albums: list[dict[str, Any]]) -> TidalSearchResult:
    return TidalSearchResult(
        artists={"items": []},
        albums={"items": albums},
        tracks={"items": []},
        topHit={"type": "ALBUMS", "value": {"id": top_hit_id, "title": "top hit"}},
    )


def _search(
    search_result: TidalSearchResult,
    find_album: Callable[[int], Coroutine[Any, Any, TidalAlbum]],
) -> Path | None:
    async def fake_search(query: str) -> TidalSearchResult:  # noqa: ARG001
        return search_result

    async def run() -> Path | None:
        async with ClientSession() as session:
            config = TidalConfig(client_id="id", client_secret="secret")  # noqa: S106
            client = TidalClient(config, session)
            client._search = fake_search  # type: ignore[method-assign] # noqa: SLF001
            client.find_album = find_album  # type: ignore[method-assign, assignment]
            return await client.search("query")

    return asyncio.run(run())


def test_search_falls_back_to_find_album_when_top_hit_missing_from_albums() -> None:
    album = TidalAlbum(**_album_payload())
    calls: list[int] = []

    async def find_album(album_id: int) -> TidalAlbum:
        calls.append(album_id)
        return album

    folder = _search(_search_result(album.id, albums=[]), find_album)

    assert folder == album.folder
    assert calls == [album.id]


def test_search_uses_album_from_search_results_when_present() -> None:
    payload = _album_payload()
    album = TidalAlbum(**payload)

    async def find_album(album_id: int) -> TidalAlbum:
        pytest.fail(f"find_album should not be called, got {album_id}")

    folder = _search(_search_result(album.id, albums=[payload]), find_album)

    assert folder == album.folder
