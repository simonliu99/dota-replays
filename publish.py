#!/usr/bin/env python3
"""Publish the match index to the switchboard document store.

The index is one document per player per month, newest month first, and holds
whole months while the module's stored bytes stay within the 12 MiB budget.
Each run reads the store's own listing and writes only the documents whose
value changed, so a hub restored from an older copy is repaired on the next
run without a local "published" table. `--check` compares the published index
with the database and exits 1 on any difference.

The store's rules are switchboard's docs/MODULES.md (Storage): a value of at
most 256 KiB, 16 MiB per module counted as collection, key and value bytes,
names of `[A-Za-z0-9._:-]+`, a list of at most 200 in key order answering
`next_after`, a PUT answering 204 and a DELETE answering 204 whether or not
the document existed.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from opendota_client import OpenDotaClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("publish")

MODULE = "dota-replays"
DATABASE_NAME = "dota_replays.db"
MONTH_COLLECTION = "matches.{player_id}"

# One list answers this many documents; the plan reads a full diff in at most
# (players × months) / 20 calls.
LIST_PAGE = 20
# A value is at most 256 KiB, and a month document is never split.
MAX_VALUE = 256 * 1024
# 75% of the store's 16 MiB per module, leaving room for the meta documents
# and a month's growth between runs.
MODULE_BUDGET = 12 * 1024 * 1024
# The page links a replay only when its name matches the hub's bulk file-name
# pattern (switchboard docs/MODULES.md, Storage; plan §3a), so a recorded name
# outside it is published as null.
BULK_FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Differences one check run logs before it says how many it left out.
REPORT_LIMIT = 10
# The summary's `published_ms` is sized with this many digits, so the budget
# does not change between two runs in the same millisecond era.
PUBLISHED_MS_DIGITS = 13


def encode(value) -> bytes:
    """Return the compact, sorted JSON the store holds for a document."""
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _directory(name: str) -> Path | None:
    """Return the directory an environment variable names, or None."""
    value = os.environ.get(name)
    if not value:
        logger.error(f"{name} is not set")
        return None
    path = Path(value)
    if not path.is_dir():
        logger.error(f"{name} is not a directory: {path}")
        return None
    return path


def _connect(path: Path) -> sqlite3.Connection:
    """Open the database read-only: the index is built from it, never written."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _count(connection: sqlite3.Connection, query: str) -> int:
    """Return the single count a query selects."""
    return connection.execute(query).fetchone()[0]


def _month_key(start_time: int) -> tuple[int, str]:
    """Return the month and the store key of a match's start time.

    The month is UTC, so two hosts group a match the same way. The key counts
    down from 999999, which makes the store's key order newest month first.
    """
    moment = datetime.fromtimestamp(start_time, timezone.utc)
    month = moment.year * 100 + moment.month
    return month, f"y:{999999 - month:06d}"


def _month_name(month: int) -> str:
    """Return a month number as `yyyy-mm`."""
    return f"{month // 100:04d}-{month % 100:02d}"


def _replay_file(download: sqlite3.Row | None, invalid: list) -> str | None:
    """Return the recorded replay file's name when the page may link it."""
    if download is None or not download["on_disk"]:
        return None
    name = download["filename"] or ""
    if BULK_FILE_PATTERN.match(name):
        return name
    invalid.append(name)
    return None


def _match_entry(row: sqlite3.Row, parsed: dict, download: sqlite3.Row | None,
                 invalid: list) -> dict:
    """Return one month document's match entry."""
    if row["radiant_win"] is None or row["player_slot"] is None:
        win = None
    else:
        win = bool(row["radiant_win"]) == (row["player_slot"] < 128)
    return {
        "match_id": row["match_id"],
        "start_time": row["start_time"],
        "duration": row["duration"],
        "hero_id": row["hero_id"],
        "kills": row["kills"],
        "deaths": row["deaths"],
        "assists": row["assists"],
        "win": win,
        "parsed": bool(parsed),
        "replay_file": _replay_file(download, invalid),
    }


def _base_counts(connection: sqlite3.Connection, players: int) -> dict:
    """Return the summary's counts that do not depend on the indexed months."""
    return {
        "total_matches": _count(connection, "SELECT COUNT(*) FROM matches"),
        "players": players,
        "with_details": _count(connection, "SELECT COUNT(*) FROM match_details"),
        "parsed": _count(
            connection, "SELECT COUNT(*) FROM match_details WHERE is_parsed = TRUE"
        ),
        "with_replay_url": _count(
            connection,
            "SELECT COUNT(*) FROM match_details WHERE replay_url IS NOT NULL",
        ),
        "downloads_on_disk": _count(
            connection, "SELECT COUNT(*) FROM downloads WHERE on_disk = TRUE"
        ),
        "failed_downloads": _count(connection, "SELECT COUNT(*) FROM download_attempts"),
        "exhausted_retries": _count(
            connection, "SELECT COUNT(*) FROM download_attempts WHERE attempt_count >= 2"
        ),
    }


def _summary_counts(base: dict, indexed_matches: int,
                    oldest_month: int | None) -> dict:
    """Return the summary document's counts."""
    return {
        **base,
        "indexed_matches": indexed_matches,
        "oldest_indexed_month": (
            _month_name(oldest_month) if oldest_month is not None else None
        ),
    }


def _used_bytes(documents: dict) -> int:
    """Return the store's size of the documents: collection, key and value."""
    return sum(
        len(collection.encode("utf-8")) + len(key.encode("utf-8")) + len(value)
        for (collection, key), value in documents.items()
    )


def build_index(connection: sqlite3.Connection, published_ms: int,
                heroes_value: bytes | None = None,
                budget: int = MODULE_BUDGET) -> tuple[dict, dict]:
    """Return the index's documents and what the run did.

    `documents` maps (collection, key) to the value bytes to store, including
    `meta/players`, `meta/heroes` when given, and `meta/summary`. Months are
    added newest first while the module's stored bytes stay within `budget`; a
    month over the 256 KiB value limit is left out and reported.
    """
    players = [
        dict(row)
        for row in connection.execute(
            "SELECT player_id, persona_name FROM players ORDER BY player_id"
        )
    ]
    parsed = {
        row["match_id"]: row["is_parsed"]
        for row in connection.execute("SELECT match_id, is_parsed FROM match_details")
    }
    downloads = {
        row["match_id"]: row
        for row in connection.execute(
            "SELECT match_id, filename, on_disk FROM downloads"
        )
    }
    invalid: list[str] = []

    months: dict[tuple[int, int], dict] = {}
    for player in players:
        player_id = player["player_id"]
        for row in connection.execute(
            "SELECT * FROM matches WHERE player_id = ? "
            "ORDER BY start_time DESC, match_id DESC",
            (player_id,),
        ):
            month, key = _month_key(row["start_time"])
            document = months.setdefault(
                (player_id, month),
                {
                    "collection": MONTH_COLLECTION.format(player_id=player_id),
                    "key": key,
                    "matches": [],
                },
            )
            document["matches"].append(
                _match_entry(
                    row,
                    parsed.get(row["match_id"]),
                    downloads.get(row["match_id"]),
                    invalid,
                )
            )

    values = {slot: encode({"matches": document["matches"]})
              for slot, document in months.items()}
    oversized = sorted(
        [
            (player_id, month)
            for (player_id, month), value in values.items()
            if len(value) > MAX_VALUE
        ]
    )
    oversized_slots = set(oversized)

    meta = {("meta", "players"): encode({"players": players})}
    if heroes_value is not None:
        meta[("meta", "heroes")] = heroes_value

    ordered_months = sorted(
        {
            month
            for (player_id, month) in months
            if (player_id, month) not in oversized_slots
        },
        reverse=True,
    )
    selected = list(ordered_months)
    base = _base_counts(connection, len(players))
    while True:
        documents = dict(meta)
        indexed_matches = 0
        for player_id, month in months:
            if month not in selected or (player_id, month) in oversized_slots:
                continue
            document = months[(player_id, month)]
            documents[(document["collection"], document["key"])] = values[
                (player_id, month)
            ]
            indexed_matches += len(document["matches"])
        oldest = selected[-1] if selected else None
        counts = _summary_counts(base, indexed_matches, oldest)
        # The summary's own size joins the budget, so the last month fits with
        # the counts it publishes.
        sized = dict(counts)
        sized["published_ms"] = 10 ** (PUBLISHED_MS_DIGITS - 1)
        documents[("meta", "summary")] = encode(sized)
        if _used_bytes(documents) <= budget or not selected:
            break
        selected.pop()

    counts = _summary_counts(base, indexed_matches, oldest)
    documents[("meta", "summary")] = encode(
        {"published_ms": published_ms, **counts}
    )
    info = {
        "month_documents": len(selected),
        "indexed_matches": indexed_matches,
        "total_matches": counts["total_matches"],
        "players": len(players),
        "oversized": oversized,
        "kept": {
            (months[(player_id, month)]["collection"],
             months[(player_id, month)]["key"])
            for (player_id, month) in oversized
        },
        "invalid_names": invalid,
    }
    return documents, info


class StoreError(Exception):
    """The hub answered something other than the document store's contract."""


class Store:
    """The module's document store routes on the hub."""

    def __init__(self, hub: str, token: str, timeout: int = 30):
        self.base_url = hub.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, collection: str, key: str | None = None,
                 query: str = "", body: bytes | None = None) -> tuple[int, bytes]:
        """Send one request and return its status and body."""
        # Collection and key go raw: both are limited to `[A-Za-z0-9._:-]`,
        # whose characters are valid in a path segment, and the hub's router
        # matches on the raw path, so an encoded `:` is refused as a name.
        path = f"/api/m/{MODULE}/docs/{collection}"
        if key is not None:
            path += f"/{key}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path + query, data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except OSError as error:
            # A read timeout is an OSError and not a URLError; both are the
            # store's error path.
            reason = getattr(error, "reason", error)
            raise StoreError(f"{method} {path} failed: {reason}") from error

    def _answer(self, method: str, path: str, status: int, body: bytes,
                wanted: tuple[int, ...]) -> bytes:
        """Return the body of an answer in `wanted`, or raise with its status."""
        if status in wanted:
            return body
        code = detail = ""
        try:
            answer = json.loads(body.decode("utf-8"))
            code = answer.get("code", "")
            detail = answer.get("detail", "")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        reason = f"{method} {path} answered {status}"
        if code:
            reason += f" {code}"
        if detail:
            reason += f": {detail}"
        raise StoreError(reason)

    def list(self, collection: str) -> dict:
        """Return every document of a collection, in key order."""
        documents = {}
        after = ""
        while True:
            query = f"?limit={LIST_PAGE}"
            if after:
                query += f"&after={urllib.parse.quote(after, safe='')}"
            status, body = self._request("GET", collection, query=query)
            path = f"/api/m/{MODULE}/docs/{collection}"
            answer = json.loads(self._answer("GET", path, status, body, (200,)))
            for document in answer["docs"]:
                documents[document["key"]] = document["value"]
            after = answer.get("next_after") or ""
            if not after:
                return documents

    def put(self, collection: str, key: str, value: bytes) -> None:
        """Store one document."""
        status, body = self._request("PUT", collection, key, body=value)
        path = f"/api/m/{MODULE}/docs/{collection}/{key}"
        self._answer("PUT", path, status, body, (204,))

    def delete(self, collection: str, key: str) -> None:
        """Remove one document."""
        status, body = self._request("DELETE", collection, key)
        path = f"/api/m/{MODULE}/docs/{collection}/{key}"
        self._answer("DELETE", path, status, body, (204,))


def _old_players(meta: dict) -> list[int]:
    """Return the player ids the published `meta/players` names."""
    players = meta.get("players")
    if not isinstance(players, dict):
        return []
    entries = players.get("players")
    if not isinstance(entries, list):
        return []
    return [
        entry["player_id"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("player_id"), int)
    ]


def _heroes_value(meta: dict) -> bytes | None:
    """Return the store's `meta/heroes` value, or None when it holds none."""
    if "heroes" not in meta:
        return None
    return encode(meta["heroes"])


def _publish(store: Store, documents: dict, info: dict, meta: dict) -> int:
    """Diff the documents against the store, write `meta/summary` last."""
    expected_collections = {collection for collection, _ in documents}
    for player_id in _old_players(meta):
        collection = MONTH_COLLECTION.format(player_id=player_id)
        if collection not in expected_collections:
            # A player the store lists but the database no longer holds: its
            # whole collection is deleted below.
            expected_collections.add(collection)
    stored = {collection: store.list(collection) for collection in expected_collections}

    puts = deletes = unchanged = 0
    for collection in sorted(expected_collections):
        expected = {
            key: json.loads(value)
            for (document_collection, key), value in documents.items()
            if document_collection == collection and key != "summary"
        }
        have = stored[collection]
        # A delete goes first, so a growing index frees its quota before it
        # writes. Only a player collection's stale keys are deleted: `meta`
        # holds documents this step does not build (`meta/heroes` when the
        # OpenDota fetch failed), and deleting one would drop it.
        if collection != "meta":
            for key in sorted(set(have) - set(expected)):
                if (collection, key) in info["kept"]:
                    continue
                store.delete(collection, key)
                deletes += 1
        for key in sorted(expected):
            if key in have and have[key] == expected[key]:
                unchanged += 1
                continue
            store.put(collection, key, documents[(collection, key)])
            puts += 1

    # meta/summary is written last, after every month it counts.
    summary = documents[("meta", "summary")]
    have = stored.get("meta", {}).get("summary")
    if have == json.loads(summary):
        unchanged += 1
    else:
        store.put("meta", "summary", summary)
        puts += 1

    if info["oversized"]:
        for player_id, month in info["oversized"]:
            logger.error(
                f"Skipped {_month_name(month)} of player {player_id}: the document "
                f"is over the {MAX_VALUE} byte value limit"
            )
    if info["invalid_names"]:
        names = len(info["invalid_names"])
        logger.warning(
            f"Published null for {names} recorded replay file "
            f"{'name' if names == 1 else 'names'} outside the bulk file-name pattern"
        )
    logger.info(
        f"Published {info['indexed_matches']} of {info['total_matches']} matches "
        f"for {info['players']} players in {info['month_documents']} month "
        f"documents: {puts} put, {deletes} deleted, {unchanged} unchanged"
    )
    return 1 if info["oversized"] else 0


def _differences(where: str, stored, expected) -> list[str]:
    """Return how a stored document's value differs from the database's index."""
    if stored == expected:
        return []
    if isinstance(expected, dict) and isinstance(stored, dict):
        lines = []
        for key in sorted(set(stored) | set(expected), key=str):
            if key not in stored:
                lines.append(f"{where}.{key} is missing from the store")
            elif key not in expected:
                lines.append(f"{where}.{key} is not in the database's index")
            else:
                lines.extend(_differences(f"{where}.{key}", stored[key], expected[key]))
        return lines
    if isinstance(expected, list) and isinstance(stored, list):
        entries = stored + expected
        if entries and all(
            isinstance(entry, dict) and "match_id" in entry for entry in entries
        ):
            stored_by_id = {entry["match_id"]: entry for entry in stored}
            expected_by_id = {entry["match_id"]: entry for entry in expected}
            lines = []
            for match_id in sorted(set(stored_by_id) | set(expected_by_id)):
                if match_id not in stored_by_id:
                    lines.append(f"{where} match {match_id} is missing from the store")
                elif match_id not in expected_by_id:
                    lines.append(f"{where} match {match_id} is not in the database's index")
                else:
                    lines.extend(
                        _differences(
                            f"{where} match {match_id}",
                            stored_by_id[match_id],
                            expected_by_id[match_id],
                        )
                    )
            return lines
    return [f"{where} is {stored!r}, the database's index holds {expected!r}"]


def _check(store: Store, documents: dict, info: dict, meta: dict) -> int:
    """Compare the store's index with the database and report every difference."""
    collections = {collection for collection, _ in documents}
    for player_id in _old_players(meta):
        collections.add(MONTH_COLLECTION.format(player_id=player_id))
    problems: list[str] = []
    for collection in sorted(collections):
        stored = store.list(collection)
        expected = {
            key: json.loads(value)
            for (document_collection, key), value in documents.items()
            if document_collection == collection
        }
        for key in sorted(set(stored) | set(expected)):
            if key not in stored:
                problems.append(f"{collection}/{key} is missing from the store")
            elif key not in expected:
                problems.append(f"{collection}/{key} is not in the database's index")
            else:
                problems.extend(
                    _differences(f"{collection}/{key}", stored[key], expected[key])
                )
    for line in problems[:REPORT_LIMIT]:
        logger.error(line)
    if len(problems) > REPORT_LIMIT:
        logger.error(f"... and {len(problems) - REPORT_LIMIT} more differences")
    if problems:
        logger.error(
            f"The index does not match the database: {len(problems)} differences"
        )
        return 1
    logger.info(
        f"The index matches the database: {info['month_documents']} month "
        f"documents, {info['indexed_matches']} matches for {info['players']} players"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Publish the index, or with --check compare it with the database."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(argument != "--check" for argument in arguments):
        logger.error("usage: publish.py [--check]")
        return 2
    check_only = "--check" in arguments

    hub = os.environ.get("SWITCHBOARD_HUB", "")
    token = os.environ.get("SWITCHBOARD_MODULE_TOKEN", "")
    bulk = _directory("SWITCHBOARD_BULK")
    if not hub or not token or bulk is None:
        for name, value in (
            ("SWITCHBOARD_HUB", hub),
            ("SWITCHBOARD_MODULE_TOKEN", token),
        ):
            if not value:
                logger.error(f"{name} is not set")
        return 1
    database = bulk / DATABASE_NAME
    if not database.is_file():
        logger.error(f"No database at {database}")
        return 1

    store = Store(hub, token)
    connection = _connect(database)
    try:
        meta = store.list("meta")
        if check_only:
            heroes_value = _heroes_value(meta)
            published_ms = 0
            summary = meta.get("summary")
            if isinstance(summary, dict) and isinstance(summary.get("published_ms"), int):
                published_ms = summary["published_ms"]
        else:
            try:
                heroes = OpenDotaClient(
                    base_url=os.environ.get("OPENDOTA_BASE_URL")
                ).get_heroes()
            except (OSError, ValueError) as error:
                logger.warning(
                    f"Could not read OpenDota's hero constants ({error}); "
                    "meta/heroes keeps the store's value"
                )
                heroes = None
            else:
                if heroes is None:
                    logger.warning(
                        "Could not read OpenDota's hero constants; meta/heroes "
                        "keeps the store's value"
                    )
            heroes_value = (
                encode({"heroes": {str(hero_id): name
                                    for hero_id, name in heroes.items()}})
                if heroes
                else _heroes_value(meta)
            )
            published_ms = int(time.time() * 1000)
        documents, info = build_index(connection, published_ms, heroes_value)
    except (sqlite3.Error, StoreError, ValueError) as error:
        logger.error(f"The index was not published: {error}")
        return 1
    finally:
        connection.close()

    try:
        if check_only:
            return _check(store, documents, info, meta)
        return _publish(store, documents, info, meta)
    except (StoreError, ValueError) as error:
        logger.error(f"The index was not published: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
