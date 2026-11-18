"""Cases for the database's queries against a real SQLite file."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from database import Database

PLAYER_ID = 1
DAY = 86400
HOUR = 3600


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
