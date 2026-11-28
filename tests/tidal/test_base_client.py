import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import ClientResponseError, ClientSession

from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import AuthState, TidalConfig, TidalToken


def _client(session: ClientSession) -> TidalClient:
    return TidalClient(TidalConfig(client_id="id", client_secret="secret"), session)  # noqa: S106


def _run_ensure(*, access_token_valid: bool, subscribed: bool = True) -> tuple[AuthState, list[str]]:
    calls: list[str] = []

    async def run() -> AuthState:
        async with ClientSession() as session:
            client = _client(session)
            client._token = object()  # type: ignore[assignment] # noqa: SLF001
            client._auth_state = AuthState.TOKEN_PRESENT  # noqa: SLF001

            async def verify() -> None:
                calls.append("verify")
                if access_token_valid or "refresh" in calls:
                    client._change_state(AuthState.LOGGED_IN)  # noqa: SLF001
                else:
                    client._change_state(AuthState.ACCESS_TOKEN_EXPIRED)  # noqa: SLF001

            async def test_download() -> None:
                calls.append("test_download")
                if not subscribed:
                    client._change_state(AuthState.UNAUTHENTICATED)  # noqa: SLF001

            async def refresh() -> None:
                calls.append("refresh")
                client._change_state(AuthState.LOGGED_IN)  # noqa: SLF001

            async def device_login() -> None:
                calls.append("device_login")
                client._change_state(AuthState.LOGGED_IN)  # noqa: SLF001

            client._verify_access_token = verify  # type: ignore[method-assign] # noqa: SLF001
            client._test_download = test_download  # type: ignore[method-assign] # noqa: SLF001
            client._login_with_refresh_token = refresh  # type: ignore[method-assign] # noqa: SLF001
            client._login = device_login  # type: ignore[method-assign] # noqa: SLF001
            await client.login()
            return client._auth_state  # noqa: SLF001

    return asyncio.run(run()), calls


def test_an_expired_access_token_is_refreshed_without_a_device_login() -> None:
    state, calls = _run_ensure(access_token_valid=False)
    assert state is AuthState.LOGGED_IN
    assert calls == ["verify", "refresh"]


def test_a_valid_token_still_checks_the_subscription() -> None:
    state, calls = _run_ensure(access_token_valid=True)
    assert state is AuthState.LOGGED_IN
    assert calls == ["verify", "test_download"]


def test_an_unsubscribed_account_falls_back_to_device_login() -> None:
    state, calls = _run_ensure(access_token_valid=True, subscribed=False)
    assert state is AuthState.LOGGED_IN
    assert calls == ["verify", "test_download", "device_login"]


class _Response:
    status = 200

    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self._payload = payload or {}

    async def json(self) -> dict[str, object]:
        return self._payload

    async def text(self) -> str:
        return ""


def test_the_real_refresh_path_reaches_logged_in(tmp_path: Path) -> None:
    async def run() -> tuple[AuthState, str]:
        async with ClientSession() as session:
            config = TidalConfig(client_id="id", client_secret="secret", token_path=tmp_path / "token.json")  # noqa: S106
            client = TidalClient(config, session)
            client._token = TidalToken(**TOKEN)  # noqa: SLF001
            client._auth_state = AuthState.TOKEN_PRESENT  # noqa: SLF001

            async def request(_method: str, _url: str, **_kwargs: object) -> _Response:
                assert client._token  # noqa: SLF001
                if client._token.access_token == TOKEN["access_token"]:  # noqa: SLF001
                    raise ClientResponseError(None, (), status=401)  # type: ignore[arg-type]
                return _Response()

            async def post(_url: str, **_kwargs: object) -> _Response:
                return _Response({"access_token": "fresh", "expires_in": 604800})

            client._request = request  # type: ignore[method-assign, assignment] # noqa: SLF001
            client._session.post = post  # type: ignore[method-assign, assignment] # noqa: SLF001
            await client.login()
            saved = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
            return client._auth_state, saved["access_token"]  # noqa: SLF001

    state, saved = asyncio.run(run())
    assert state is AuthState.LOGGED_IN
    assert saved == "fresh"


TOKEN = {
    "access_token": "expired",
    "client_name": "c",
    "expires_in": 1,
    "refresh_token": "r",
    "scope": "s",
    "token_type": "Bearer",
    "user_id": 1,
    "user": {"country_code": "JP", "email": "a@example.com", "user_id": 1, "username": "u"},
}


def test_a_401_mid_run_refreshes_the_token_and_retries(tmp_path: Path) -> None:
    async def run() -> tuple[AuthState, list[str]]:
        async with ClientSession() as session:
            config = TidalConfig(
                client_id="id",
                client_secret="secret",  # noqa: S106
                token_path=tmp_path / "token.json",
                sleep_between_requests=0.01,
            )
            client = TidalClient(config, session)
            client._token = TidalToken(**TOKEN)  # noqa: SLF001
            client._auth_state = AuthState.LOGGED_IN  # noqa: SLF001
            calls: list[str] = []

            async def request(_method: str, url: str, **kwargs: Any) -> _Response:
                token = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
                calls.append(f"{url.rsplit('/', 1)[-1]}:{token}")
                if token == TOKEN["access_token"]:
                    raise ClientResponseError(None, (), status=401)  # type: ignore[arg-type]
                return _Response()

            async def post(_url: str, **_kwargs: object) -> _Response:
                calls.append("refresh")
                return _Response({"access_token": "fresh", "expires_in": 604800})

            async def get(_url: str, **_kwargs: object) -> _Response:
                return _Response()

            client._session.request = request  # type: ignore[method-assign, assignment] # noqa: SLF001
            client._session.post = post  # type: ignore[method-assign, assignment] # noqa: SLF001
            client._session.get = get  # type: ignore[method-assign, assignment] # noqa: SLF001
            await client._request("GET", "https://api.tidalhifi.com/v1/search", with_auth_headers=True)  # noqa: SLF001
            return client._auth_state, calls  # noqa: SLF001

    state, calls = asyncio.run(run())
    assert state is AuthState.LOGGED_IN
    assert calls == ["search:expired", "sessions:expired", "refresh", "sessions:fresh", "search:fresh"]
