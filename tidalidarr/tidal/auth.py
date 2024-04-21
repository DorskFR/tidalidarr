import json
import logging
from contextlib import suppress

from overrides import overrides
from requests import PreparedRequest, Session
from requests.auth import AuthBase
from requests.exceptions import HTTPError
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


class TidalAuth(AuthBase):
    def __init__(self, config: TidalConfig, session: Session) -> None:
        self._config = config
        self._session = session
        self._token = self.login()

    @overrides
    def __call__(self, r: PreparedRequest) -> PreparedRequest:
        if r.path_url != "/v1/sessions" and not self.verify_access_token():
            self._token = self.login()
        r.headers.update({"Authorization": f"Bearer {self._token.access_token}"})
        return r

    def get_device_authorization(self) -> TidalDeviceAuth:
        url = f"{self._config.auth_url}/device_authorization"
        body = {
            "client_id": self._config.client_id,
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = self._session.post(url, data=body)
        resp.raise_for_status()
        content = resp.json()
        return TidalDeviceAuth(**content)

    @retry(
        wait=wait_fixed(60),
        retry=retry_if_exception_type(HTTPError),
        stop=stop_after_delay(300),
        after=after_log(logger, logging.WARNING),
        reraise=True,
    )
    def login_with_device_code(self, device_authorization: TidalDeviceAuth) -> TidalToken:
        url = f"{self._config.auth_url}/token"
        body = {
            "client_id": self._config.client_id,
            "scope": "r_usr+w_usr+w_sub",
            "device_code": device_authorization.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        resp = self._session.post(
            url,
            data=body,
            auth=(self._config.client_id, self._config.client_secret),
        )
        resp.raise_for_status()
        content = resp.json()
        return TidalToken(**content)

    def login_with_refresh_token(self) -> TidalToken:
        url = f"{self._config.auth_url}/token"
        body = {
            "client_id": self._config.client_id,
            "scope": "r_usr+w_usr+w_sub",
            "refresh_token": self._token.refresh_token,
            "grant_type": "refresh_token",
        }
        resp = self._session.post(
            url,
            data=body,
            auth=(self._config.client_id, self._config.client_secret),
        )
        resp.raise_for_status()
        content = resp.json()
        return TidalToken(**(self._token.dict() | content))

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

    def verify_access_token(self) -> bool:
        resp = self._session.get("https://api.tidal.com/v1/sessions", auth=self)
        return resp.status_code == 200

    def login(self) -> TidalToken:
        token = self.load_token()

        if not token:
            device_authorization = self.get_device_authorization()
            logger.info(f"Please login: https://{device_authorization.verification_uri_complete}")
            try:
                token = self.login_with_device_code(device_authorization)
            except HTTPError as error:
                raise TidalLoginFailedError from error
        elif not self.verify_access_token():
            logger.info(f"Logging with refresh_token (remaining time: {token.expires_in} hours)")
            token = self.login_with_refresh_token()

        self.save_token(token)
        self._config.country_code = token.user.country_code
        logger.info("Now logged in")
        return token
