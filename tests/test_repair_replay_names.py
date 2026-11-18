"""Cases for repair_replay_names.py, the one-off rename of mislabeled replays.

Each case builds a replay folder and a WAL database in a temporary directory
and calls the script's main() in this process, so it can assert on the standard
output and patch the database copy. Nothing is written outside the temporary
directory and no network call is made.
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import repair_replay_names
from database import Database

BZIP2_MAGIC = b"BZh"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class RepairReplayNamesTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "replays"
        self.folder.mkdir()
        self.db_path = self.root / "dota_replays.db"
        db = Database(str(self.db_path))
        try:
            db.upsert_player(1, "Test Player")
        finally:
            db.close()

    def write(self, name: str, body: bytes) -> Path:
        path = self.folder / name
        path.write_bytes(body)
        return path

    def record(self, match_id: int, filename: str, file_size: int) -> None:
        db = Database(str(self.db_path))
        try:
            db.record_download(match_id, filename, file_size)
        finally:
            db.close()

    def filename_for(self, match_id: int) -> str:
        db = Database(str(self.db_path))
        try:
            rows = [row for row in db.get_downloads() if row["match_id"] == match_id]
        finally:
            db.close()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]["filename"]

    def run_main(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = repair_replay_names.main(
                [
                    "--db",
                    str(self.db_path),
                    "--replay-dir",
                    str(self.folder),
                    *args,
                ]
            )
        return code, output.getvalue()

    def seed_mixed_folder(self) -> None:
        self.record(123, "2_2.dem.bz2", 13)
        self.write("1_1.dem.bz2", BZIP2_MAGIC + b"bzip2")
        self.write("2_2.dem.bz2", ZSTD_MAGIC + b"with a row")
        self.write("3_3.dem.bz2", ZSTD_MAGIC + b"no row")
        self.write("4_4.dem.bz2", b"junk")

    def test_a_dry_run_changes_nothing_and_exits_one_while_work_remains(self):
        self.seed_mixed_folder()
        listing_before = sorted(os.listdir(self.folder))
        files_before = {
            name: (self.folder / name).read_bytes() for name in listing_before
        }
        database_before = self.db_path.read_bytes()
        root_before = sorted(os.listdir(self.root))

        code, output = self.run_main()

        self.assertEqual(sorted(os.listdir(self.folder)), listing_before)
        self.assertEqual(
            {name: (self.folder / name).read_bytes() for name in listing_before},
            files_before,
        )
        self.assertEqual(sorted(os.listdir(self.root)), root_before)
        self.assertEqual(self.db_path.read_bytes(), database_before)
        self.assertEqual(code, 1)
        self.assertIn(
            "would rename 2_2.dem.bz2 -> 2_2.dem.zst (row: match 123)", output
        )
        self.assertIn("would rename 3_3.dem.bz2 -> 3_3.dem.zst (no row)", output)
        self.assertIn("left alone: 4_4.dem.bz2 (unknown 6a756e6b)", output)
        self.assertIn("dry run: 2 renames, 0 row updates, 0 conflicts", output)
        self.assertIn("wal file present: False", output)

    def test_apply_renames_the_files_and_updates_the_row(self):
        self.seed_mixed_folder()

        code, output = self.run_main("--apply")

        self.assertEqual(code, 0, output)
        self.assertIn("renamed 2_2.dem.bz2 -> 2_2.dem.zst (row: match 123)", output)
        self.assertIn("renamed 3_3.dem.bz2 -> 3_3.dem.zst (no row)", output)
        self.assertEqual(
            sorted(os.listdir(self.folder)),
            ["1_1.dem.bz2", "2_2.dem.zst", "3_3.dem.zst", "4_4.dem.bz2"],
        )
        expected = {
            "1_1.dem.bz2": BZIP2_MAGIC + b"bzip2",
            "2_2.dem.zst": ZSTD_MAGIC + b"with a row",
            "3_3.dem.zst": ZSTD_MAGIC + b"no row",
            "4_4.dem.bz2": b"junk",
        }
        self.assertEqual(
            {name: (self.folder / name).read_bytes() for name in expected}, expected
        )
        self.assertEqual(self.filename_for(123), "2_2.dem.zst")

        copies = [name for name in os.listdir(self.root) if ".before-repair-" in name]
        self.assertEqual(len(copies), 1, copies)
        copy_path = self.root / copies[0]
        connection = sqlite3.connect(f"file:{copy_path}?mode=ro", uri=True)
        try:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT filename FROM downloads WHERE match_id = 123"
                ).fetchall(),
                [("2_2.dem.bz2",)],
            )
        finally:
            connection.close()

        code, output = self.run_main()

        self.assertEqual(code, 0, output)
        self.assertIn("dry run: 0 renames, 0 row updates, 0 conflicts", output)

    def test_apply_updates_the_row_an_interrupted_run_left(self):
        self.record(123, "5_5.dem.bz2", 13)
        self.write("5_5.dem.zst", ZSTD_MAGIC + b"already renamed")

        code, output = self.run_main("--apply")

        self.assertEqual(code, 0, output)
        self.assertIn("updated row 123 -> 5_5.dem.zst", output)
        self.assertEqual(os.listdir(self.folder), ["5_5.dem.zst"])
        self.assertEqual(self.filename_for(123), "5_5.dem.zst")

    def test_a_conflict_is_reported_and_neither_file_changes(self):
        self.record(123, "6_6.dem.bz2", 13)
        self.write("6_6.dem.bz2", ZSTD_MAGIC + b"a")
        self.write("6_6.dem.zst", ZSTD_MAGIC + b"b")

        code, output = self.run_main("--apply")

        self.assertEqual((self.folder / "6_6.dem.zst").read_bytes(), ZSTD_MAGIC + b"b")
        self.assertEqual((self.folder / "6_6.dem.bz2").read_bytes(), ZSTD_MAGIC + b"a")
        self.assertEqual(self.filename_for(123), "6_6.dem.bz2")
        self.assertIn("conflict: 6_6.dem.zst already exists", output)
        self.assertEqual(code, 1, output)

    def test_a_failed_database_copy_renames_nothing(self):
        self.record(123, "7_7.dem.bz2", 13)
        self.write("7_7.dem.bz2", ZSTD_MAGIC + b"payload")
        self.write("8_8.dem.bz2", ZSTD_MAGIC + b"payload")
        listing_before = sorted(os.listdir(self.folder))

        with mock.patch.object(
            repair_replay_names, "_copy_database", side_effect=OSError("copy failed")
        ):
            code, output = self.run_main("--apply")

        self.assertEqual(code, 1)
        self.assertIn("copy failed", output)
        self.assertEqual(sorted(os.listdir(self.folder)), listing_before)
        self.assertEqual(
            (self.folder / "7_7.dem.bz2").read_bytes(), ZSTD_MAGIC + b"payload"
        )
        self.assertEqual(self.filename_for(123), "7_7.dem.bz2")


if __name__ == "__main__":
    unittest.main()
