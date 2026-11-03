"""Cases for the switchboard job entries, the submit helper and the copy."""

import ast
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import db_copy
import dota_replays
from database import Database
from dota_replays import DotaReplays
from opendota_client import OpenDotaClient
from tests.fake_hub import FakeHub
from tests.support import FIXTURE_PATH, RecordedOpenDota, ReplayServer, load_fixture

ROOT = Path(__file__).resolve().parent.parent
UPDATE_ENTRY = ROOT / "jobs" / "update"
VERIFY_ENTRY = ROOT / "jobs" / "verify"
CHECK_ENTRY = ROOT / "jobs" / "check"
SUBMIT_HELPER = ROOT / "scripts" / "switchboard-submit"
PLIST = ROOT / "switchboard" / "dev.dota-replays.update.plist"
HUB_TOKEN = "sbm_test_token"

PLAYER_ID = 86745912
MATCH_ID = 8554321
REPLAY_NAME = f"{MATCH_ID}.dem.bz2"


def run(command, cwd, env, merge=False, timeout=120):
    """Run command and return its completed process."""
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge else subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


class JobEntryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.bulk = self.root / "bulk"
        self.bulk.mkdir()
        self.replays = self.root / "0. Personal" / "Games" / "dota-replays"
        self.replays.mkdir(parents=True)
        self.legacy_copy = self.root / "legacy-copy"
        self.legacy_copy.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        os.symlink(sys.executable, self.bin / "python3")
        self.hub = FakeHub(token=HUB_TOKEN)
        self.addCleanup(self.hub.close)
        self.opendota = RecordedOpenDota(FIXTURE_PATH)
        self.addCleanup(self.opendota.close)

    def env(self, **overrides):
        """Return an entry environment that uses this test's python3."""
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "PYTHONUNBUFFERED": "1",
            "SWITCHBOARD_BULK": str(self.bulk),
            "DOTA_REPLAY_DIR": str(self.replays),
            "DOTA_DB_COPY_DIR": str(self.legacy_copy),
            "SWITCHBOARD_HUB": self.hub.base_url,
            "SWITCHBOARD_MODULE_TOKEN": HUB_TOKEN,
            "OPENDOTA_BASE_URL": f"{self.opendota.base_url}/api",
        }
        env.update(overrides)
        return env

    def run_entry(self, entry, env=None, merge=False, cwd=None):
        return run(
            [str(entry)], cwd or self.run_dir, env or self.env(), merge=merge
        )

    def case_env(self, label):
        """Return an environment and empty directories for one subtest."""
        case_root = self.root / label
        case_root.mkdir()
        directories = {}
        for name in ("bulk", "replays", "run"):
            directories[name] = case_root / name
            directories[name].mkdir()
        env = self.env(
            SWITCHBOARD_BULK=str(directories["bulk"]),
            DOTA_REPLAY_DIR=str(directories["replays"]),
        )
        return env, directories

    def assert_empty(self, directories):
        for name in ("bulk", "replays", "run"):
            self.assertEqual(os.listdir(directories[name]), [], name)

    def assert_nothing_created(self):
        self.assertEqual(os.listdir(self.run_dir), [])
        self.assertEqual(os.listdir(self.bulk), [])
        self.assertEqual(os.listdir(self.replays), [])
        self.assertEqual(os.listdir(self.legacy_copy), [])

    def seed_backup(self, stamp, players=2, matches=2, folder=None):
        target_dir = folder or (self.replays / "db-backups")
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = self.root / f"seed-entry-{stamp}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_db_path = temp_dir / "temp.db"
        db = Database(str(temp_db_path))
        try:
            for p in range(1, players + 1):
                db.upsert_player(p, f"Player {p}")
            if matches > 0:
                match_data = [
                    {
                        "match_id": 2000 + m,
                        "start_time": 1600000000 + m,
                        "duration": 1800,
                        "hero_id": 1,
                        "kills": 1,
                        "deaths": 1,
                        "assists": 1,
                        "radiant_win": True,
                        "player_slot": 0,
                    }
                    for m in range(1, matches + 1)
                ]
                db.insert_matches(1, match_data)
        finally:
            db.close()

        target = target_dir / f"dota_replays-{stamp}.db"
        src_uri = temp_db_path.resolve().as_uri() + "?mode=ro"
        src_conn = sqlite3.connect(src_uri, uri=True)
        try:
            dst_conn = sqlite3.connect(target)
            try:
                src_conn.backup(dst_conn)
                dst_conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        return target

    def parse_strays(self, output):
        pattern = re.compile(
            r"Stray entry in the replay folder: (?P<name>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\") \((?P<kind>file|directory|symlink|other)(?:, (?P<size>[0-9]+) bytes)?\)$"
        )
        pairs = []
        for line in output.splitlines():
            m = pattern.search(line)
            if m:
                name = ast.literal_eval(m.group("name"))
                kind = m.group("kind")
                pairs.append((name, kind))
        return pairs

    def test_a_missing_variable_fails_and_creates_nothing(self):
        for name in ("SWITCHBOARD_BULK", "DOTA_REPLAY_DIR"):
            with self.subTest(name=name):
                env, directories = self.case_env(f"missing-variable-{name}")
                del env[name]
                result = self.run_entry(UPDATE_ENTRY, env, cwd=directories["run"])
                self.assertNotEqual(result.returncode, 0)
                self.assert_empty(directories)

    def test_check_entry_compares_the_index_with_the_database(self):
        fixture = load_fixture()
        player_id = fixture["player_id"]
        match_id = fixture["match_ids"][0]
        self.seed_player(player_id)
        database = Database(str(self.bulk / "dota_replays.db"))
        try:
            database.insert_matches(
                player_id,
                [
                    {
                        "match_id": match_id,
                        "start_time": int(datetime.now().timestamp()),
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
        finally:
            database.close()

        missing = self.run_entry(CHECK_ENTRY)
        published = self.run_entry(UPDATE_ENTRY, merge=True)
        present = self.run_entry(CHECK_ENTRY, merge=True)

        self.assertEqual(published.returncode, 0, published.stdout)
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn("is missing from the store", missing.stderr)
        self.assertEqual(present.returncode, 0, present.stdout)
        self.assertIn("The index matches the database", present.stdout)

    def test_a_missing_directory_fails_and_is_not_created(self):
        cases = [
            (UPDATE_ENTRY, "SWITCHBOARD_BULK"),
            (UPDATE_ENTRY, "DOTA_REPLAY_DIR"),
            (VERIFY_ENTRY, "SWITCHBOARD_BULK"),
            (VERIFY_ENTRY, "DOTA_REPLAY_DIR"),
            (CHECK_ENTRY, "SWITCHBOARD_BULK"),
        ]
        for entry, name in cases:
            with self.subTest(entry=entry.name, name=name):
                label = f"missing-directory-{entry.name}-{name}"
                env, directories = self.case_env(label)
                missing = directories["run"].parent / "missing"
                env[name] = str(missing)
                result = self.run_entry(entry, env, cwd=directories["run"])
                self.assertEqual(result.returncode, 1)
                self.assertFalse(missing.exists())
                self.assert_empty(directories)

    def test_python_below_310_fails_without_running_the_cli(self):
        stub = self.bin / "python3"
        stub.unlink()
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            "    printf 'Python 3.9.6\\n'\n"
            "    exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

        result = self.run_entry(UPDATE_ENTRY)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.9.6", result.stdout)
        self.assertIn("below 3.10", result.stderr)
        self.assert_nothing_created()

    def test_update_entry_backs_up_the_database_into_the_replay_folder(self):
        fixture = load_fixture()
        player_id = fixture["player_id"]
        match_id = fixture["match_ids"][0]
        opendota = RecordedOpenDota(FIXTURE_PATH)
        self.addCleanup(opendota.close)
        replay_server = ReplayServer(b"a replay body")
        self.addCleanup(replay_server.close)
        opendota.responses[f"/api/matches/{match_id}"]["replay_url"] = (
            f"{replay_server.base_url}/570/{match_id}.dem.bz2"
        )
        self.seed_player(player_id)

        backup_dir = self.replays / "db-backups"

        # Subcase A: sets DOTA_DB_COPY_DIR to legacy-copy
        before_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        result_a = self.run_entry(
            UPDATE_ENTRY,
            self.env(
                OPENDOTA_BASE_URL=f"{opendota.base_url}/api",
                DOTA_DB_COPY_DIR=str(self.legacy_copy),
            ),
        )
        after_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        self.assertEqual(result_a.returncode, 0, result_a.stderr)
        candidates = [
            f
            for f in os.listdir(backup_dir)
            if re.fullmatch(r"dota_replays-[0-9]{8}T[0-9]{6}Z\.db", f)
        ] if backup_dir.exists() else []
        self.assertEqual(len(candidates), 1, f"db-backups holds candidates: {candidates}")
        backup_name = candidates[0]
        stamp = re.fullmatch(r"dota_replays-([0-9]{8}T[0-9]{6}Z)\.db", backup_name).group(1)
        self.assertTrue(before_stamp <= stamp <= after_stamp, f"{stamp} not in [{before_stamp}, {after_stamp}]")

        backup_file = backup_dir / backup_name
        conn = sqlite3.connect(backup_file.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            copied = conn.execute("SELECT player_id, persona_name FROM players ORDER BY player_id").fetchall()
        finally:
            conn.close()

        database = Database(str(self.bulk / "dota_replays.db"))
        try:
            original = database.conn.execute("SELECT player_id, persona_name FROM players ORDER BY player_id").fetchall()
        finally:
            database.close()
        self.assertEqual(copied, [tuple(row) for row in original])

        self.assertFalse((backup_dir / f"{backup_name}.part").exists())
        self.assertFalse((self.bulk / "dota_replays.db.copy").exists())
        self.assertEqual(os.listdir(self.legacy_copy), [])

        # Subcase B: unsets DOTA_DB_COPY_DIR: exits 0
        env_b = self.env(OPENDOTA_BASE_URL=f"{opendota.base_url}/api")
        env_b.pop("DOTA_DB_COPY_DIR", None)
        result_b = self.run_entry(UPDATE_ENTRY, env_b)
        self.assertEqual(result_b.returncode, 0, result_b.stderr)

    def test_an_entry_without_a_database_fails_and_creates_nothing(self):
        for entry in (UPDATE_ENTRY, VERIFY_ENTRY, CHECK_ENTRY):
            with self.subTest(entry=entry.name):
                env, directories = self.case_env(f"missing-db-{entry.name}")
                result = self.run_entry(entry, env, cwd=directories["run"])
                self.assertEqual(result.returncode, 1)
                self.assertIn("no database at", result.stderr)
                self.assert_empty(directories)

    def test_verify_entry_prints_status_before_verifying(self):
        database = Database(str(self.bulk / "dota_replays.db"))
        try:
            database.upsert_player(PLAYER_ID, "Test Player")
            database.insert_matches(
                PLAYER_ID,
                [
                    {
                        "match_id": MATCH_ID,
                        "start_time": int(datetime.now().timestamp()),
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
            database.record_download(MATCH_ID, REPLAY_NAME, 3)
        finally:
            database.close()
        (self.replays / REPLAY_NAME).write_bytes(b"abc")
        self.seed_backup(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            players=1,
            matches=1,
        )

        result = self.run_entry(VERIFY_ENTRY, merge=True)

        self.assertEqual(result.returncode, 0, result.stdout)
        output = result.stdout
        self.assertIn("DATABASE STATUS", output)
        self.assertIn("Players tracked:          1", output)
        self.assertIn("Verifying 1 downloads", output)
        self.assertIn("All files verified on disk", output)
        self.assertLess(
            output.index("DATABASE STATUS"), output.index("Verifying 1 downloads")
        )
        self.assertIsNotNone(self.hub.document("meta", "summary"))

    def test_a_failed_update_step_does_not_skip_the_later_steps(self):
        self.seed_player(1)
        log = self.root / "steps.txt"
        self.stub_step_runner(log, fail_step=1)

        result = self.run_entry(
            UPDATE_ENTRY, self.env(STUB_LOG=str(log)), merge=True
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        steps = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(steps), 4, steps)
        self.assertRegex(
            steps[0], r"^dota_replays\.py --db \S+/dota_replays\.db .* -n 100 "
        )
        self.assertNotIn("--recheck-parsing", steps[0])
        self.assertIn("--recheck-parsing", steps[1])
        self.assertEqual(steps[2], "db_copy.py")
        self.assertEqual(steps[3], "publish.py")
        exit_lines = [line for line in result.stdout.splitlines() if " exited " in line]
        self.assertEqual(
            exit_lines,
            [
                f"{UPDATE_ENTRY}: update exited 1",
                f"{UPDATE_ENTRY}: recheck-parsing exited 0",
                f"{UPDATE_ENTRY}: database-copy exited 0",
                f"{UPDATE_ENTRY}: publish exited 0",
            ],
        )
        self.assertIn("one or more steps failed", result.stdout)

    def test_a_failed_verify_step_does_not_skip_publish(self):
        self.seed_player(1)
        log = self.root / "steps.txt"
        self.stub_step_runner(log, fail_step=2)

        result = self.run_entry(
            VERIFY_ENTRY, self.env(STUB_LOG=str(log)), merge=True
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        steps = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(steps), 5, steps)
        self.assertIn("--status", steps[0])
        self.assertIn("--verify", steps[1])
        self.assertNotIn("--status", steps[1])
        self.assertIn("db_copy.py --check", steps[2])
        self.assertIn("replay_folder.py", steps[3])
        self.assertEqual(steps[4], "publish.py")
        exit_lines = [line for line in result.stdout.splitlines() if " exited " in line]
        self.assertEqual(
            exit_lines,
            [
                f"{VERIFY_ENTRY}: status exited 0",
                f"{VERIFY_ENTRY}: verify exited 1",
                f"{VERIFY_ENTRY}: backup-check exited 0",
                f"{VERIFY_ENTRY}: folder-check exited 0",
                f"{VERIFY_ENTRY}: publish exited 0",
            ],
        )
        self.assertIn("one or more steps failed", result.stdout)

    def test_the_verify_entry_runs_the_backup_check(self):
        self.seed_player(1)
        result = self.run_entry(VERIFY_ENTRY, merge=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("backup-check exited 1", result.stdout)
        self.assertIsNotNone(self.hub.document("meta", "summary"))

    def test_verify_names_every_stray_entry_in_the_replay_folder(self):
        database = Database(str(self.bulk / "dota_replays.db"))
        try:
            database.upsert_player(1, "Test Player")
            database.record_download(123456, "123_456.dem.bz2", 100)
        finally:
            database.close()
        (self.replays / "123_456.dem.bz2").write_bytes(b"replay data")
        fresh_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.seed_backup(fresh_stamp, players=1, matches=0)

        # Add strays:
        (self.replays / "100").write_bytes(b"x" * 100)
        (self.replays / "notes.txt").write_bytes(b"notes")
        (self.replays / ".DS_Store").write_bytes(b"ds")
        (self.replays / "misc").mkdir()
        outside = self.root / "outside.dem.bz2"
        outside.write_bytes(b"outside")
        os.symlink(str(outside), str(self.replays / "999_1.dem.bz2"))
        (self.replays / "123_457.dem.bz2.part").write_bytes(b"part")
        (self.replays / "bad name.dem.bz2").write_bytes(b"bad")
        (self.replays / "dota_replays-20261105T133012Z.db").write_bytes(b"backup")

        listing_before = sorted(os.listdir(self.replays))
        files_before = {
            f: (self.replays / f).read_bytes()
            for f in listing_before
            if (self.replays / f).is_file() and not (self.replays / f).is_symlink()
        }
        symlinks_before = {
            f: os.readlink(self.replays / f)
            for f in listing_before
            if (self.replays / f).is_symlink()
        }

        result = self.run_entry(VERIFY_ENTRY, merge=True)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("folder-check exited 1", result.stdout)
        self.assertIn("backup-check exited 0", result.stdout)
        pairs = self.parse_strays(result.stdout)
        expected = [
            (".DS_Store", "file"),
            ("100", "file"),
            ("123_457.dem.bz2.part", "file"),
            ("999_1.dem.bz2", "symlink"),
            ("bad name.dem.bz2", "file"),
            ("dota_replays-20261105T133012Z.db", "file"),
            ("misc", "directory"),
            ("notes.txt", "file"),
        ]
        self.assertEqual(pairs, expected)
        self.assertIn("Stray entries in the replay folder: 8", result.stdout)
        size_lines = [
            line
            for line in result.stdout.splitlines()
            if "Stray entry in the replay folder: '100'" in line
        ]
        self.assertEqual(len(size_lines), 1, size_lines)
        self.assertIn("(file, 100 bytes)", size_lines[0])
        self.assertIsNotNone(self.hub.document("meta", "summary"))

        listing_after = sorted(os.listdir(self.replays))
        self.assertEqual(listing_before, listing_after)
        files_after = {
            f: (self.replays / f).read_bytes()
            for f in listing_after
            if (self.replays / f).is_file() and not (self.replays / f).is_symlink()
        }
        self.assertEqual(files_before, files_after)
        symlinks_after = {
            f: os.readlink(self.replays / f)
            for f in listing_after
            if (self.replays / f).is_symlink()
        }
        self.assertEqual(symlinks_before, symlinks_after)

    def test_verify_passes_a_folder_of_replays_and_backups(self):
        database = Database(str(self.bulk / "dota_replays.db"))
        try:
            database.upsert_player(1, "Test Player")
            database.record_download(123456, "123_456.dem.bz2", 100)
        finally:
            database.close()
        (self.replays / "123_456.dem.bz2").write_bytes(b"replay 1")
        (self.replays / "123_458.dem.bz2").write_bytes(b"replay 2")
        fresh_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.seed_backup(fresh_stamp, players=1, matches=0)
        (self.replays / "db-backups" / "held").mkdir()

        listing_before = sorted(os.listdir(self.replays))
        files_before = {
            f: (self.replays / f).read_bytes()
            for f in listing_before
            if (self.replays / f).is_file() and not (self.replays / f).is_symlink()
        }

        result = self.run_entry(VERIFY_ENTRY, merge=True)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("folder-check exited 0", result.stdout)
        self.assertIn("holds 2 replay files and db-backups", result.stdout)

        listing_after = sorted(os.listdir(self.replays))
        self.assertEqual(listing_before, listing_after)
        files_after = {
            f: (self.replays / f).read_bytes()
            for f in listing_after
            if (self.replays / f).is_file() and not (self.replays / f).is_symlink()
        }
        self.assertEqual(files_before, files_after)

    def test_a_db_backups_symlink_is_a_stray(self):
        database = Database(str(self.bulk / "dota_replays.db"))
        try:
            database.upsert_player(1, "Test Player")
            database.record_download(123456, "123_456.dem.bz2", 100)
        finally:
            database.close()
        (self.replays / "123_456.dem.bz2").write_bytes(b"replay 1")

        outside_dir = self.root / "outside_backups"
        outside_dir.mkdir()
        fresh_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.seed_backup(fresh_stamp, folder=outside_dir)
        os.symlink(str(outside_dir), str(self.replays / "db-backups"))

        listing_before = sorted(os.listdir(self.replays))
        files_before = {
            f: (self.replays / f).read_bytes()
            for f in listing_before
            if (self.replays / f).is_file() and not (self.replays / f).is_symlink()
        }
        symlinks_before = {
            f: os.readlink(self.replays / f)
            for f in listing_before
            if (self.replays / f).is_symlink()
        }

        result = self.run_entry(VERIFY_ENTRY, merge=True)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("folder-check exited 1", result.stdout)
        pairs = self.parse_strays(result.stdout)
        self.assertEqual(pairs, [("db-backups", "symlink")])

        listing_after = sorted(os.listdir(self.replays))
        self.assertEqual(listing_before, listing_after)
        symlinks_after = {
            f: os.readlink(self.replays / f)
            for f in listing_after
            if (self.replays / f).is_symlink()
        }
        self.assertEqual(symlinks_before, symlinks_after)
        files_after = {
            f: (self.replays / f).read_bytes()
            for f in listing_after
            if (self.replays / f).is_file() and not (self.replays / f).is_symlink()
        }
        self.assertEqual(files_before, files_after)

    def test_a_name_with_a_newline_is_logged_quoted(self):
        database = Database(str(self.bulk / "dota_replays.db"))
        try:
            database.upsert_player(1, "Test Player")
            database.record_download(123456, "123_456.dem.bz2", 100)
        finally:
            database.close()
        (self.replays / "123_456.dem.bz2").write_bytes(b"replay 1")
        fresh_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.seed_backup(fresh_stamp, players=1, matches=0)

        evil_name = "evil\nStray entry in the replay folder: 'fake' (file, 1 bytes)"
        (self.replays / evil_name).write_bytes(b"1")

        listing_before = sorted(os.listdir(self.replays))
        files_before = {
            f: (self.replays / f).read_bytes()
            for f in listing_before
            if (self.replays / f).is_file() and not (self.replays / f).is_symlink()
        }

        result = self.run_entry(VERIFY_ENTRY, merge=True)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("folder-check exited 1", result.stdout)
        self.assertIn("backup-check exited 0", result.stdout)
        marker = "Stray entry in the replay folder: "
        marker_lines = [l for l in result.stdout.splitlines() if marker in l]
        self.assertEqual(len(marker_lines), 1, marker_lines)
        pairs = self.parse_strays(result.stdout)
        self.assertEqual(pairs, [(evil_name, "file")])
        self.assertIn("Stray entries in the replay folder: 1", result.stdout)

        listing_after = sorted(os.listdir(self.replays))
        self.assertEqual(listing_before, listing_after)
        files_after = {
            f: (self.replays / f).read_bytes()
            for f in listing_after
            if (self.replays / f).is_file() and not (self.replays / f).is_symlink()
        }
        self.assertEqual(files_before, files_after)

    def stub_step_runner(self, log, fail_step):
        """Replace python3 with a stub that records each step and fails one."""
        stub = self.bin / "python3"
        stub.unlink()
        stub.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "    --version) printf 'Python 3.13.0\\n'; exit 0 ;;\n"
            "    -c) exit 0 ;;\n"
            "esac\n"
            "printf '%s\\n' \"$*\" >> \"$STUB_LOG\"\n"
            "count=$(wc -l < \"$STUB_LOG\")\n"
            f"if [ \"$count\" -eq {fail_step} ]; then\n"
            "    exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

    def seed_player(self, player_id):
        database = Database(str(self.bulk / "dota_replays.db"))
        try:
            database.upsert_player(player_id, "Test Player")
        finally:
            database.close()


class SubmitHelperTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.cache = self.root / "cache"
        self.stub_log = self.root / "switchboard-argv.txt"
        self.date_log = self.root / "date-calls.txt"
        self.gitconfig = self.root / "gitconfig"
        self.gitconfig.write_text("", encoding="utf-8")
        self.token = self.root / "submit.token"
        self.token.write_text("sba_test_token\n", encoding="utf-8")
        self.write_stub(
            "switchboard",
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$@" > "$STUB_LOG"\n'
            "exit 0\n",
        )
        self.write_stub(
            "date",
            "#!/bin/sh\n"
            'printf \'TZ=%s\\n\' "${TZ:-}" >> "$STUB_DATE_LOG"\n'
            'printf \'arg=%s\\n\' "$@" >> "$STUB_DATE_LOG"\n'
            'printf \'%s\\n\' "$STUB_DATE_HHMM"\n',
        )
        self.make_fixture_clone()

    def write_stub(self, name, text):
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def git(self, *arguments, env=None):
        result = subprocess.run(
            ["git", *arguments],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return result.stdout

    def git_env(self):
        config = self.root / "gitconfig"
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
        }
        env.update(
            {
                "GIT_CONFIG_GLOBAL": str(config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "Fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.com",
                "GIT_COMMITTER_NAME": "Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.com",
            }
        )
        return env

    def make_fixture_clone(self):
        env = self.git_env()
        repository = self.root / "fixture"
        repository.mkdir()
        (repository / "jobs").mkdir()
        (repository / "jobs" / "update").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (repository / "jobs" / "verify").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.git("init", "--quiet", str(repository), env=env)
        self.git(
            "-C",
            str(repository),
            "symbolic-ref",
            "HEAD",
            "refs/heads/master",
            env=env,
        )
        self.git("-C", str(repository), "add", ".", env=env)
        self.git("-C", str(repository), "commit", "--quiet", "-m", "fixture", env=env)
        self.head = self.git("-C", str(repository), "rev-parse", "HEAD", env=env).strip()
        self.tree = self.git(
            "-C", str(repository), "rev-parse", "HEAD^{tree}", env=env
        ).strip()
        clone = self.cache / "dota-replays-submit" / "repo.git"
        clone.parent.mkdir(parents=True)
        self.git("clone", "--bare", "--quiet", str(repository), str(clone), env=env)

    def helper_env(self, **overrides):
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_CACHE_HOME": str(self.cache),
            "STUB_LOG": str(self.stub_log),
            "STUB_DATE_LOG": str(self.date_log),
            "STUB_DATE_HHMM": "1200",
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.com",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.com",
        }
        env.update(overrides)
        return env

    def run_helper(self, arguments, **overrides):
        return run(
            [str(SUBMIT_HELPER), *arguments],
            self.run_dir,
            self.helper_env(**overrides),
        )

    def recorded_argv(self):
        return self.stub_log.read_text(encoding="utf-8").splitlines()

    def value_after(self, argv, flag):
        return argv[argv.index(flag) + 1]

    def test_the_argv_names_the_entry_resource_and_limits(self):
        for task, backstop, quiet in (
            ("update", "10800", "600"),
            ("verify", "1800", "600"),
            ("check", "600", "300"),
        ):
            with self.subTest(task=task):
                self.stub_log.unlink(missing_ok=True)
                result = self.run_helper(
                    [
                        "--hub",
                        "https://hub.example.com",
                        "--token-file",
                        str(self.token),
                        task,
                    ]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = self.recorded_argv()
                self.assertEqual(self.value_after(argv, "--project"), "dota-replays")
                self.assertEqual(self.value_after(argv, "--task"), task)
                self.assertEqual(self.value_after(argv, "--entry"), f"jobs/{task}")
                self.assertEqual(
                    self.value_after(argv, "--repo"), "simonliu99/dota-replays"
                )
                self.assertEqual(
                    self.value_after(argv, "--gres"), "bulk-dota-replays=1"
                )
                self.assertEqual(
                    self.value_after(argv, "--secret"), "SWITCHBOARD_MODULE_TOKEN"
                )
                self.assertEqual(self.value_after(argv, "--base"), self.head)
                self.assertEqual(self.value_after(argv, "--commit"), self.head)
                self.assertEqual(self.value_after(argv, "--tree"), self.tree)
                self.assertEqual(
                    self.value_after(argv, "--demand-cores"),
                    "1",
                )
                self.assertEqual(self.value_after(argv, "--demand-mem-mb"), "512")
                self.assertEqual(self.value_after(argv, "--backstop"), backstop)
                self.assertEqual(self.value_after(argv, "--quiet"), quiet)
                self.assertRegex(self.head, r"^[0-9a-f]{40}$")
                self.assertIn(str(self.token), argv)
                self.assertNotIn("sba_test_token", "\n".join(argv))

    def test_a_call_without_hub_or_token_exits_before_switchboard(self):
        cases = [
            ["--token-file", str(self.token), "update"],
            ["--hub", "https://hub.example.com", "update"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.stub_log.unlink(missing_ok=True)
                result = self.run_helper(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.stub_log.exists())

    def test_not_before_refuses_a_time_before_0500_pacific(self):
        result = self.run_helper(
            [
                "--hub",
                "https://hub.example.com",
                "--token-file",
                str(self.token),
                "--not-before",
                "05:00",
                "update",
            ],
            STUB_DATE_HHMM="0459",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.stub_log.exists())
        self.assertIn("before 05:00", result.stderr)
        calls = self.date_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls[0], "TZ=America/Los_Angeles")
        self.assertIn("arg=+%H%M", calls)

    def test_not_before_submits_at_0500_pacific(self):
        result = self.run_helper(
            [
                "--hub",
                "https://hub.example.com",
                "--token-file",
                str(self.token),
                "--not-before",
                "05:00",
                "update",
            ],
            STUB_DATE_HHMM="0500",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.recorded_argv()
        self.assertEqual(self.value_after(argv, "--task"), "update")


class DatabaseCopyTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bulk = self.root / "bulk"
        self.bulk.mkdir()
        self.replays = self.root / "replays"
        self.replays.mkdir()
        self.backup_dir = self.replays / "db-backups"
        self.legacy_copy = self.root / "legacy-copy"
        self.legacy_copy.mkdir()
        self.check_run = self.root / "check_run"
        self.check_run.mkdir()
        self.database = Database(str(self.bulk / "dota_replays.db"))
        self.addCleanup(self.database.close)
        self.database.upsert_player(1, "First Player")
        self.database.upsert_player(2, "Second Player")
        self.insert_matches(2)
        environment = mock.patch.dict(
            os.environ,
            {
                "SWITCHBOARD_BULK": str(self.bulk),
                "DOTA_REPLAY_DIR": str(self.replays),
                "DOTA_DB_COPY_DIR": str(self.legacy_copy),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def insert_matches(self, count):
        if count > 0:
            match_data = [
                {
                    "match_id": 1000 + m,
                    "start_time": 1600000000 + m,
                    "duration": 1800,
                    "hero_id": 1,
                    "kills": 1,
                    "deaths": 1,
                    "assists": 1,
                    "radiant_win": True,
                    "player_slot": 0,
                }
                for m in range(1, count + 1)
            ]
            self.database.insert_matches(1, match_data)

    def seed_backup(self, stamp, players=2, matches=2):
        temp_dir = self.root / f"seed-{stamp}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_db_path = temp_dir / "temp.db"
        db = Database(str(temp_db_path))
        try:
            for p in range(1, players + 1):
                db.upsert_player(p, f"Player {p}")
            if matches > 0:
                match_data = [
                    {
                        "match_id": 2000 + m,
                        "start_time": 1600000000 + m,
                        "duration": 1800,
                        "hero_id": 1,
                        "kills": 1,
                        "deaths": 1,
                        "assists": 1,
                        "radiant_win": True,
                        "player_slot": 0,
                    }
                    for m in range(1, matches + 1)
                ]
                db.insert_matches(1, match_data)
        finally:
            db.close()

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"dota_replays-{stamp}.db"
        src_uri = temp_db_path.resolve().as_uri() + "?mode=ro"
        src_conn = sqlite3.connect(src_uri, uri=True)
        try:
            dst_conn = sqlite3.connect(target)
            try:
                src_conn.backup(dst_conn)
                dst_conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        return target

    def run_check(self, *arguments):
        return subprocess.run(
            [sys.executable, str(ROOT / "db_copy.py"), "--check", *arguments],
            cwd=str(self.check_run),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_backups_beyond_fourteen_are_pruned_oldest_first(self):
        for day in range(1, 15):
            self.seed_backup(f"200101{day:02d}T000000Z")
        (self.backup_dir / "100").write_bytes(b"100")
        (self.backup_dir / "notes.txt").write_bytes(b"notes")
        (self.backup_dir / "dota_replays-20010101T000000Z.db.bak").write_bytes(b"bak")
        (self.backup_dir / "dota_replays-99999999T999999Z.db").write_bytes(b"invalid date")
        (self.backup_dir / "dota_replays-٢٠٠١٠١٠١T000000Z.db").write_bytes(b"arabic")
        (self.backup_dir / "dota_replays-20000101T000000Z.db").mkdir()
        outside = self.root / "outside.txt"
        outside.write_bytes(b"target")
        link = self.backup_dir / "dota_replays-20000102T000000Z.db"
        os.symlink(str(outside), str(link))

        self.assertEqual(db_copy.main([]), 0)

        candidates = [
            f
            for f in os.listdir(self.backup_dir)
            if re.fullmatch(r"dota_replays-([0-9]{8}T[0-9]{6}Z)\.db", f)
            and os.path.isfile(self.backup_dir / f)
            and not os.path.islink(self.backup_dir / f)
        ]
        valid_candidates = []
        for c in candidates:
            m = re.fullmatch(r"dota_replays-([0-9]{8}T[0-9]{6}Z)\.db", c)
            try:
                time.strptime(m.group(1), "%Y%m%dT%H%M%SZ")
                valid_candidates.append(c)
            except ValueError:
                pass
        now_stamp = time.strftime("%Y%m%dT", time.gmtime())
        new_candidates = [
            c for c in valid_candidates if c.startswith(f"dota_replays-{now_stamp}")
        ]
        self.assertEqual(len(new_candidates), 1)
        self.assertEqual(len(valid_candidates), 14)
        self.assertFalse((self.backup_dir / "dota_replays-20010101T000000Z.db").exists())
        self.assertTrue((self.backup_dir / "dota_replays-20010102T000000Z.db").is_file())
        self.assertEqual((self.backup_dir / "100").read_bytes(), b"100")
        self.assertEqual((self.backup_dir / "notes.txt").read_bytes(), b"notes")
        self.assertEqual((self.backup_dir / "dota_replays-20010101T000000Z.db.bak").read_bytes(), b"bak")
        self.assertEqual((self.backup_dir / "dota_replays-99999999T999999Z.db").read_bytes(), b"invalid date")
        self.assertEqual((self.backup_dir / "dota_replays-٢٠٠١٠١٠١T000000Z.db").read_bytes(), b"arabic")
        self.assertTrue((self.backup_dir / "dota_replays-20000101T000000Z.db").is_dir())
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), str(outside))
        self.assertEqual(outside.read_bytes(), b"target")

    def test_a_backup_dated_after_the_clock_stops_pruning(self):
        for day in range(1, 15):
            self.seed_backup(f"200101{day:02d}T000000Z")
        future = self.seed_backup("20990101T000000Z")
        self.assertEqual(db_copy.main([]), 1)
        for day in range(1, 15):
            self.assertTrue((self.backup_dir / f"dota_replays-200101{day:02d}T000000Z.db").is_file())
        self.assertTrue(future.is_file())
        now_stamp = time.strftime("%Y%m%dT", time.gmtime())
        new_backups = [f for f in os.listdir(self.backup_dir) if f.startswith(f"dota_replays-{now_stamp}")]
        self.assertEqual(len(new_backups), 1)

    def test_a_short_copy_leaves_no_backup(self):
        earlier = self.seed_backup("20010101T000000Z")
        earlier_bytes = earlier.read_bytes()
        real_copyfile = shutil.copyfile

        def fake_copyfile(src, dst, *args, **kwargs):
            if "db-backups" in str(dst):
                with open(src, "rb") as s, open(dst, "wb") as d:
                    content = s.read()
                    d.write(content[: len(content) // 2])
                return dst
            return real_copyfile(src, dst, *args, **kwargs)

        with mock.patch("db_copy.shutil.copyfile", side_effect=fake_copyfile):
            self.assertEqual(db_copy.main([]), 1)

        self.assertEqual(earlier.read_bytes(), earlier_bytes)
        names = os.listdir(self.backup_dir)
        self.assertEqual(names, ["dota_replays-20010101T000000Z.db"])
        self.assertFalse(any(n.endswith(".part") for n in names))

    def test_a_symlinked_backup_folder_is_refused(self):
        outside = self.root / "outside_backups"
        outside.mkdir()
        os.symlink(str(outside), str(self.backup_dir))
        self.assertEqual(db_copy.main([]), 1)
        self.assertEqual(os.listdir(outside), [])

    def test_a_stale_part_is_removed(self):
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        part = self.backup_dir / "dota_replays-20010101T000000Z.db.part"
        part.write_bytes(b"stale part")
        arabic_part = self.backup_dir / "dota_replays-٢٠٠١٠١٠١T000000Z.db.part"
        arabic_part.write_bytes(b"arabic part")
        self.assertEqual(db_copy.main([]), 0)
        self.assertFalse(part.exists())
        self.assertTrue(arabic_part.is_file())
        self.assertEqual(arabic_part.read_bytes(), b"arabic part")

    def test_an_unwritable_backup_folder_keeps_the_earlier_backups(self):
        if os.geteuid() == 0:
            self.skipTest("root writes into a directory with no write permission")
        self.assertEqual(db_copy.main([]), 0)
        backups = (
            [f for f in os.listdir(self.backup_dir) if f.endswith(".db")]
            if self.backup_dir.exists()
            else []
        )
        self.assertEqual(len(backups), 1)
        first_backup = self.backup_dir / backups[0]
        first_bytes = first_backup.read_bytes()

        self.backup_dir.chmod(0o555)
        self.addCleanup(self.backup_dir.chmod, 0o755)

        later = time.gmtime(time.time() + 60)
        with mock.patch("time.gmtime", return_value=later):
            with self.assertLogs("db_copy", level="ERROR") as captured:
                self.assertEqual(db_copy.main([]), 1)
        self.assertIn("Permission denied", "\n".join(captured.output))
        self.assertEqual(first_backup.read_bytes(), first_bytes)
        self.assertEqual(os.listdir(self.backup_dir), [backups[0]])

    def test_a_missing_replay_folder_creates_nothing(self):
        missing = self.root / "missing_replays"
        with mock.patch.dict(os.environ, {"DOTA_REPLAY_DIR": str(missing)}):
            parent_listing = os.listdir(self.root)
            with self.assertRaises(SystemExit):
                db_copy.main([])
            self.assertFalse(missing.exists())
            self.assertEqual(os.listdir(self.root), parent_listing)

    def test_a_smaller_database_writes_and_prunes_nothing(self):
        seeded_bytes = {}
        for day in range(1, 15):
            name = f"200101{day:02d}T000000Z"
            path = self.seed_backup(name, players=2, matches=2)
            seeded_bytes[path.name] = path.read_bytes()

        live_db_path = self.bulk / "dota_replays.db"
        self.database.close()
        live_db_path.unlink()
        self.database = Database(str(live_db_path))
        self.database.upsert_player(1, "Only Player")
        self.insert_matches(1)

        with self.assertLogs("db_copy", level="ERROR") as captured:
            self.assertEqual(db_copy.main([]), 1)

        for name, data in seeded_bytes.items():
            self.assertEqual((self.backup_dir / name).read_bytes(), data)
        self.assertEqual(set(os.listdir(self.backup_dir)), set(seeded_bytes.keys()))
        self.assertFalse(any(n.endswith(".part") for n in os.listdir(self.backup_dir)))
        output_str = "\n".join(captured.output)
        self.assertIn("1 matches", output_str)
        self.assertIn("2 matches", output_str)

    def test_a_refused_shrink_still_refuses_on_the_next_run(self):
        seeded_bytes = {}
        for day in range(1, 15):
            name = f"200101{day:02d}T000000Z"
            path = self.seed_backup(name, players=2, matches=2)
            seeded_bytes[path.name] = path.read_bytes()

        live_db_path = self.bulk / "dota_replays.db"
        self.database.close()
        live_db_path.unlink()
        self.database = Database(str(live_db_path))
        self.database.upsert_player(1, "Only Player")
        self.insert_matches(1)

        self.assertEqual(db_copy.main([]), 1)
        self.assertEqual(db_copy.main([]), 1)

        for name, data in seeded_bytes.items():
            self.assertEqual((self.backup_dir / name).read_bytes(), data)
        self.assertEqual(set(os.listdir(self.backup_dir)), set(seeded_bytes.keys()))

    def test_an_existing_backup_name_is_never_overwritten(self):
        target = self.seed_backup("20261105T133012Z")
        kept = target.read_bytes()
        with mock.patch("time.gmtime", return_value=time.struct_time((2026, 11, 5, 13, 30, 12, 3, 309, 0))):
            self.assertEqual(db_copy.main([]), 1)
        self.assertEqual(target.read_bytes(), kept)
        self.assertFalse(any(n.endswith(".part") for n in os.listdir(self.backup_dir)))

    def test_backups_moved_into_held_let_a_smaller_database_back_up(self):
        seeded_bytes = {}
        for day in range(1, 15):
            name = f"200101{day:02d}T000000Z"
            path = self.seed_backup(name, players=2, matches=2)
            seeded_bytes[path.name] = path.read_bytes()

        live_db_path = self.bulk / "dota_replays.db"
        self.database.close()
        live_db_path.unlink()
        self.database = Database(str(live_db_path))
        self.database.upsert_player(1, "Only Player")
        self.insert_matches(1)

        self.assertEqual(db_copy.main([]), 1)

        held_dir = self.backup_dir / "held"
        held_dir.mkdir()
        for name in seeded_bytes:
            os.replace(self.backup_dir / name, held_dir / name)

        self.assertEqual(db_copy.main([]), 0)

        candidates = [
            f
            for f in os.listdir(self.backup_dir)
            if re.fullmatch(r"dota_replays-[0-9]{8}T[0-9]{6}Z\.db", f)
            and (self.backup_dir / f).is_file()
        ]
        self.assertEqual(len(candidates), 1)
        conn = sqlite3.connect((self.backup_dir / candidates[0]).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
            matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            self.assertEqual((players, matches), (1, 1))
        finally:
            conn.close()

        for name, data in seeded_bytes.items():
            self.assertEqual((held_dir / name).read_bytes(), data)

    def test_check_passes_on_a_fresh_backup(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.seed_backup(stamp)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(stamp, result.stderr)
        self.assertIn("players", result.stderr)
        self.assertIn("matches", result.stderr)
        self.assertEqual(os.listdir(self.legacy_copy), [])

    def test_check_fails_without_a_backup(self):
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("No candidate backups", result.stderr)

    def test_check_fails_on_an_old_backup(self):
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y%m%dT%H%M%SZ")
        self.seed_backup(old_stamp)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        # A stamp one hour old passes
        fresh_stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
        self.seed_backup(fresh_stamp)
        result_fresh = self.run_check()
        self.assertEqual(result_fresh.returncode, 0, result_fresh.stderr)

    def test_check_fails_when_the_database_lost_matches(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.seed_backup(stamp, players=2, matches=2)
        # Delete one match from live database
        self.database.conn.execute("DELETE FROM matches WHERE match_id = (SELECT MIN(match_id) FROM matches)")
        self.database.conn.commit()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Database lost records", result.stderr)
        self.assertIn("1 matches", result.stderr)
        self.assertIn("2 matches", result.stderr)

    def test_check_fails_on_a_corrupt_backup(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.seed_backup(stamp)
        data = bytearray(path.read_bytes())
        page_size = 4096
        mid_page = (len(data) // page_size // 2) * page_size
        data[mid_page : mid_page + page_size] = b"\x00" * page_size
        path.write_bytes(data)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Integrity check failed for newest backup", result.stderr)

    def test_check_fails_on_more_than_fourteen_backups(self):
        now = datetime.now(timezone.utc)
        for hours in range(15):
            stamp = (now - timedelta(hours=hours)).strftime("%Y%m%dT%H%M%SZ")
            self.seed_backup(stamp)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Too many candidate backups", result.stderr)

    def test_check_fails_on_a_short_copy(self):
        self.seed_backup(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        real_copyfile = shutil.copyfile

        def short_copyfile(src, dst, *args, **kwargs):
            real_copyfile(src, dst, *args, **kwargs)
            dst_path = Path(dst)
            dst_path.write_bytes(dst_path.read_bytes()[: dst_path.stat().st_size // 2])

        with mock.patch("db_copy.shutil.copyfile", side_effect=short_copyfile):
            with self.assertLogs("db_copy", level="ERROR") as captured:
                self.assertEqual(db_copy.check_backups(8), 1)
        self.assertTrue(any("Size mismatch" in line for line in captured.output))

    def test_check_fails_on_a_bad_backup_folder(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outside = self.root / "outside_backups"
        outside.mkdir()
        self.backup_dir = outside
        self.seed_backup(stamp)
        self.backup_dir = self.replays / "db-backups"
        os.symlink(str(outside), str(self.backup_dir))
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("is missing, a symlink or not a directory", result.stderr)

        os.unlink(str(self.backup_dir))
        self.backup_dir.write_bytes(b"not a directory")
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("is missing, a symlink or not a directory", result.stderr)

        self.backup_dir.unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("is missing, a symlink or not a directory", result.stderr)
        self.assertFalse(self.backup_dir.exists())

    def test_check_fails_on_a_backup_dated_in_the_future(self):
        future_stamp = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        self.seed_backup(future_stamp)
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("more than one hour in the future", result.stderr)


class ParseWaitBudgetTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_the_cli_takes_the_option(self):
        with mock.patch.object(
            sys, "argv", ["dota_replays.py", "--parse-wait-budget", "30"]
        ):
            arguments = dota_replays.parse_args()
        self.assertEqual(arguments.parse_wait_budget, 30)

    def test_the_budget_caps_the_waits_and_later_matches_skip_the_wait(self):
        client = mock.MagicMock(spec=OpenDotaClient)
        database = mock.MagicMock(spec=Database)
        now = int(datetime.now().timestamp())
        matches = [
            {"match_id": 700 + index, "start_time": now - index} for index in range(3)
        ]
        database.get_matches_without_details.return_value = matches
        client.get_match_details.side_effect = [
            {
                "match_id": match["match_id"],
                "start_time": match["start_time"],
                "version": None,
            }
            for match in matches
        ]
        client.request_parse.side_effect = ["job-1", "job-2", "job-3"]
        clock = [1000.0]
        timeouts = []

        def poll(job_id, timeout=20, poll_interval=20):
            timeouts.append(timeout)
            clock[0] += timeout
            return False

        client.poll_parse_completion.side_effect = poll
        app = DotaReplays(
            db=database,
            client=client,
            replay_dir=self.root / "replays",
            wait_for_parse=True,
            parse_wait_budget=30,
        )

        with mock.patch("dota_replays.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertLogs("dota_replays", level="INFO") as captured:
                app._fetch_match_details(player_id=1)

        self.assertEqual(timeouts, [30])
        self.assertEqual(client.request_parse.call_count, 3)
        self.assertEqual(client.poll_parse_completion.call_count, 1)
        self.assertEqual(
            len([line for line in captured.output if "budget spent" in line]), 2
        )


class TriggerTestCase(unittest.TestCase):
    def test_each_shell_script_parses_and_is_executable(self):
        for script in (UPDATE_ENTRY, VERIFY_ENTRY, CHECK_ENTRY, SUBMIT_HELPER):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ["sh", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(os.access(script, os.X_OK), f"{script} is not executable")

    def test_plutil_accepts_the_trigger(self):
        result = subprocess.run(
            ["plutil", "-lint", str(PLIST)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_the_trigger_submits_weekly_after_0500_on_thursday(self):
        with PLIST.open("rb") as handle:
            plist = plistlib.load(handle)

        self.assertEqual(plist["Label"], "dev.dota-replays.update")
        arguments = plist["ProgramArguments"]
        self.assertEqual(arguments[-1], "update")
        self.assertEqual(arguments[arguments.index("--not-before") + 1], "05:00")
        self.assertEqual(
            plist["StartCalendarInterval"],
            [{"Weekday": 4, "Hour": 5, "Minute": 30}],
        )


if __name__ == "__main__":
    unittest.main()
