"""Cases for the replay download paths, against a local replay server."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from database import Database
from dota_replays import DotaReplays
from opendota_client import OpenDotaClient
from tests.support import ReplayServer, WorkingDirectory

PLAYER_ID = 1
MATCH_ID = 8554321
BODY = bytes(value % 256 for value in range(200_000))


class ReplayDownloadTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(str(self.root / "dota_replays.db"))
        self.addCleanup(self.db.close)
        self.replay_dir = self.root / "replays"

    def app(self) -> DotaReplays:
        return DotaReplays(
            db=self.db,
            client=OpenDotaClient(base_url="http://127.0.0.1:9/api"),
            replay_dir=self.replay_dir,
            wait_for_parse=False,
        )

    def target(self) -> Path:
        return self.replay_dir / f"{MATCH_ID}.dem.bz2"

    def part(self) -> Path:
        target = self.target()
        return target.with_name(f"{target.name}.part")

    def seed(self, replay_url: str) -> None:
        self.db.upsert_player(PLAYER_ID, "Test Player")
        self.db.insert_matches(
            PLAYER_ID,
            [
                {
                    "match_id": MATCH_ID,
                    "start_time": int(time.time()),
                    "duration": 1200,
                    "hero_id": 1,
                    "kills": 0,
                    "deaths": 0,
                    "assists": 0,
                    "radiant_win": True,
                    "player_slot": 0,
                }
            ],
        )
        self.db.upsert_match_details(
            MATCH_ID,
            {"match_id": MATCH_ID, "version": 21, "replay_url": replay_url},
        )

    def test_download_streams_to_the_file_and_records_its_size(self):
        server = ReplayServer(BODY)
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        app = self.app()
        run_dir = self.root / "run"
        run_dir.mkdir()

        with WorkingDirectory(run_dir):
            with self.assertLogs("dota_replays", level="INFO") as captured:
                app._download_replays()

        target = self.target()
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), BODY)
        self.assertEqual(target.stat().st_size, len(BODY))
        self.assertFalse(self.part().exists())
        self.assertEqual(os.listdir(run_dir), [])
        downloads = self.db.get_downloads()
        self.assertEqual(
            [(row["match_id"], row["file_size"]) for row in downloads],
            [(MATCH_ID, len(BODY))],
        )
        self.assertEqual(len(server.requests), 1)
        log = "\n".join(captured.output)
        self.assertIn(f"match {MATCH_ID}: {len(BODY)} bytes", log)

    def test_half_a_body_leaves_no_replay_and_the_next_run_finishes(self):
        server = ReplayServer(BODY, truncate_first=True)
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        app = self.app()

        app._download_replays()

        self.assertFalse(self.target().exists())
        self.assertFalse(self.part().exists())
        self.assertEqual(self.db.get_downloads(), [])
        attempt = self.db.get_download_attempt(MATCH_ID)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt["attempt_count"], 1)

        app._download_replays()

        self.assertTrue(self.target().is_file())
        self.assertEqual(self.target().read_bytes(), BODY)
        self.assertEqual(
            [row["file_size"] for row in self.db.get_downloads()], [len(BODY)]
        )
        self.assertEqual(len(server.requests), 2)

    def test_redownload_writes_a_part_file_before_renaming(self):
        server = ReplayServer(BODY, truncate_first=True)
        self.addCleanup(server.close)
        filename = f"{MATCH_ID}.dem.bz2"
        self.seed(f"{server.base_url}/570/{filename}")
        self.db.record_download(MATCH_ID, filename, len(BODY))
        app = self.app()

        app.verify_downloads(redownload=True)

        self.assertFalse(self.target().exists())
        self.assertFalse(self.part().exists())

        app.verify_downloads(redownload=True)

        self.assertTrue(self.target().is_file())
        self.assertEqual(self.target().read_bytes(), BODY)
        self.assertEqual(
            [row["file_size"] for row in self.db.get_downloads()], [len(BODY)]
        )


if __name__ == "__main__":
    unittest.main()
