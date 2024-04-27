import json
import logging
from contextlib import suppress
from typing import Any

from aiohttp import BasicAuth, ClientError, ClientResponse, ClientSession
from pydantic import HttpUrl
from tenacity import (
    after_log,
    retry,
    retry_if_exception_type,
    stop_after_delay,
    wait_fixed,
)

from tidalidarr.tidal.models import (
    TidalConfig,
    TidalDeviceAuth,
    TidalLoginFailedError,
    TidalToken,
)

logger = logging.getLogger(__name__)


class TidalBaseClient:
    def __init__(self, config: TidalConfig, session: ClientSession) -> None:
        self._config = config
        self._session = session
        self._token: TidalToken | None = None

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
        is_authenticated: bool = False,
    ) -> ClientResponse:
        if not self._token:
            self._token = await self.login()
        url = url if isinstance(url, HttpUrl) else HttpUrl(url)
        if url.path != "/v1/sessions" and not (await self.verify_access_token()):
            self._token = await self.login()
        if is_authenticated:
            headers = (headers or {}) | {"Authorization": f"Bearer {self._token.access_token}"}
        return await self._session.request(
            method,
            str(url),
            auth=auth,
            data=data,
            headers=headers,
            json=json,
            params=params,
        )

    async def verify_access_token(self) -> bool:
        resp = await self._request("GET", "https://api.tidal.com/v1/sessions", is_authenticated=True)
        return resp.status == 200

    async def login(self) -> TidalToken:
        """
        Main login function, try to load the token from .json file
        Refresh the access token if necessary.
        Otherwise start a new login via device authorization
        """

        token = self.load_token()

        if not token:
            device_authorization = await self.get_device_authorization()
            logger.info(f"Please login: https://{device_authorization.verification_uri_complete}")
            try:
                token = await self.login_with_device_code(device_authorization)
            except ClientError as error:
                raise TidalLoginFailedError from error
        elif not (await self.verify_access_token()):
            logger.info(f"Logging with refresh_token (remaining time: {token.expires_in} hours)")
            token = await self.login_with_refresh_token()

        self.save_token(token)
        self._config.country_code = token.user.country_code
        logger.info("Now logged in")
        return token

    def load_token(self) -> TidalToken | None:
        with (
            suppress(json.JSONDecodeError, FileNotFoundError),
            self._config.token_path.open(mode="r", encoding="utf-8") as p,
        ):
            token = TidalToken(**json.load(p))
            self._token = token
            logger.info(f"Loaded token from {self._config.token_path}")
            return token
        return None

    def save_token(self, token: TidalToken) -> None:
        with self._config.token_path.open(mode="w", encoding="utf-8") as p:
            json.dump(token.dict(), p)
        logging.info(f"Token saved to {self._config.token_path}")

    async def login_with_refresh_token(self) -> TidalToken:
        if not self._token:
            raise ValueError("We need a token to login with refresh token")
        url = f"{self._config.auth_url}/token"
        body = {
            "client_id": self._config.client_id,
            "scope": "r_usr+w_usr+w_sub",
            "refresh_token": self._token.refresh_token,
            "grant_type": "refresh_token",
        }
        resp = await self._request(
            "POST",
            url,
            data=body,
            auth=BasicAuth(self._config.client_id, self._config.client_secret),
        )
        content = await resp.json()
        return TidalToken(**(self._token.dict() | content))

    async def get_device_authorization(self) -> TidalDeviceAuth:
        """
        When there is no token active, request a new device authorization.
        The response contains a link to access and login via browser
        """
        url = f"{self._config.auth_url}/device_authorization"
        body = {
            "client_id": self._config.client_id,
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = await self._request("POST", url, data=body)
        content = await resp.json()
        return TidalDeviceAuth(**content)

    @retry(
        wait=wait_fixed(60),
        retry=retry_if_exception_type(ClientError),
        stop=stop_after_delay(300),
        after=after_log(logger, logging.WARNING),
        reraise=True,
    )
    async def login_with_device_code(self, device_authorization: TidalDeviceAuth) -> TidalToken:
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
        resp = await self._request(
            "POST",
            url,
            data=body,
            auth=BasicAuth(self._config.client_id, self._config.client_secret),
        )
        content = await resp.json()
        return TidalToken(**content)
