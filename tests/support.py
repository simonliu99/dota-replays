"""Local HTTP servers standing in for OpenDota and the replay CDN.

The OpenDota fake serves the answers tests/record-opendota recorded, so its
messages come from real responses. Both fakes bind an ephemeral port on the
loopback interface and run in a thread, so a test talks to them over HTTP.
"""

import json
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "opendota.json"


def load_fixture(path: str | Path = FIXTURE_PATH) -> dict:
    """Return the fixture tests/record-opendota wrote."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


class WorkingDirectory:
    """Run a block with the process's working directory set to a path."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __enter__(self) -> Path:
        self._previous = Path.cwd()
        os.chdir(self.path)
        return self.path

    def __exit__(self, *exc_info) -> bool:
        os.chdir(self._previous)
        return False


class _QuietHandler(BaseHTTPRequestHandler):
    """A handler that writes no access log lines."""

    def log_message(self, format: str, *args) -> None:
        pass


class _HttpServer(ThreadingHTTPServer):
    """An HTTP server that skips HTTPServer's reverse DNS lookup on bind."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class _LocalServer:
    """One loopback HTTP server on an ephemeral port, serving from a thread."""

    def __init__(self):
        self._server = _HttpServer(("127.0.0.1", 0), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _build_handler(self):
        owner = self

        class Handler(_QuietHandler):
            def do_GET(self):
                owner.handle_request(self, "GET")

            def do_POST(self):
                owner.handle_request(self, "POST")

        return Handler

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def handle_request(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


class RecordedOpenDota(_LocalServer):
    """Serve the recorded OpenDota answers, with injectable failures."""

    def __init__(self, fixture_path: str | Path = FIXTURE_PATH):
        self.fixture = load_fixture(fixture_path)
        self.responses = {
            entry["path"]: entry["body"] for entry in self.fixture["responses"]
        }
        self.failures: dict[tuple[str, str], list[int]] = {}
        self.requests: list[tuple[str, str]] = []
        super().__init__()

    def fail_next(self, path: str, statuses: list[int], method: str = "GET") -> None:
        """Answer the next len(statuses) requests for path with these statuses."""
        self.failures[(method, path)] = list(statuses)

    def handle_request(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = urlsplit(handler.path).path
        self.requests.append((method, path))
        statuses = self.failures.get((method, path))
        if statuses:
            status = statuses.pop(0)
            body = b'{"error": "injected failure"}'
        elif path in self.responses:
            status = 200
            body = json.dumps(self.responses[path]).encode("utf-8")
        else:
            status = 404
            body = b'{"error": "not recorded"}'
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


class ReplayServer(_LocalServer):
    """Serve one replay body, optionally cutting the first attempt short."""

    def __init__(self, body: bytes, truncate_first: bool = False):
        self.body = body
        self.truncate_first = truncate_first
        self.requests: list[tuple[str, str]] = []
        super().__init__()

    def handle_request(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        self.requests.append((method, urlsplit(handler.path).path))
        handler.send_response(200)
        handler.send_header("Content-Type", "application/x-bzip2")
        handler.send_header("Content-Length", str(len(self.body)))
        handler.end_headers()
        if self.truncate_first and len(self.requests) == 1:
            handler.wfile.write(self.body[: len(self.body) // 2])
            handler.wfile.flush()
            handler.close_connection = True
            return
        handler.wfile.write(self.body)
