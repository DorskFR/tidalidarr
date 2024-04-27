import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler

from overrides import overrides
from requests import Session

from tidalidarr.lidarr.client import LidarrClient, LidarrConfig
from tidalidarr.tidal.client import TidalClient
from tidalidarr.tidal.models import TidalConfig

logger = logging.getLogger(__name__)


class HTTPServerWithSession(HTTPServer):
    def __init__(self, server_address, RequestHandlerClass, session: Session | None = None) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self._session = session or Session()
        self.tidal_client = TidalClient(TidalConfig(), session)
        self.lidarr_client = LidarrClient(LidarrConfig(), session)


class GetHTTPRequestHandler(SimpleHTTPRequestHandler):
    @overrides
    def do_GET(self):  # noqa: N802
        path = self.path
        if path.startswith("/album"):
            album_id_str = path.removeprefix("/album").removeprefix("/")
            try:
                album_id = int(album_id_str)
                server: HTTPServerWithSession = self.server
                album = server.tidal_client.find_album(album_id)

                self.send_response(200)
                self.send_header("Content-type", "text/event-stream")
                self.end_headers()

                self.wfile.write(f"Downloading: {album.title}\n\n".encode())
                self.wfile.flush()

                for progress in server.tidal_client.download_album(album):
                    self.wfile.write(f"{progress}\n".encode())
                    self.wfile.flush()

                self.wfile.write(f"\nComplete: {album.folder}\n".encode())
            except ValueError:
                self.send_error(400, "Invalid album id")
        else:
            self.send_error(404, "Not supported")


def run_server() -> None:
    server_address = ("", 8000)
    with Session() as session:
        httpd = HTTPServerWithSession(server_address, GetHTTPRequestHandler, session)
        logger.info("Starting server...")
        httpd.serve_forever()
