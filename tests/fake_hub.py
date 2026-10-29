"""A local stand-in for the hub's module document store.

It applies the rules switchboard's docs/MODULES.md (Storage) states: a bearer
token, names of `[A-Za-z0-9._:-]+` at most 128 bytes, 256 KiB per value, 16 MiB
per module counted as collection, key and value bytes, and a list in key order
answering `next_after`. Its error bodies are shaped as
`internal/httpapi/docs.go` and `worker.ErrorAnswer` write them at switchboard
`0072c500`: `{"code": …, "detail": …}`, the detail left out when it is empty.
"""

import json
import re
from urllib.parse import parse_qs, urlsplit

from tests.support import _LocalServer

MODULE = "dota-replays"
MAX_VALUE = 256 * 1024
MAX_MODULE = 16 * 1024 * 1024
MAX_NAME = 128
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 100
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")


class _Invalid(Exception):
    """A request the store's rules refuse."""


class FakeHub(_LocalServer):
    """A document store that applies the hub's rules and counts the writes."""

    def __init__(self, token: str = "sbm_test_token", module: str = MODULE):
        self.token = token
        self.module = module
        # (collection, key) -> the value bytes the store holds.
        self.documents: dict[tuple[str, str], bytes] = {}
        self.requests: list[tuple[str, str]] = []
        # (method, collection, key) -> a status the next request answers.
        self.forced: dict[tuple[str, str, str], int] = {}
        self.updated_ms = 0
        super().__init__()

    # ==================== what a test reads ====================

    def paths(self, method: str | None = None) -> list[str]:
        """Return the paths the store answered, for one method or for all."""
        return [path for request, path in self.requests if method in (None, request)]

    def writes(self, method: str) -> int:
        """Return how many requests of a write method the store took."""
        return len([1 for request, _ in self.requests if request == method])

    def document(self, collection: str, key: str):
        """Return a stored document's value, or None when it holds none."""
        value = self.documents.get((collection, key))
        return None if value is None else json.loads(value)

    def collection(self, collection: str) -> dict[str, object]:
        """Return every stored document of a collection, by key."""
        return {
            key: json.loads(value)
            for (held, key), value in self.documents.items()
            if held == collection
        }

    def delete_document(self, collection: str, key: str) -> None:
        """Remove a document behind the publisher's back."""
        self.documents.pop((collection, key), None)

    def replace_document(self, collection: str, key: str, value) -> None:
        """Store a value behind the publisher's back."""
        self.documents[(collection, key)] = json.dumps(
            value, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def fail(self, method: str, collection: str, key: str, status: int) -> None:
        """Answer the next request for one document with a status."""
        self.forced[(method, collection, key)] = status

    def size(self) -> int:
        """Return the module's stored bytes, as docs/MODULES.md counts them."""
        return sum(
            len(collection.encode("utf-8")) + len(key.encode("utf-8")) + len(value)
            for (collection, key), value in self.documents.items()
        )

    # ==================== the store ====================

    def handle_request(self, handler, method: str) -> None:
        parsed = urlsplit(handler.path)
        self.requests.append((method, parsed.path))
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        try:
            status, payload = self.answer(method, parsed, body, handler.headers)
        except _Invalid as error:
            status, payload = 400, _error("invalid", str(error))
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def answer(self, method: str, parsed, body: bytes, headers):
        authorization = headers.get("Authorization") or ""
        if not authorization:
            return 401, _error("no-token")
        if authorization != f"Bearer {self.token}":
            return 401, _error("invalid-token")
        # The segments are read raw, as chi's RawPath routing does: a
        # percent-encoded name reaches the store still encoded and is refused.
        parts = parsed.path.split("/")
        if (
            len(parts) not in (6, 7)
            or parts[1:3] != ["api", "m"]
            or parts[4] != "docs"
        ):
            return 404, _error("not-found")
        module, collection = parts[3], parts[5]
        if module != self.module:
            return 403, _error("wrong-module")
        key = parts[6] if len(parts) == 7 else None
        check_name("collection", collection)
        if key is not None:
            check_name("key", key)
        forced = self.forced.pop((method, collection, key or ""), None)
        if forced is not None:
            return forced, _error("forced", f"the test forced {forced}")

        if method == "GET" and key is not None:
            value = self.documents.get((collection, key))
            if value is None:
                return 404, _error("not-found")
            return 200, value
        if method == "GET":
            return self.list(collection, parsed.query)
        if method == "PUT":
            return self.put(collection, key, body)
        if method == "DELETE":
            self.documents.pop((collection, key), None)
            return 204, b""
        return 405, _error("not-found")

    def list(self, collection: str, query: str):
        parameters = parse_qs(query)
        raw = parameters.get("limit", [""])[0]
        limit = DEFAULT_LIST_LIMIT
        if raw:
            try:
                limit = int(raw)
            except ValueError:
                raise _Invalid(f"limit must be 1 to {MAX_LIST_LIMIT}")
            if limit < 1 or limit > MAX_LIST_LIMIT:
                raise _Invalid(f"limit must be 1 to {MAX_LIST_LIMIT}")
        after = parameters.get("after", [""])[0]
        if after:
            check_name("after", after)
        keys = sorted(
            key for (held, key) in self.documents if held == collection and key > after
        )
        page = keys[:limit]
        next_after = page[-1] if len(page) == limit else None
        answer = {
            "schema": 1,
            "docs": [
                {
                    "key": key,
                    "value": json.loads(self.documents[(collection, key)]),
                    "updated_ms": 0,
                }
                for key in page
            ],
            # The store always answers the field, null on the last page.
            "next_after": next_after,
        }
        return 200, json.dumps(answer, separators=(",", ":")).encode("utf-8")

    def put(self, collection: str, key: str | None, body: bytes):
        if key is None:
            return 405, _error("not-found")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _Invalid(f"the value is not a JSON object: {error}")
        if not isinstance(parsed, dict):
            raise _Invalid("the value is not a JSON object")
        value = json.dumps(
            parsed, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(value) > MAX_VALUE:
            return 413, _error(
                "too-large", f"a document value is at most {MAX_VALUE} bytes"
            )
        held = len(self.documents.get((collection, key), b""))
        used = self.size()
        size = len(collection.encode("utf-8")) + len(key.encode("utf-8")) + len(value)
        if used - held + size > MAX_MODULE:
            return 413, _error(
                "too-large",
                f"the module holds {used} bytes, the limit is {MAX_MODULE}",
            )
        self.documents[(collection, key)] = value
        self.updated_ms += 1
        return 204, b""


def check_name(what: str, name: str) -> None:
    """Refuse a collection or key outside the store's pattern."""
    if len(name.encode("utf-8")) < 1 or len(name.encode("utf-8")) > MAX_NAME:
        raise _Invalid(f"the {what} is not 1 to {MAX_NAME} bytes")
    if not NAME_PATTERN.match(name):
        raise _Invalid(f"the {what} is not 1 to {MAX_NAME} bytes of [A-Za-z0-9._:-]")


def _error(code: str, detail: str = "") -> bytes:
    """Return an error answer's body."""
    answer = {"code": code}
    if detail:
        answer["detail"] = detail
    return json.dumps(answer).encode("utf-8")
