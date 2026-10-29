"""Cases for the publisher, its check mode and the fake hub's store."""

import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import publish
from database import Database
from tests.fake_hub import FakeHub
from tests.support import RecordedOpenDota, _LocalServer

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
TOKEN = "sbm_test_token"
# int(publish.time.time() * 1000) while a case runs: 2025-10-29T05:40:00Z.
PUBLISHED_MS = 1761716400000
PLAYER = 86745912
OTHER_PLAYER = 111620041
FEBRUARY = "y:797597"   # 2024-02
JANUARY = "y:797598"    # 2024-01


def stamp(year: int, month: int, day: int, hour: int = 18) -> int:
    """Return a UTC timestamp for a fixture match's start time."""
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


class _SilentServer(_LocalServer):
    """A server that accepts a request and never answers it."""

    def handle_request(self, handler, method):
        time.sleep(2)


def seed_database(path, players, matches, details=(), downloads=(), off_disk=()):
    """Write a fixture database and close it."""
    database = Database(str(path))
    try:
        for player_id, persona_name in players:
            database.upsert_player(player_id, persona_name)
        by_player: dict[int, list[dict]] = {}
        for match in matches:
            row = dict(match)
            by_player.setdefault(row.pop("player_id"), []).append(row)
        for player_id, rows in by_player.items():
            database.insert_matches(player_id, rows)
        for match_id, data in details:
            database.upsert_match_details(match_id, data)
        if downloads:
            database.conn.executemany(
                "INSERT INTO downloads (match_id, filename, on_disk) VALUES (?, ?, 1)",
                list(downloads),
            )
            database.conn.commit()
        for match_id in off_disk:
            database.update_on_disk_status(match_id, False)
    finally:
        database.close()


def golden_seed():
    """Return the fixed fixture the golden documents are built from."""
    players = [(PLAYER, "Arteezy"), (OTHER_PLAYER, "SumaiL")]
    matches = [
        {
            "player_id": PLAYER,
            "match_id": 8634561781,
            "start_time": stamp(2024, 2, 24),
            "duration": 2410,
            "hero_id": 1,
            "kills": 12,
            "deaths": 3,
            "assists": 9,
            "radiant_win": True,
            "player_slot": 0,
        },
        {
            "player_id": PLAYER,
            "match_id": 8634561782,
            "start_time": stamp(2024, 2, 20),
            "duration": 3320,
            "hero_id": 2,
            "kills": 4,
            "deaths": 8,
            "assists": 14,
            "radiant_win": False,
            "player_slot": 128,
        },
        {
            "player_id": PLAYER,
            "match_id": 8634561783,
            "start_time": stamp(2024, 2, 16),
            "duration": 1875,
            "hero_id": 11,
            "kills": 20,
            "deaths": 1,
            "assists": 5,
            "radiant_win": True,
            "player_slot": 128,
        },
        {
            "player_id": PLAYER,
            "match_id": 8634561784,
            "start_time": stamp(2024, 2, 12),
            "duration": 4500,
            "hero_id": 25,
            "kills": 2,
            "deaths": 11,
            "assists": 6,
            "radiant_win": False,
            "player_slot": 0,
        },
        {
            "player_id": PLAYER,
            "match_id": 8634561785,
            "start_time": stamp(2024, 2, 8),
            "duration": 2900,
            "hero_id": 100,
            "kills": 7,
            "deaths": 7,
            "assists": 7,
            "radiant_win": True,
            "player_slot": 1,
        },
        {
            "player_id": PLAYER,
            "match_id": 8634561790,
            "start_time": stamp(2024, 1, 20),
            "duration": 2100,
            "hero_id": 3,
            "kills": 3,
            "deaths": 3,
            "assists": 3,
            "radiant_win": None,
            "player_slot": None,
        },
        {
            "player_id": OTHER_PLAYER,
            "match_id": 8634561791,
            "start_time": stamp(2024, 2, 18),
            "duration": 2600,
            "hero_id": 5,
            "kills": 6,
            "deaths": 2,
            "assists": 18,
            "radiant_win": True,
            "player_slot": 0,
        },
        {
            "player_id": OTHER_PLAYER,
            "match_id": 8634561792,
            "start_time": stamp(2024, 2, 14),
            "duration": 3060,
            "hero_id": 6,
            "kills": 1,
            "deaths": 9,
            "assists": 4,
            "radiant_win": False,
            "player_slot": 128,
        },
        {
            "player_id": OTHER_PLAYER,
            "match_id": 8634561793,
            "start_time": stamp(2024, 1, 15),
            "duration": 1900,
            "hero_id": 7,
            "kills": 9,
            "deaths": 0,
            "assists": 2,
            "radiant_win": True,
            "player_slot": 0,
        },
    ]
    details = [
        (8634561781, {"version": 22, "replay_url": "http://replay.example/a.dem.bz2"}),
        (8634561782, {"version": None}),
        (8634561783, {"version": 7}),
        (8634561784, {"version": None}),
        (8634561785, {"version": None}),
        (8634561790, {"version": None}),
        (8634561791, {"version": 22, "replay_url": "http://replay.example/b.dem.bz2"}),
        (8634561792, {"version": None}),
        (8634561793, {"version": None}),
    ]
    downloads = [
        (8634561781, "8634561781_1891250204.dem.bz2"),
        (8634561782, "8634561782_1891250204.dem.bz2"),
        (8634561783, "8634561783_1891250204.dem.bz2"),
        (8634561784, "8634561784_1891250204.dem.bz2"),
        (8634561785, "8634561785_1891250204.dem.bz2"),
        (8634561791, "8634561791_1891250204.dem.bz2"),
        (8634561792, "../x.dem.bz2"),
        (8634561793, "8634561793_1891250204.dem.bz2"),
    ]
    return players, matches, details, downloads, (8634561793,)


class PublishTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bulk = self.root / "bulk"
        self.bulk.mkdir()
        self.path = self.bulk / "dota_replays.db"
        self.hub = FakeHub(token=TOKEN)
        self.addCleanup(self.hub.close)
        self.opendota = RecordedOpenDota()
        self.addCleanup(self.opendota.close)
        environment = mock.patch.dict(
            os.environ,
            {
                "SWITCHBOARD_BULK": str(self.bulk),
                "SWITCHBOARD_HUB": self.hub.base_url,
                "SWITCHBOARD_MODULE_TOKEN": TOKEN,
                "OPENDOTA_BASE_URL": f"{self.opendota.base_url}/api",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def run_main(self, *arguments, published_ms=PUBLISHED_MS):
        """Run the publisher on a fixed clock and return its code and log."""
        with mock.patch("publish.time.time", return_value=published_ms / 1000):
            with self.assertLogs("publish", level="DEBUG") as captured:
                code = publish.main(list(arguments))
        return code, "\n".join(captured.output)

    def seed_golden(self):
        players, matches, details, downloads, off_disk = golden_seed()
        seed_database(self.path, players, matches, details, downloads, off_disk)

    def seed_one_match_per_month(self, players, months=156):
        """Seed one match in each month from 2013-01, for every player."""
        matches = []
        for index, (player_id, _) in enumerate(players):
            for step in range(months):
                year = 2013 + step // 12
                month = step % 12 + 1
                matches.append(
                    {
                        "player_id": player_id,
                        "match_id": 8_000_000_000 + index * 1_000 + step,
                        "start_time": stamp(year, month, 5),
                        "duration": 2_400,
                        "hero_id": 1 + step % 100,
                        "kills": step % 15,
                        "deaths": step % 7,
                        "assists": step % 11,
                        "radiant_win": step % 2 == 0,
                        "player_slot": 0 if step % 2 == 0 else 128,
                    }
                )
        seed_database(self.path, players, matches)

    def test_the_first_publish_puts_every_month_and_three_meta_documents(self):
        self.seed_one_match_per_month([(1, "One"), (2, "Two"), (3, "Three")])

        code, log = self.run_main()

        self.assertEqual(code, 0, log)
        self.assertEqual(self.hub.writes("PUT"), 3 * 156 + 3)
        self.assertEqual(self.hub.writes("DELETE"), 0)
        self.assertEqual(len(self.hub.documents), 3 * 156 + 3)
        self.assertTrue(
            self.hub.paths("PUT")[-1].endswith("/docs/meta/summary"),
            self.hub.paths("PUT")[-1],
        )
        summary = self.hub.document("meta", "summary")
        self.assertEqual(summary["indexed_matches"], 468)
        self.assertEqual(summary["total_matches"], 468)
        self.assertEqual(summary["players"], 3)
        self.assertEqual(summary["oldest_indexed_month"], "2013-01")

    def test_a_second_publish_with_no_change_puts_only_the_summary(self):
        self.seed_golden()
        self.assertEqual(self.run_main()[0], 0)
        puts, deletes = self.hub.writes("PUT"), self.hub.writes("DELETE")

        code, log = self.run_main(published_ms=PUBLISHED_MS + 60_000)

        self.assertEqual(code, 0, log)
        self.assertEqual(self.hub.writes("PUT") - puts, 1)
        self.assertEqual(self.hub.paths("PUT")[-1].rsplit("/", 2)[-2:], ["meta", "summary"])
        self.assertEqual(self.hub.writes("DELETE"), deletes)

    def test_a_new_match_puts_its_month_and_the_summary(self):
        self.seed_one_match_per_month([(1, "One")], months=2)
        self.assertEqual(self.run_main()[0], 0)
        database = Database(str(self.path))
        try:
            database.insert_matches(
                1,
                [
                    {
                        "match_id": 8_000_009_999,
                        "start_time": stamp(2013, 2, 20),
                        "duration": 2_500,
                        "hero_id": 9,
                        "kills": 5,
                        "deaths": 4,
                        "assists": 8,
                        "radiant_win": True,
                        "player_slot": 0,
                    }
                ],
            )
        finally:
            database.close()
        puts = self.hub.writes("PUT")

        code, log = self.run_main()

        self.assertEqual(code, 0, log)
        self.assertEqual(self.hub.writes("PUT") - puts, 2)
        self.assertEqual(
            self.hub.document("matches.1", "y:798697")["matches"][0]["match_id"],
            8_000_009_999,
        )
        self.assertEqual(self.hub.document("meta", "summary")["indexed_matches"], 3)

    def test_a_month_the_database_no_longer_holds_is_deleted(self):
        self.seed_one_match_per_month([(1, "One")], months=2)
        self.assertEqual(self.run_main()[0], 0)
        database = Database(str(self.path))
        try:
            database.conn.execute("DELETE FROM matches WHERE match_id = ?", (8_000_000_000,))
            database.conn.commit()
        finally:
            database.close()

        code, log = self.run_main()

        self.assertEqual(code, 0, log)
        self.assertEqual(self.hub.writes("DELETE"), 1)
        self.assertIsNone(self.hub.document("matches.1", "y:798698"))
        self.assertEqual(self.hub.document("meta", "summary")["indexed_matches"], 1)

    def test_a_player_the_store_lists_but_the_database_dropped_is_deleted(self):
        self.seed_one_match_per_month([(1, "One"), (2, "Two")], months=2)
        self.assertEqual(self.run_main()[0], 0)
        database = Database(str(self.path))
        try:
            database.conn.execute("DELETE FROM matches WHERE player_id = 2")
            database.conn.execute("DELETE FROM players WHERE player_id = 2")
            database.conn.commit()
        finally:
            database.close()

        code, log = self.run_main()

        self.assertEqual(code, 0, log)
        self.assertEqual(self.hub.collection("matches.2"), {})
        self.assertEqual(self.hub.document("meta", "players")["players"], [
            {"player_id": 1, "persona_name": "One"}
        ])

    def test_the_cap_keeps_whole_months_newest_first(self):
        players = [(1, "Cap Player")]
        matches = []
        per_month = 1_250
        for step in range(60):
            year = 2020 + step // 12
            month = step % 12 + 1
            for number in range(per_month):
                matches.append(
                    {
                        "player_id": 1,
                        "match_id": 8_500_000_000 + step * per_month + number,
                        "start_time": stamp(year, month, 1 + number % 27),
                        "duration": 2_400,
                        "hero_id": 1 + number % 100,
                        "kills": number % 15,
                        "deaths": number % 7,
                        "assists": number % 11,
                        "radiant_win": number % 2 == 0,
                        "player_slot": 0 if number % 2 == 0 else 128,
                    }
                )
        downloads = [
            (match["match_id"], f"{match['match_id']}_1891250204.dem.bz2")
            for match in matches
        ]
        seed_database(self.path, players, matches, downloads=downloads)

        code, log = self.run_main()
        code, log = self.run_main()

        self.assertEqual(code, 0, log)
        self.assertLessEqual(self.hub.size(), publish.MODULE_BUDGET)
        stored = [
            key for (collection, key) in self.hub.documents if collection == "matches.1"
        ]
        self.assertLess(len(stored), 60)
        summary = self.hub.document("meta", "summary")
        oldest = min(999999 - int(key[2:]) for key in stored)
        self.assertEqual(
            summary["oldest_indexed_month"], f"{oldest // 100:04d}-{oldest % 100:02d}"
        )
        self.assertLess(summary["indexed_matches"], summary["total_matches"])
        for (collection, key), value in self.hub.documents.items():
            if collection == "matches.1":
                self.assertLessEqual(len(value), publish.MAX_VALUE)
        self.assertIn("Published ", log)

    def test_a_month_over_the_value_limit_is_skipped_and_the_run_exits_1(self):
        # The measured width of one match in a month document, from the golden
        # fixture's February month, which has a replay file on every match.
        february = (GOLDEN / "matches.86745912" / f"{FEBRUARY}.json").read_bytes()
        per_match = (len(february) - len(b'{"matches":[]}')) / 5
        self.assertGreater(per_match, 0)
        count = int(publish.MAX_VALUE / per_match) + 2
        self.assertGreater(count * per_match, publish.MAX_VALUE)
        matches = [
            {
                "player_id": PLAYER,
                "match_id": 8_600_000_000 + number,
                "start_time": stamp(2024, 2, 1 + number % 28),
                "duration": 2_400,
                "hero_id": 1 + number % 100,
                "kills": 3,
                "deaths": 2,
                "assists": 9,
                "radiant_win": number % 2 == 0,
                "player_slot": 0,
            }
            for number in range(count)
        ]
        matches.append(
            {
                "player_id": PLAYER,
                "match_id": 8_600_000_001 + count,
                "start_time": stamp(2024, 1, 10),
                "duration": 2_000,
                "hero_id": 3,
                "kills": 1,
                "deaths": 1,
                "assists": 1,
                "radiant_win": True,
                "player_slot": 0,
            }
        )
        downloads = [
            (match["match_id"], f"{match['match_id']}_1891250204.dem.bz2")
            for match in matches
        ]
        seed_database(self.path, [(PLAYER, "Arteezy")], matches, downloads=downloads)

        code, log = self.run_main()

        self.assertEqual(code, 1, log)
        self.assertIn(f"Skipped 2024-02 of player {PLAYER}", log)
        self.assertIsNone(self.hub.document(f"matches.{PLAYER}", FEBRUARY))
        self.assertIsNotNone(self.hub.document(f"matches.{PLAYER}", JANUARY))
        summary = self.hub.document("meta", "summary")
        self.assertEqual(summary["indexed_matches"], 1)
        self.assertEqual(summary["total_matches"], count + 1)
        self.assertEqual(summary["oldest_indexed_month"], "2024-01")
        self.assertIn("in 1 month documents", log)

    def test_an_oversized_month_beside_another_players_month_is_skipped(self):
        matches = [
            {
                "player_id": PLAYER,
                "match_id": 8_600_000_000 + number,
                "start_time": stamp(2024, 2, 1 + number % 28),
                "duration": 2_400,
                "hero_id": 100 + number % 30,
                "kills": 10 + number % 20,
                "deaths": 10 + number % 20,
                "assists": 10 + number % 20,
                "radiant_win": number % 2 == 0,
                "player_slot": 0,
            }
            for number in range(2_000)
        ]
        matches.append(
            {
                "player_id": PLAYER,
                "match_id": 8_600_003_000,
                "start_time": stamp(2024, 1, 10),
                "duration": 2_000,
                "hero_id": 3,
                "kills": 1,
                "deaths": 1,
                "assists": 1,
                "radiant_win": True,
                "player_slot": 0,
            }
        )
        matches.append(
            {
                "player_id": OTHER_PLAYER,
                "match_id": 8_600_002_000,
                "start_time": stamp(2024, 2, 10),
                "duration": 2_000,
                "hero_id": 3,
                "kills": 1,
                "deaths": 1,
                "assists": 1,
                "radiant_win": True,
                "player_slot": 0,
            }
        )
        downloads = [
            (match["match_id"], f"{match['match_id']}_1891250204.dem.bz2")
            for match in matches
        ]
        seed_database(
            self.path,
            [(PLAYER, "Arteezy"), (OTHER_PLAYER, "SumaiL")],
            matches,
            downloads=downloads,
        )

        code, log = self.run_main()

        self.assertEqual(code, 1, log)
        self.assertIn(f"Skipped 2024-02 of player {PLAYER}", log)
        self.assertIsNone(self.hub.document(f"matches.{PLAYER}", FEBRUARY))
        self.assertIsNotNone(self.hub.document(f"matches.{OTHER_PLAYER}", FEBRUARY))
        self.assertIsNotNone(self.hub.document(f"matches.{PLAYER}", JANUARY))
        summary = self.hub.document("meta", "summary")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["indexed_matches"], 2)

    def test_a_forced_413_exits_1_and_writes_no_summary(self):
        self.seed_golden()
        self.hub.fail("PUT", f"matches.{PLAYER}", FEBRUARY, 413)

        code, log = self.run_main()

        self.assertEqual(code, 1, log)
        self.assertIn("413", log)
        self.assertIn(f"/docs/matches.{PLAYER}/{FEBRUARY}", log)
        self.assertIsNone(self.hub.document("meta", "summary"))

    def test_a_401_exits_1_and_prints_no_token(self):
        self.seed_golden()
        wrong = "sbm_wrong_token"
        os.environ["SWITCHBOARD_MODULE_TOKEN"] = wrong

        code, log = self.run_main()

        self.assertEqual(code, 1, log)
        self.assertIn("401", log)
        self.assertNotIn(wrong, log)
        self.assertNotIn(TOKEN, log)
        self.assertEqual(self.hub.documents, {})

    def test_a_missing_variable_exits_1(self):
        self.seed_golden()
        del os.environ["SWITCHBOARD_HUB"]

        code, log = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("SWITCHBOARD_HUB is not set", log)

    def test_a_hero_fetch_timeout_keeps_the_stored_heroes(self):
        self.seed_golden()
        self.assertEqual(self.run_main()[0], 0)
        stored = self.hub.document("meta", "heroes")

        with mock.patch("publish.OpenDotaClient") as client:
            client.return_value.get_heroes.side_effect = TimeoutError("timed out")
            code, log = self.run_main(published_ms=PUBLISHED_MS + 60_000)

        self.assertEqual(code, 0, log)
        self.assertIn("Could not read OpenDota's hero constants (timed out)", log)
        self.assertEqual(self.hub.document("meta", "heroes"), stored)

    def test_a_replay_file_is_the_recorded_name_or_null(self):
        self.seed_golden()

        code, log = self.run_main()

        self.assertEqual(code, 0, log)
        february = {
            match["match_id"]: match
            for match in self.hub.document(f"matches.{OTHER_PLAYER}", FEBRUARY)["matches"]
        }
        self.assertEqual(
            february[8634561791]["replay_file"], "8634561791_1891250204.dem.bz2"
        )
        self.assertIsNone(february[8634561792]["replay_file"])
        self.assertIn("Published null for 1 recorded replay file name", log)
        january = self.hub.document(f"matches.{OTHER_PLAYER}", JANUARY)["matches"]
        self.assertIsNone(january[0]["replay_file"])
        self.assertIsNone(
            self.hub.document(f"matches.{PLAYER}", JANUARY)["matches"][0]["replay_file"]
        )

    def test_the_golden_documents_match_byte_for_byte(self):
        self.seed_golden()

        code, log = self.run_main()

        self.assertEqual(code, 0, log)
        golden = {
            (path.parent.name, path.stem): path.read_bytes()
            for path in GOLDEN.glob("*/*.json")
        }
        self.assertTrue(golden, "tests/golden holds no documents")
        self.assertEqual(set(golden), set(self.hub.documents))
        for slot, value in golden.items():
            self.assertEqual(self.hub.documents[slot], value, f"{slot[0]}/{slot[1]}")

    def test_an_unchanged_index_checks_and_a_deletion_fails(self):
        self.seed_golden()
        self.assertEqual(self.run_main()[0], 0)

        code, log = self.run_main("--check")

        self.assertEqual(code, 0, log)
        self.assertIn("The index matches the database", log)

        self.hub.delete_document(f"matches.{PLAYER}", FEBRUARY)

        code, log = self.run_main("--check")

        self.assertEqual(code, 1, log)
        self.assertIn(f"matches.{PLAYER}/{FEBRUARY} is missing from the store", log)

    def test_check_compares_each_matched_replay_file(self):
        self.seed_golden()
        self.assertEqual(self.run_main()[0], 0)
        document = self.hub.document(f"matches.{PLAYER}", FEBRUARY)
        document["matches"][0]["replay_file"] = "8634561781_9999999999.dem.bz2"
        self.hub.replace_document(f"matches.{PLAYER}", FEBRUARY, document)

        code, log = self.run_main("--check")

        self.assertEqual(code, 1, log)
        self.assertIn("replay_file", log)
        self.assertIn("8634561781", log)
        self.assertIn("8634561781_9999999999.dem.bz2", log)


class StoreTimeoutTestCase(unittest.TestCase):
    def test_a_store_read_timeout_is_a_store_error(self):
        silent = _SilentServer()
        self.addCleanup(silent.close)
        store = publish.Store(silent.base_url, TOKEN, timeout=0.1)

        with self.assertRaises(publish.StoreError) as caught:
            store.list("meta")

        self.assertIn("timed out", str(caught.exception))


class FakeHubRulesTestCase(unittest.TestCase):
    """The fake applies the store's rules, not only the publisher's reading."""

    def setUp(self):
        self.hub = FakeHub(token=TOKEN)
        self.addCleanup(self.hub.close)
        self.store = publish.Store(self.hub.base_url, TOKEN)

    def raw(self, method, path, body=None, token=TOKEN):
        request = urllib.request.Request(
            self.hub.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_a_value_over_the_limit_answers_413_and_changes_nothing(self):
        value = json.dumps(
            {"x": "a" * (publish.MAX_VALUE - 7)}, separators=(",", ":")
        ).encode()
        self.assertEqual(len(value), publish.MAX_VALUE + 1)

        status, body = self.raw("PUT", "/api/m/dota-replays/docs/c/k", value)

        self.assertEqual(status, 413)
        self.assertIn(b"too-large", body)
        self.assertEqual(self.hub.documents, {})

    def test_a_name_outside_the_pattern_answers_400(self):
        for name in ("bad name", "a/b", "a+b", "x" * 129):
            with self.subTest(name=name[:10]):
                path = f"/api/m/dota-replays/docs/{urllib.parse.quote(name, safe='')}"
                status, body = self.raw("GET", path)
                self.assertEqual(status, 400)
                self.assertIn(b"invalid", body)

    def test_a_percent_encoded_key_answers_400(self):
        status, body = self.raw("PUT", "/api/m/dota-replays/docs/c/y%3A1", b"{}")

        self.assertEqual(status, 400)
        self.assertIn(b"invalid", body)
        self.assertEqual(self.hub.documents, {})

        status, body = self.raw("PUT", "/api/m/dota-replays/docs/c/y:1", b"{}")

        self.assertEqual(status, 204)
        self.assertIn(("c", "y:1"), self.hub.documents)

    def test_a_list_page_over_200_answers_400(self):
        status, body = self.raw("GET", "/api/m/dota-replays/docs/meta?limit=201")

        self.assertEqual(status, 400)
        self.assertIn(b"invalid", body)

    def test_the_module_quota_counts_collection_key_and_value(self):
        # 63 values of 256 KiB fill 16,515,387 of the module's 16,777,216
        # bytes; the 64th would pass it.
        value = json.dumps(
            {"x": "a" * (publish.MAX_VALUE - 8)}, separators=(",", ":")
        ).encode()
        for number in range(63):
            self.store.put("c", f"k{number:03d}", value)
        self.assertLessEqual(self.hub.size(), 16 * 1024 * 1024)

        with self.assertRaises(publish.StoreError) as caught:
            self.store.put("c", "k063", value)

        self.assertIn("413", str(caught.exception))
        self.assertEqual(len(self.hub.documents), 63)


if __name__ == "__main__":
    unittest.main()
