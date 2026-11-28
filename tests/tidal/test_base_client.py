import asyncio

from aiohttp import ClientSession

from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import AuthState, TidalConfig


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
