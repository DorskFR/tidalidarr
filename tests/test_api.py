import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import ClientSession
from starlette.requests import Request
from starlette.responses import Response

from tidalidarr import api
from tidalidarr.grabs import GrabStatus, GrabStore
from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import AlbumCandidate, TidalAlbum, TidalConfig

KEY = "k3y"


def _album() -> TidalAlbum:
    with Path("tests/tidal/data/album.json").open(mode="r", encoding="utf-8") as p:
        return TidalAlbum(**json.load(p))


def _request(
    client: TidalClient,
    method: str,
    body: Any = None,
    *,
    key: str | None = KEY,
    configured_key: str = KEY,
    grab_id: str | None = None,
) -> Request:
    headers = [(b"x-api-key", key.encode())] if key is not None else []
    raw = json.dumps(body).encode() if body is not None else b""

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": headers,
        "query_string": b"",
        "path_params": {"grab_id": grab_id} if grab_id else {},
        "state": {"tidal_client": client, "api_config": api.ApiConfig(api_key=configured_key)},
    }
    return Request(scope, receive)


def _json(response: Response) -> Any:
    return json.loads(bytes(response.body))


def _run(test: Callable[[TidalClient], Awaitable[None]], tmp_path: Path) -> None:
    async def run() -> None:
        async with ClientSession() as session:
            config = TidalConfig(client_id="id", client_secret="secret", download_path=tmp_path)  # noqa: S106
            client = TidalClient(config, session, GrabStore())
            client._auth_state = client._auth_state.LOGGED_IN  # noqa: SLF001
            await test(client)

    asyncio.run(run())


def test_routes_require_the_api_key(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        assert (await api.health(_request(client, "GET", key=None))).status_code == 401
        assert (await api.health(_request(client, "GET", key="wrong"))).status_code == 401
        disabled = await api.search(_request(client, "POST", {}, configured_key=""))
        assert disabled.status_code == 503
        healthy = await api.health(_request(client, "GET"))
        assert healthy.status_code == 200
        assert _json(healthy) == {"logged_in": True, "auth_url": None}

    _run(test, tmp_path)


def test_search_returns_scored_candidates(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()
        seen: list[tuple[str, str, int | None]] = []

        async def search_albums(artist: str, title: str, year: int | None = None) -> list[AlbumCandidate]:
            seen.append((artist, title, year))
            return [AlbumCandidate.score(album, artist, title, year)]

        client.search_albums = search_albums  # type: ignore[method-assign]
        response = await api.search(
            _request(client, "POST", {"artist": "Sekai No Owari", "album": "scent of memory", "year": 2021})
        )

        assert response.status_code == 200
        assert seen == [("Sekai No Owari", "scent of memory", 2021)]
        [result] = _json(response)
        assert result["id"] == str(album.id)
        assert result["quality"] == "LOSSLESS"
        assert result["confidence"] == 1.0

        bad = await api.search(_request(client, "POST", {"artist": "x"}))
        assert bad.status_code == 400

    _run(test, tmp_path)


def test_search_and_grab_are_refused_until_logged_in(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        client._auth_state = client._auth_state.UNAUTHENTICATED  # noqa: SLF001
        client.auth_url = "https://link.tidal.com/ABCDE"
        response = await api.search(_request(client, "POST", {"artist": "a", "album": "b"}))
        assert response.status_code == 503
        assert _json(response)["auth_url"] == "https://link.tidal.com/ABCDE"
        client.auth_url = ""
        grab = await api.create_grab(_request(client, "POST", {"id": "1"}))
        assert grab.status_code == 503
        assert _json(grab)["auth_url"] is None

    _run(test, tmp_path)


def test_grab_lifecycle(tmp_path: Path) -> None:
    async def test(client: TidalClient) -> None:
        album = _album()

        async def find_album(album_id: int) -> TidalAlbum:
            assert album_id == album.id
            return album

        client.find_album = find_album  # type: ignore[method-assign]
        created = await api.create_grab(_request(client, "POST", {"id": str(album.id)}))
        assert created.status_code == 202
        assert _json(created)["status"] == "queued"

        fetched = await api.get_grab(_request(client, "GET", grab_id=str(album.id)))
        assert _json(fetched)["progress"] == 0.0

        assert (await api.create_grab(_request(client, "POST", {"id": "abc"}))).status_code == 400
        assert (await api.get_grab(_request(client, "GET", grab_id="404"))).status_code == 404

        deleted = await api.delete_grab(_request(client, "DELETE", grab_id=str(album.id)))
        assert deleted.status_code == 204
        job = client.grabs.get(str(album.id))
        assert job is not None
        assert job.status is GrabStatus.CANCELLED
        assert (await api.delete_grab(_request(client, "DELETE", grab_id="404"))).status_code == 404

    _run(test, tmp_path)
