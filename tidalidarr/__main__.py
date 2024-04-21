import logging
import time

import requests

from tidalidarr.lidarr.client import LidarrClient, LidarrConfig
from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import TidalConfig

logging.basicConfig(level="INFO", format="%(message)s", datefmt="[%X]")
logger = logging.getLogger(__name__)


def main() -> None:
    with requests.Session() as session:
        tidal_client = TidalClient(TidalConfig(), session)
        lidarr_client = LidarrClient(LidarrConfig(), session)

        while True:
            for query in lidarr_client.get_missing_albums():
                lidarr_client.cleanup_download_folder()
                if path := tidal_client.search(query):
                    lidarr_client.manual_import(path)
                    lidarr_client.trigger_import(path)

            logger.info("Finished checking all missing albums, waiting 60 seconds before next iteration")
            time.sleep(60)


if __name__ == "__main__":
    main()
