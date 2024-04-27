import logging
import os
import time
from threading import Thread

import requests
import sentry_sdk

from tidalidarr.api.server import run_server
from tidalidarr.lidarr.client import LidarrClient, LidarrConfig
from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import TidalConfig
from tidalidarr.utils import contains_japanese, romanize

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    with requests.Session() as session:
        tidal_client = TidalClient(TidalConfig(), session)
        lidarr_client = LidarrClient(LidarrConfig(), session)

        while True:
            for query in lidarr_client.get_missing_albums():
                lidarr_client.cleanup_download_folder()
                path = tidal_client.search(query)
                path = path or tidal_client.search(romanize(query)) if contains_japanese(query) else None
                if path:
                    lidarr_client.manual_import(path)
                    lidarr_client.trigger_import(path)

            logger.info("Finished checking all missing albums, waiting 60 seconds before next iteration")
            time.sleep(60)


if __name__ == "__main__":
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        release=f"tidalidarr@v{os.getenv('IMAGE_VERSION', 'latest')}",
        environment=os.getenv("SENTRY_ENVIRONMENT", "development"),
        sample_rate=1.0,
        enable_tracing=True,
        traces_sample_rate=1.0,
    )
    server_thread = Thread(target=run_server)
    server_thread.start()
    # main()
