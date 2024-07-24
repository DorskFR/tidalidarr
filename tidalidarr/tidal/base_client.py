import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from aiohttp import BasicAuth, ClientError, ClientResponse, ClientResponseError, ClientSession
from pydantic import HttpUrl
from tenacity import (
    after_log,
    retry,
    retry_if_exception_type,
    stop_after_delay,
    wait_fixed,
)

from tidalidarr.tidal.models import (
    AssetPresentation,
    AudioQuality,
    AuthState,
    PlaybackMode,
    TidalAllAuthenticationFailedError,
    TidalAuthenticationError,
    TidalConfig,
    TidalDeviceAuth,
    TidalLoginWithDeviceFailedError,
    TidalToken,
)

logger = logging.getLogger(__name__)


class TidalBaseClient:
    def __init__(self, config: TidalConfig, session: ClientSession) -> None:
        self._config = config
        self._session = session
        self._token: TidalToken | None = None
        self._auth_state: AuthState = AuthState.UNAUTHENTICATED
        self._auth_lock = asyncio.Lock()

    async def _request(
        self,
        method: str,
        url: HttpUrl | str,
        auth: BasicAuth | None = None,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        *,
        with_auth_headers: bool = False,
    ) -> ClientResponse:
        if HttpUrl(str(url)).path != "/v1/sessions":
            async with self._auth_lock:
                await self._ensure_authenticated()

        # Insert auth headers if the request should be authenticated
        if with_auth_headers:
            assert self._token
            params = (params or {}) | {"countryCode": self._config.country_code}
            headers = (headers or {}) | {"Authorization": f"Bearer {self._token.access_token}"}

        # TODO: cheap rate limit to avoid 429, to improve
        await asyncio.sleep(1)

        # Log the error and re raise
        try:
            response = await self._session.request(
                method,
                str(url),
                auth=auth,
                data=data,
                headers=headers,
                json=json,
                params=params,
            )
        except ClientResponseError as error:
            if error.status == 401 and HttpUrl(str(url)).path != "/v1/sessions":
                await self._verify_access_token()
                await self._test_download()
            raise
        return response

    async def _ensure_authenticated(self):
        try:
            if not self._token:
                self._load_token()

            if self._auth_state is AuthState.TOKEN_PRESENT:
                await self._verify_access_token()
                await self._test_download()

            if self._auth_state is AuthState.UNAUTHENTICATED:
                await self._login()

            if self._auth_state is AuthState.ACCESS_TOKEN_EXPIRED:
                await self._login_with_refresh_token()

            if self._auth_state is AuthState.REFRESH_TOKEN_EXPIRED:
                await self._login()

            if self._auth_state is not AuthState.LOGGED_IN:
                raise TidalAllAuthenticationFailedError("❌ Could not login")

        except TidalAuthenticationError:
            logger.exception("❌ Authentication failed")
            raise

    def _change_state(self, new_state: AuthState) -> None:
        if self._auth_state == new_state:
            return
        match (self._auth_state, new_state):
            case (AuthState.UNAUTHENTICATED, AuthState.TOKEN_PRESENT):
                self._auth_state = new_state
            case (AuthState.UNAUTHENTICATED, AuthState.LOGGED_IN):
                self._auth_state = new_state
            case (AuthState.TOKEN_PRESENT, AuthState.ACCESS_TOKEN_EXPIRED):
                self._auth_state = new_state
            case (AuthState.TOKEN_PRESENT, AuthState.LOGGED_IN):
                self._auth_state = new_state
            case (AuthState.LOGGED_IN, AuthState.UNAUTHENTICATED):
                self._auth_state = new_state
            case (AuthState.LOGGED_IN, AuthState.ACCESS_TOKEN_EXPIRED):
                self._auth_state = new_state
            case (AuthState.ACCESS_TOKEN_EXPIRED, AuthState.LOGGED_IN):
                self._auth_state = new_state
            case (AuthState.ACCESS_TOKEN_EXPIRED, AuthState.REFRESH_TOKEN_EXPIRED):
                self._auth_state = new_state
            case (AuthState.ACCESS_TOKEN_EXPIRED, AuthState.UNAUTHENTICATED):
                self._auth_state = new_state
            case (AuthState.ACCESS_TOKEN_REFRESHED, AuthState.UNAUTHENTICATED):
                self._auth_state = AuthState.SUBSCRIPTION_EXPIRED
            case (AuthState.REFRESH_TOKEN_EXPIRED, AuthState.LOGGED_IN):
                self._auth_state = new_state
            case _:
                raise ValueError(f"This state transition is not allowed: {self._auth_state} -> {new_state}")
        logger.info(f"🔏 New auth state: {self._auth_state}")

    def _load_token(self) -> None:
        with (
            suppress(json.JSONDecodeError, FileNotFoundError),
            self._config.token_path.open(mode="r", encoding="utf-8") as p,
        ):
            self._token = TidalToken(**json.load(p))
            logger.info(f"💽 Loaded token from {self._config.token_path}")
            self._change_state(AuthState.TOKEN_PRESENT)

    def _save_token(self, token: TidalToken) -> None:
        with self._config.token_path.open(mode="w", encoding="utf-8") as p:
            json.dump(token.dict(), p)
        logging.info(f"💾 Token saved to {self._config.token_path}")

    async def _verify_access_token(self) -> None:
        try:
            resp = await self._request("GET", "https://api.tidal.com/v1/sessions", with_auth_headers=True)
        except ClientResponseError:
            logger.warning("❌ Access token is not valid")
            self._change_state(AuthState.ACCESS_TOKEN_EXPIRED)
        else:
            assert resp.status == 200
            _ = await resp.text()
            logger.info("🔑 Access token is valid")
            self._change_state(AuthState.LOGGED_IN)

    async def _login(self) -> None:
        """
        Main login function, try to load the token from .json file
        Refresh the access token if necessary.
        Otherwise start a new login via device authorization
        """
        try:
            logger.info("Logging in with device authorization")
            device_authorization = await self._get_device_authorization()
            logger.info(f"🌐 Please login at this URL: https://{device_authorization.verification_uri_complete}")
            self._token = await self._login_with_device_code(device_authorization)
            self._save_token(self._token)
            self._config.country_code = self._token.user.country_code
            logger.info("🎉 Now logged in")
            self._change_state(AuthState.LOGGED_IN)
        except ClientError as error:
            raise TidalLoginWithDeviceFailedError from error

    async def _get_device_authorization(self) -> TidalDeviceAuth:
        """
        When there is no token active, request a new device authorization.
        The response contains a link to access and login via browser
        """
        url = f"{self._config.auth_url}/device_authorization"
        body = {
            "client_id": self._config.client_id,
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = await self._session.post(url, data=body)
        content = await resp.json()
        return TidalDeviceAuth(**content)

    @retry(
        wait=wait_fixed(10),
        retry=retry_if_exception_type(ClientError),
        stop=stop_after_delay(300),
        after=after_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _login_with_device_code(self, device_authorization: TidalDeviceAuth) -> TidalToken:
        """
        Attempt to login with a device authorization.
        Retry while the device has not been approved via browser for 5 min.
        """
        url = f"{self._config.auth_url}/token"
        body = {
            "client_id": self._config.client_id,
            "scope": "r_usr+w_usr+w_sub",
            "device_code": device_authorization.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        resp = await self._session.post(
            url, data=body, auth=BasicAuth(self._config.client_id, self._config.client_secret)
        )
        content = await resp.json()
        return TidalToken(**content)

    async def _login_with_refresh_token(self) -> None:
        if not self._token:
            raise ValueError("👻 We need a token to login with refresh token")
        url = f"{self._config.auth_url}/token"
        body = {
            "client_id": self._config.client_id,
            "scope": "r_usr+w_usr+w_sub",
            "refresh_token": self._token.refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            resp = await self._session.post(
                url, data=body, auth=BasicAuth(self._config.client_id, self._config.client_secret)
            )
            content = await resp.json()
            self._token = TidalToken(**(self._token.dict() | content))
            self._save_token(self._token)
            logger.info("♻️ Refreshed access token")
            self._change_state(AuthState.ACCESS_TOKEN_REFRESHED)
            await self._verify_access_token()
        except ClientError:
            logger.warning("❌ Refresh token is not valid")
            self._change_state(AuthState.REFRESH_TOKEN_EXPIRED)

    async def _test_download(self) -> None:
        try:
            url = f"{self._config.api_hifi_url}/tracks/{self._config.test_track_id}/playbackinfopostpaywall"
            await self._session.get(
                str(url),
                headers={"Authorization": f"Bearer {self._token and self._token.access_token}"},
                params={
                    "audioquality": AudioQuality.LOW,
                    "playbackmode": PlaybackMode.STREAM,
                    "assetpresentation": AssetPresentation.FULL,
                    "countryCode": self._config.country_code,
                },
            )
        except ClientError:
            logger.warning("❌ Could not complete the test download, is the account subscribed?")
            self._change_state(AuthState.UNAUTHENTICATED)
