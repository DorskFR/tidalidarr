import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import TypedDict

import sentry_sdk
import uvicorn
from aiohttp import ClientSession, ClientTimeout
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from tidalidarr import api
from tidalidarr.grabs import GrabStore
from tidalidarr.lidarr.client import LidarrClient, LidarrConfig
from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import TidalConfig
from tidalidarr.utils import USER_AGENT, contains_japanese, romanize

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    release=f"tidalidarr@v{os.getenv('IMAGE_VERSION', 'latest')}",
    environment=os.getenv("SENTRY_ENVIRONMENT", "development"),
    sample_rate=1.0,
    enable_tracing=True,
    traces_sample_rate=1.0,
)


class State(TypedDict):
    session: ClientSession
    tidal_client: TidalClient
    lidarr_client: LidarrClient | None
    api_config: api.ApiConfig
    background_tasks: set[asyncio.Task]


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncIterator[State]:
    async with ClientSession(
        headers={"User-Agent": USER_AGENT}, raise_for_status=True, timeout=ClientTimeout(60)
    ) as session:
        tidal_config = TidalConfig()
        lidarr_config = LidarrConfig()
        if lidarr_config.enabled and not lidarr_config.api_key:
            raise RuntimeError("LIDARR_API_KEY is required unless LIDARR_ENABLED=false")
        api_config = api.ApiConfig()
        tidal_client = TidalClient(
            tidal_config, session, GrabStore(tidal_config.grabs_path), track_ready=lidarr_config.enabled
        )
        lidarr_client = LidarrClient(lidarr_config, session) if lidarr_config.enabled else None
        task_handles = {
            asyncio.create_task(startup(tidal_client)),
            asyncio.create_task(tidal_client.process_queue()),
        }
        if lidarr_client:
            task_handles.add(asyncio.create_task(periodic_check(tidal_client, lidarr_client)))
        else:
            logger.info("Lidarr sync disabled")
        if not api_config.api_key:
            logger.warning("TIDALIDARR_API_KEY is not set, search and grab API disabled")
        try:
            yield {
                "session": session,
                "tidal_client": tidal_client,
                "lidarr_client": lidarr_client,
                "api_config": api_config,
                "background_tasks": task_handles,
            }
        finally:
            for handle in task_handles:
                with contextlib.suppress(asyncio.CancelledError):
                    handle.cancel()


async def startup(tidal_client: TidalClient) -> None:
    while not tidal_client.is_logged_in:
        try:
            await tidal_client.login()
        except Exception:
            logger.exception("Tidal login failed, retrying in 60 seconds")
            await asyncio.sleep(60)
    try:
        await tidal_client.resume_pending()
    except Exception:
        logger.exception("Resuming pending grabs failed")


async def periodic_check(tidal_client: TidalClient, lidarr_client: LidarrClient) -> None:
    while True:
        try:
            logger.info("Checking all missing albums")
            async for query in lidarr_client.get_missing_albums():
                if not (await tidal_client.search(query)) and contains_japanese(query):
                    await tidal_client.search(romanize(query))
                # Let's nest this so that we try to import often instead of waiting for all downloads to be processed
                async for path in tidal_client.get_ready_paths():
                    await lidarr_client.trigger_import(path)
                    await lidarr_client.manual_import(path)
                await asyncio.sleep(0)
            lidarr_client.cleanup_download_folder()  # only cleanup after imports are done
        except Exception:
            logger.exception("There was an error during periodic check")
        finally:
            logger.info("Finished periodic check, sleeping 60 seconds")
            await asyncio.sleep(60)


async def healthz(request: Request) -> PlainTextResponse:
    tidal_client: TidalClient = request.state.tidal_client
    return (
        PlainTextResponse(content="OK")
        if tidal_client.is_logged_in
        else PlainTextResponse(content="UNAUTHENTICATED", status_code=401)
    )


async def index(_request: Request) -> PlainTextResponse:
    return PlainTextResponse(content="Hello!")


async def queue_info(request: Request) -> JSONResponse:
    queue_information = request.state.tidal_client.get_queue_information()
    return JSONResponse(content=json.loads(queue_information.json()))


async def auth(request: Request) -> PlainTextResponse:
    tidal_client: TidalClient = request.state.tidal_client
    return (
        PlainTextResponse(content="Logged in")
        if tidal_client.is_logged_in
        else PlainTextResponse(content=tidal_client.auth_url)
    )


async def get_album(request: Request) -> JSONResponse:
    tidal_client: TidalClient = request.state.tidal_client
    try:
        album_id = int(request.path_params["album_id"])
        album = await tidal_client.find_album(album_id)
        result = await tidal_client.enqueue_album(album)
        return JSONResponse({"result": result}, status_code=200)
    except ValueError:
        return JSONResponse({"error": "Invalid album id"}, status_code=400)


app = Starlette(
    routes=[
        Route("/healthz", endpoint=healthz, methods=["GET"]),
        Route("/auth", endpoint=auth, methods=["GET"]),
        Route("/album/{album_id}", endpoint=get_album, methods=["GET"]),
        Route("/queue", endpoint=queue_info, methods=["GET"]),
        Route("/health", endpoint=api.health, methods=["GET"]),
        Route("/search", endpoint=api.search, methods=["POST"]),
        Route("/grabs", endpoint=api.create_grab, methods=["POST"]),
        Route("/grabs/{grab_id}", endpoint=api.get_grab, methods=["GET"]),
        Route("/grabs/{grab_id}", endpoint=api.delete_grab, methods=["DELETE"]),
        Route("/", endpoint=index, methods=["GET"]),
    ],
    lifespan=lifespan,
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["*"],
        ),
    ],
)

if __name__ == "__main__":
    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("TIDALIDARR_UVICORN_PORT", "8000")))
