import hmac
import json
from typing import Any

from aiohttp import ClientResponseError
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tidalidarr.tidal.client import TidalClient


class ApiConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="tidalidarr_")

    api_key: str = ""


class SearchRequest(BaseModel):
    artist: str
    album: str
    year: int | None = None


class GrabRequest(BaseModel):
    id: str


def _error(message: str, status_code: int, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message} | extra, status_code=status_code)


def _unauthorized(request: Request) -> JSONResponse | None:
    expected: str = request.state.api_config.api_key
    if not expected:
        return _error("API disabled: TIDALIDARR_API_KEY is not set", 503)
    provided = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        return _error("Invalid API key", 401)
    return None


def _not_logged_in(client: TidalClient) -> JSONResponse | None:
    if client.is_logged_in:
        return None
    return _error("Tidal is not logged in", 503, auth_url=client.auth_url or None)


async def _body[T: BaseModel](request: Request, model: type[T]) -> T | JSONResponse:
    try:
        return model(**await request.json())
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        return _error(f"Invalid request body: {error}", 400)


async def health(request: Request) -> Response:
    if denied := _unauthorized(request):
        return denied
    client: TidalClient = request.state.tidal_client
    return JSONResponse({"logged_in": client.is_logged_in, "auth_url": client.auth_url or None})


async def search(request: Request) -> Response:
    if denied := _unauthorized(request):
        return denied
    client: TidalClient = request.state.tidal_client
    if pending := _not_logged_in(client):
        return pending
    body = await _body(request, SearchRequest)
    if isinstance(body, JSONResponse):
        return body
    candidates = await client.search_albums(body.artist, body.album, body.year)
    return JSONResponse([c.model_dump(mode="json") for c in candidates])


async def create_grab(request: Request) -> Response:
    if denied := _unauthorized(request):
        return denied
    client: TidalClient = request.state.tidal_client
    if pending := _not_logged_in(client):
        return pending
    body = await _body(request, GrabRequest)
    if isinstance(body, JSONResponse):
        return body
    if not body.id.isdigit():
        return _error("Invalid album id", 400)
    try:
        album = await client.find_album(int(body.id))
    except ClientResponseError as error:
        if error.status == 404:
            return _error("Album not found on Tidal", 404)
        return _error(f"Tidal returned HTTP {error.status}", 502)
    job = client.grab(album)
    return JSONResponse(job.model_dump(mode="json"), status_code=202)


async def get_grab(request: Request) -> Response:
    if denied := _unauthorized(request):
        return denied
    client: TidalClient = request.state.tidal_client
    job = client.grabs.get(request.path_params["grab_id"])
    if job is None:
        return _error("Grab not found", 404)
    return JSONResponse(job.model_dump(mode="json"))


async def delete_grab(request: Request) -> Response:
    if denied := _unauthorized(request):
        return denied
    client: TidalClient = request.state.tidal_client
    if client.cancel_grab(request.path_params["grab_id"]) is None:
        return _error("Grab not found", 404)
    return Response(status_code=204)
