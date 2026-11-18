"""Local HTTP servers standing in for OpenDota and the replay CDN.

The OpenDota fake serves the answers tests/record-opendota recorded, so its
messages come from real responses. Both fakes bind an ephemeral port on the
loopback interface and run in a thread, so a test talks to them over HTTP.
"""

import json
import os
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "opendota.json"
# tests/record-opendota --parse wrote this: one parse request and its polls.
PARSE_FIXTURE_PATH = FIXTURE_PATH.with_name("opendota-parse.json")


def closed_port() -> int:
    """Return a loopback port that refuses connections on this host.

    Bind an ephemeral port, read it, and close the socket: nothing listens
    there afterwards, so a client's connect is refused at once. A fixed port
    such as 9 is refused on macOS but filtered on WSL2, where each request then
    waits for its timeout instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


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
        # close() waits for serve_forever to see the shutdown, which it checks
        # once per poll interval; the default 0.5 s added up to half the suite.
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    def _build_handler(self):
        owner = self

        class Handler(_QuietHandler):
            def do_GET(self):
                owner.handle_request(self, "GET")

            def do_POST(self):
                owner.handle_request(self, "POST")

            def do_PUT(self):
                owner.handle_request(self, "PUT")

            def do_DELETE(self):
                owner.handle_request(self, "DELETE")

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
    """Serve the recorded OpenDota answers, with injectable failures.

    `responses` holds the GET answers by path. The parse fixture's answers are
    kept by method and path and served in their recorded order, the last one
    repeating. `targets` records each request's method, raw target (path and
    query) and headers, as the client sent them.
    """

    def __init__(self, fixture_path: str | Path = FIXTURE_PATH,
                 parse_fixture_path: str | Path = PARSE_FIXTURE_PATH):
        self.fixture = load_fixture(fixture_path)
        self.responses = {
            entry["path"]: entry["body"] for entry in self.fixture["responses"]
        }
        self.parse_fixture = load_fixture(parse_fixture_path)
        self.sequences: dict[tuple[str, str], list] = {}
        for entry in self.parse_fixture["responses"]:
            key = (entry["method"], entry["path"])
            self.sequences.setdefault(key, []).append(entry["body"])
        self.failures: dict[tuple[str, str], list[int]] = {}
        self.truncations: dict[str, int] = {}
        self.raw: dict[str, tuple[str, bytes]] = {}
        self.requests: list[tuple[str, str]] = []
        self.targets: list[tuple[str, str, dict[str, str]]] = []
        super().__init__()

    def fail_next(self, path: str, statuses: list[int], method: str = "GET") -> None:
        """Answer the next len(statuses) requests for path with these statuses."""
        self.failures[(method, path)] = list(statuses)

    def truncate_next(self, path: str, times: int = 1) -> None:
        """Send the next times answers for path with their body cut short."""
        self.truncations[path] = times

    def answer_raw(self, path: str, body: bytes, content_type: str = "text/html") -> None:
        """Answer every request for path with 200 and this body, JSON or not."""
        self.raw[path] = (content_type, body)

    def _write(self, handler: BaseHTTPRequestHandler, status: int, body: bytes,
               content_type: str = "application/json") -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def handle_request(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = urlsplit(handler.path).path
        self.requests.append((method, path))
        self.targets.append((method, handler.path, dict(handler.headers.items())))
        statuses = self.failures.get((method, path))
        if statuses:
            status = statuses.pop(0)
            self._write(handler, status, b'{"error": "injected failure"}')
            return
        sequence = self.sequences.get((method, path))
        if sequence:
            body = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            self._write(handler, 200, json.dumps(body).encode("utf-8"))
            return
        if method != "GET":
            self._write(handler, 404, b'{"error": "not recorded"}')
            return
        if path in self.raw:
            content_type, body = self.raw[path]
            self._write(handler, 200, body, content_type)
            return
        if path not in self.responses:
            self._write(handler, 404, b'{"error": "not recorded"}')
            return

        body = json.dumps(self.responses[path]).encode("utf-8")
        truncations = self.truncations.get(path, 0)
        if truncations:
            # Announce the whole body and send half of it, so the client's
            # read() raises IncompleteRead as it would on a stalled connection.
            self.truncations[path] = truncations - 1
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body[: len(body) // 2])
            handler.wfile.flush()
            handler.close_connection = True
            return

        self._write(handler, 200, body)


class ReplayServer(_LocalServer):
    """Serve one replay body, optionally cutting the first attempt short."""

    def __init__(
        self,
        body: bytes,
        truncate_first: bool = False,
        status: int = 200,
        content_encoding: str | None = None,
        content_type: str = "application/x-bzip2",
    ):
        self.body = body
        self.truncate_first = truncate_first
        self.status = status
        self.content_encoding = content_encoding
        self.content_type = content_type
        self.requests: list[tuple[str, str]] = []
        super().__init__()

    def handle_request(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        self.requests.append((method, urlsplit(handler.path).path))
        if self.status != 200:
            handler.send_response(self.status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        handler.send_response(200)
        handler.send_header("Content-Type", self.content_type)
        if self.content_encoding is not None:
            handler.send_header("Content-Encoding", self.content_encoding)
        handler.send_header("Content-Length", str(len(self.body)))
        handler.end_headers()
        if self.truncate_first and len(self.requests) == 1:
            handler.wfile.write(self.body[: len(self.body) // 2])
            handler.wfile.flush()
            handler.close_connection = True
            return
        handler.wfile.write(self.body)
