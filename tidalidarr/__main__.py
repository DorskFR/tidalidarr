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
    lidarr_client: LidarrClient
    background_tasks: set[asyncio.Task]


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncIterator[State]:
    async with ClientSession(
        headers={"User-Agent": USER_AGENT}, raise_for_status=True, timeout=ClientTimeout(60)
    ) as session:
        tidal_client = TidalClient(TidalConfig(), session)
        lidarr_client = LidarrClient(LidarrConfig(), session)
        task_handles = {
            asyncio.create_task(periodic_check(tidal_client, lidarr_client)),
            asyncio.create_task(tidal_client.process_queue()),
        }
        try:
            yield {
                "session": session,
                "tidal_client": tidal_client,
                "lidarr_client": lidarr_client,
                "background_tasks": task_handles,
            }
        finally:
            for handle in task_handles:
                with contextlib.suppress(asyncio.CancelledError):
                    handle.cancel()


async def periodic_check(tidal_client: TidalClient, lidarr_client: LidarrClient) -> None:
    while True:
        logger.info("Checking all missing albums")
        async for query in lidarr_client.get_missing_albums():
            if not (await tidal_client.search(query)) and contains_japanese(query):
                await tidal_client.search(romanize(query))
            # Let's nest this so that we try to import often instead of waiting for all downloads to be processed
            async for path in tidal_client.get_ready_paths():
                await lidarr_client.trigger_import(path)
                await lidarr_client.manual_import(path)
            await asyncio.sleep(0)
        logger.info("Finished periodic check, sleeping 60 seconds")
        lidarr_client.cleanup_download_folder()  # only cleanup after imports are done
        await asyncio.sleep(60)


async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse(content="OK")


async def index(_request: Request) -> PlainTextResponse:
    return PlainTextResponse(content="Hello!")


async def queue_info(request: Request) -> JSONResponse:
    queue_information = request.state.tidal_client.get_queue_information()
    return JSONResponse(content=json.loads(queue_information.json()))


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
        Route("/album/{album_id}", endpoint=get_album, methods=["GET"]),
        Route("/queue", endpoint=queue_info, methods=["GET"]),
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
