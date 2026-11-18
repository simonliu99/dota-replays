"""Cases for the database's queries against a real SQLite file."""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from database import Database

PLAYER_ID = 1
OTHER_PLAYER = 2
SHARED_MATCH = 777
DAY = 86400
HOUR = 3600

# The schema dota_replays.py wrote before it kept a schema_version table,
# written out here so the case does not depend on the code it tests.
V1_SCHEMA = """
    CREATE TABLE players (
        player_id INTEGER PRIMARY KEY,
        persona_name TEXT,
        last_updated TIMESTAMP
    );
    CREATE TABLE matches (
        match_id INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL,
        start_time INTEGER NOT NULL,
        duration INTEGER,
        hero_id INTEGER,
        kills INTEGER,
        deaths INTEGER,
        assists INTEGER,
        radiant_win BOOLEAN,
        player_slot INTEGER,
        FOREIGN KEY (player_id) REFERENCES players(player_id)
    );
    CREATE TABLE match_details (
        match_id INTEGER PRIMARY KEY,
        data JSON NOT NULL,
        replay_url TEXT,
        is_parsed BOOLEAN DEFAULT FALSE,
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (match_id) REFERENCES matches(match_id)
    );
    CREATE TABLE downloads (
        match_id INTEGER PRIMARY KEY,
        filename TEXT NOT NULL,
        file_size INTEGER,
        on_disk BOOLEAN DEFAULT TRUE,
        downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        verified_at TIMESTAMP,
        FOREIGN KEY (match_id) REFERENCES matches(match_id)
    );
    CREATE TABLE parse_jobs (
        match_id INTEGER PRIMARY KEY,
        job_id INTEGER,
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY (match_id) REFERENCES matches(match_id)
    );
    CREATE INDEX idx_matches_player_id ON matches(player_id);
    CREATE INDEX idx_matches_start_time ON matches(start_time);
    CREATE INDEX idx_downloads_on_disk ON downloads(on_disk);
"""
MATCH_COLUMNS = (
    "match_id, player_id, start_time, duration, hero_id, kills, deaths, "
    "assists, radiant_win, player_slot"
)


def match_row(match_id: int, start_time: int) -> dict:
    """Return a match list entry shaped as OpenDota's."""
    return {
        "match_id": match_id,
        "start_time": start_time,
        "duration": 1200,
        "hero_id": 1,
        "kills": 0,
        "deaths": 0,
        "assists": 0,
        "radiant_win": True,
        "player_slot": 0,
    }


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "dota_replays.db"

    def database(self) -> Database:
        db = Database(str(self.db_path))
        self.addCleanup(db.close)
        return db

    def set_time_zone(self, name: str) -> None:
        """Run the rest of the case in a time zone, restoring the old one after."""
        previous = os.environ.get("TZ")

        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = name
        time.tzset()

    def seed_unparsed(self, db: Database, ages: dict[int, int]) -> None:
        """Store one unparsed match per id, started ages[id] seconds ago."""
        now = int(time.time())
        db.upsert_player(PLAYER_ID, "Test Player")
        db.insert_matches(
            PLAYER_ID,
            [match_row(match_id, now - age) for match_id, age in ages.items()],
        )
        for match_id in ages:
            db.upsert_match_details(match_id, {"match_id": match_id})

    def write_v1_file(self, rows: list[tuple]) -> None:
        """Write a database as the code before schema versions left it."""
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(V1_SCHEMA)
            connection.execute(
                "INSERT INTO players (player_id, persona_name) VALUES (?, ?)",
                (PLAYER_ID, "Test Player"),
            )
            connection.executemany(
                f"INSERT INTO matches ({MATCH_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        finally:
            connection.close()

    def read_rows(self, db: Database) -> list[tuple]:
        return [
            tuple(row)
            for row in db.conn.execute(
                f"SELECT {MATCH_COLUMNS} FROM matches ORDER BY match_id, player_id"
            )
        ]

    def test_a_v1_file_migrates_to_the_current_schema_and_keeps_its_rows(self):
        rows = [
            (10, PLAYER_ID, 1_600_000_000, 1800, 5, 1, 2, 3, 1, 0),
            (11, PLAYER_ID, 1_600_000_100, 2000, 6, 4, 5, 6, 0, 128),
        ]
        self.write_v1_file(rows)

        db = self.database()

        versions = [row[0] for row in db.conn.execute("SELECT version FROM schema_version ORDER BY version")]
        self.assertEqual(versions, [1, 2, 3])
        tables = {
            row[0]
            for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertIn("download_attempts", tables)
        self.assertNotIn("matches_v3", tables)
        self.assertEqual(self.read_rows(db), rows)
        indexes = {
            row[0]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'matches'"
            )
        }
        self.assertLessEqual({"idx_matches_player_id", "idx_matches_start_time"}, indexes)

    def test_a_v2_file_migrates_once_and_keeps_its_rows(self):
        rows = [(12, PLAYER_ID, 1_600_000_000, 1800, 5, 1, 2, 3, 1, 0)]
        self.write_v1_file(rows)
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript("""
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
                INSERT INTO schema_version (version) VALUES (1), (2);
                CREATE TABLE download_attempts (
                    match_id INTEGER PRIMARY KEY,
                    attempt_count INTEGER DEFAULT 0,
                    last_attempt_at TIMESTAMP,
                    last_error TEXT,
                    FOREIGN KEY (match_id) REFERENCES matches(match_id)
                );
                INSERT INTO download_attempts (match_id, attempt_count) VALUES (12, 1);
            """)
        finally:
            connection.close()

        db = self.database()
        again = Database(str(self.db_path))
        again.close()

        versions = [row[0] for row in db.conn.execute("SELECT version FROM schema_version ORDER BY version")]
        self.assertEqual(versions, [1, 2, 3])
        self.assertEqual(self.read_rows(db), rows)
        self.assertEqual(db.get_download_attempt(12)["attempt_count"], 1)

    def test_a_match_two_players_share_is_stored_for_each(self):
        db = self.database()
        db.upsert_player(PLAYER_ID, "Test Player")
        db.upsert_player(OTHER_PLAYER, "Other Player")
        now = int(time.time())
        radiant = match_row(SHARED_MATCH, now)
        dire = dict(radiant, hero_id=2, kills=9, player_slot=128)

        counts = [
            db.insert_matches(PLAYER_ID, [radiant]),
            db.insert_matches(OTHER_PLAYER, [dire]),
            db.insert_matches(PLAYER_ID, [radiant]),
            db.insert_matches(OTHER_PLAYER, [dire]),
        ]

        self.assertEqual(counts, [1, 1, 0, 0])
        self.assertEqual(
            [
                tuple(row)
                for row in db.conn.execute(
                    "SELECT match_id, player_id, hero_id, player_slot FROM matches "
                    "ORDER BY player_id"
                )
            ],
            [(SHARED_MATCH, PLAYER_ID, 1, 0), (SHARED_MATCH, OTHER_PLAYER, 2, 128)],
        )
        self.assertEqual(db.get_match_ids_for_player(OTHER_PLAYER), {SHARED_MATCH})

        db.upsert_match_details(
            SHARED_MATCH,
            {"match_id": SHARED_MATCH, "replay_url": "http://replay.invalid/570/777_1.dem.bz2"},
        )
        self.assertEqual(
            [row["match_id"] for row in db.get_matches_with_replay_url()], [SHARED_MATCH]
        )
        self.assertEqual(
            [row["match_id"] for row in db.get_unparsed_matches()], [SHARED_MATCH]
        )
        stats = db.get_stats()
        self.assertEqual(stats["matches_total"], 1)
        self.assertEqual(stats["matches_without_details"], 0)

    def test_new_details_replace_the_stored_replay_url(self):
        db = self.database()
        db.upsert_player(PLAYER_ID, "Test Player")
        db.insert_matches(PLAYER_ID, [match_row(7, int(time.time()))])
        db.upsert_match_details(7, {"match_id": 7, "version": 21})
        db.upsert_match_details(
            7, {"match_id": 7, "version": 21, "replay_url": "http://replay.invalid/570/7_1.dem.bz2"}
        )

        self.assertEqual(
            [row["replay_url"] for row in db.get_matches_with_replay_url()],
            ["http://replay.invalid/570/7_1.dem.bz2"],
        )

    def test_the_recheck_keeps_matches_of_the_last_21_days(self):
        db = self.database()
        self.seed_unparsed(db, {20: 20 * DAY, 22: 22 * DAY})

        matches = db.get_unparsed_matches(max_age_days=21)

        self.assertEqual([match["match_id"] for match in matches], [20])

    def test_the_recheck_cutoff_is_the_same_west_of_utc(self):
        self.set_time_zone("America/New_York")
        db = self.database()
        self.seed_unparsed(
            db,
            {
                1: 21 * DAY - HOUR,
                2: 21 * DAY + HOUR,
                3: 21 * DAY + 4 * HOUR,
            },
        )

        matches = db.get_unparsed_matches(max_age_days=21)

        self.assertEqual([match["match_id"] for match in matches], [1])


if __name__ == "__main__":
    unittest.main()
