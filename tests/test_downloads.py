"""Cases for the replay download paths, against a local replay server."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from database import Database
from dota_replays import DotaReplays
from opendota_client import OpenDotaClient
from tests.support import ReplayServer, WorkingDirectory, closed_port

PLAYER_ID = 1
MATCH_ID = 8554321
BZIP2_MAGIC = b"BZh"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
BODY = BZIP2_MAGIC + bytes(value % 256 for value in range(200_000))


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
            client=OpenDotaClient(base_url=f"http://127.0.0.1:{closed_port()}/api"),
            replay_dir=self.replay_dir,
            wait_for_parse=False,
        )

    def target(self) -> Path:
        return self.replay_dir / f"{MATCH_ID}.dem.bz2"

    def zst_target(self) -> Path:
        return self.replay_dir / f"{MATCH_ID}.dem.zst"

    def part(self) -> Path:
        target = self.target()
        return target.with_name(f"{target.name}.part")

    def match_row(self, match_id: int, age_days: int = 0) -> dict:
        return {
            "match_id": match_id,
            "start_time": int(time.time()) - age_days * 86400,
            "duration": 1200,
            "hero_id": 1,
            "kills": 0,
            "deaths": 0,
            "assists": 0,
            "radiant_win": True,
            "player_slot": 0,
        }

    def seed(self, replay_url: str, age_days: int = 0) -> None:
        self.db.upsert_player(PLAYER_ID, "Test Player")
        self.db.insert_matches(PLAYER_ID, [self.match_row(MATCH_ID, age_days)])
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
        self.assertIsNone(self.db.get_download_attempt(MATCH_ID))

    def test_a_match_older_than_21_days_is_not_requested(self):
        server = ReplayServer(BODY)
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2", age_days=22)
        app = self.app()

        app._download_replays()

        self.assertEqual(server.requests, [])
        self.assertEqual(self.db.get_downloads(), [])
        self.assertFalse(self.target().exists())

    def test_two_failed_attempts_stop_the_download_until_a_forced_retry(self):
        server = ReplayServer(BODY, status=503)
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        app = self.app()

        app._download_replays()
        app._download_replays()
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(self.db.get_download_attempt(MATCH_ID)["attempt_count"], 2)

        server.status = 200
        app._download_replays()
        self.assertEqual(len(server.requests), 2)
        self.assertFalse(self.target().exists())

        app._download_replays(force_retry=True)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(self.target().read_bytes(), BODY)

    def test_an_odd_replay_url_records_no_download(self):
        for url_end in ("570/", "570/.."):
            with self.subTest(url_end=url_end):
                server = ReplayServer(BODY)
                self.addCleanup(server.close)
                self.seed(f"{server.base_url}/{url_end}")
                app = self.app()

                app._download_replays()

                self.assertEqual(self.db.get_downloads(), [])
                self.assertEqual(server.requests, [])
                attempt = self.db.get_download_attempt(MATCH_ID)
                self.assertIn("unexpected replay URL name", attempt["last_error"])
                self.db.clear_download_attempts(MATCH_ID)

    def test_verify_marks_a_missing_or_wrong_sized_replay_off_disk(self):
        server = ReplayServer(BODY)
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        other = MATCH_ID + 1
        self.db.insert_matches(PLAYER_ID, [self.match_row(other)])
        app = self.app()
        self.target().write_bytes(BODY)
        for match_id in (MATCH_ID, other):
            self.db.record_download(match_id, f"{match_id}.dem.bz2", len(BODY))

        app.verify_downloads()

        self.assertEqual(
            {row["match_id"]: row["on_disk"] for row in self.db.get_downloads()},
            {MATCH_ID: 1, other: 0},
        )

        for label, body in (("truncated", BODY[:1000]), ("empty", b"")):
            with self.subTest(label=label):
                self.target().write_bytes(body)

                app.verify_downloads()

                self.assertEqual(
                    {row["match_id"]: row["on_disk"] for row in self.db.get_downloads()},
                    {MATCH_ID: 0, other: 0},
                )
        self.assertEqual(server.requests, [])

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

    def test_a_zstd_body_at_a_bz2_url_is_stored_as_dem_zst(self):
        body = ZSTD_MAGIC + b"a zstd body"
        server = ReplayServer(body)
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        app = self.app()

        app._download_replays()

        self.assertTrue(self.zst_target().is_file())
        self.assertEqual(self.zst_target().read_bytes(), body)
        self.assertFalse(self.target().exists())
        self.assertFalse(self.part().exists())
        self.assertEqual(
            [row["filename"] for row in self.db.get_downloads()],
            [f"{MATCH_ID}.dem.zst"],
        )
        self.assertEqual(len(server.requests), 1)

    def test_an_unknown_body_leaves_no_replay_and_records_an_attempt(self):
        server = ReplayServer(b"<html>not a replay</html>")
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        app = self.app()

        app._download_replays()

        self.assertEqual(os.listdir(self.replay_dir), [])
        self.assertEqual(self.db.get_downloads(), [])
        attempt = self.db.get_download_attempt(MATCH_ID)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt["attempt_count"], 1)
        self.assertIn("unknown replay format", attempt["last_error"])

    def test_a_content_encoded_response_is_refused(self):
        server = ReplayServer(BODY, content_encoding="zstd")
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        app = self.app()

        app._download_replays()

        self.assertEqual(os.listdir(self.replay_dir), [])
        self.assertEqual(self.db.get_downloads(), [])
        attempt = self.db.get_download_attempt(MATCH_ID)
        self.assertIsNotNone(attempt)
        self.assertIn("unexpected Content-Encoding", attempt["last_error"])

    def test_redownload_stores_a_zstd_body_as_dem_zst(self):
        body = ZSTD_MAGIC + b"a zstd body"
        server = ReplayServer(body)
        self.addCleanup(server.close)
        filename = f"{MATCH_ID}.dem.bz2"
        self.seed(f"{server.base_url}/570/{filename}")
        self.db.record_download(MATCH_ID, filename, len(body))
        app = self.app()

        app.verify_downloads(redownload=True)

        self.assertTrue(self.zst_target().is_file())
        self.assertEqual(self.zst_target().read_bytes(), body)
        self.assertFalse(self.target().exists())
        self.assertEqual(
            [row["filename"] for row in self.db.get_downloads()],
            [f"{MATCH_ID}.dem.zst"],
        )
        self.assertEqual(len(server.requests), 1)

    def test_a_dem_zst_already_on_disk_is_recorded_without_a_request(self):
        server = ReplayServer(ZSTD_MAGIC + b"a zstd body")
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        app = self.app()
        body = ZSTD_MAGIC + b"already here"
        self.zst_target().write_bytes(body)

        app._download_replays()

        self.assertEqual(server.requests, [])
        self.assertEqual(
            [(row["filename"], row["file_size"]) for row in self.db.get_downloads()],
            [(f"{MATCH_ID}.dem.zst", len(body))],
        )

    def test_a_dem_bz2_already_on_disk_is_recorded_without_a_request(self):
        server = ReplayServer(BODY)
        self.addCleanup(server.close)
        self.seed(f"{server.base_url}/570/{MATCH_ID}.dem.bz2")
        app = self.app()
        body = BZIP2_MAGIC + b"already here"
        self.target().write_bytes(body)

        app._download_replays()

        self.assertEqual(server.requests, [])
        self.assertEqual(self.target().read_bytes(), body)
        self.assertEqual(
            [(row["filename"], row["file_size"]) for row in self.db.get_downloads()],
            [(f"{MATCH_ID}.dem.bz2", len(body))],
        )

    def test_scan_registers_dem_zst_files(self):
        other_match = MATCH_ID + 1
        self.seed(f"http://replay.invalid/570/{MATCH_ID}.dem.bz2")
        self.db.insert_matches(PLAYER_ID, [self.match_row(other_match)])
        self.db.upsert_match_details(
            other_match,
            {
                "match_id": other_match,
                "version": 21,
                "replay_url": "http://replay.invalid/570/abcd_205.dem.bz2",
            },
        )
        app = self.app()
        self.zst_target().write_bytes(ZSTD_MAGIC + b"one")
        (self.replay_dir / "abcd_205.dem.zst").write_bytes(ZSTD_MAGIC + b"two")

        app.scan_replay_directory()

        self.assertEqual(
            {row["match_id"]: row["filename"] for row in self.db.get_downloads()},
            {MATCH_ID: f"{MATCH_ID}.dem.zst", other_match: "abcd_205.dem.zst"},
        )


if __name__ == "__main__":
    unittest.main()
