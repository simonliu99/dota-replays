"""Cases for the switchboard job entries, the submit helper and the copy."""

import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import db_copy
import dota_replays
from database import Database
from dota_replays import DotaReplays
from opendota_client import OpenDotaClient
from tests.support import FIXTURE_PATH, RecordedOpenDota, ReplayServer, load_fixture

ROOT = Path(__file__).resolve().parent.parent
UPDATE_ENTRY = ROOT / "jobs" / "update"
VERIFY_ENTRY = ROOT / "jobs" / "verify"
SUBMIT_HELPER = ROOT / "scripts" / "switchboard-submit"
PLIST = ROOT / "switchboard" / "dev.dota-replays.update.plist"

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
        self.replays = self.root / "replays"
        self.replays.mkdir()
        self.copy_dir = self.root / "copy"
        self.copy_dir.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        os.symlink(sys.executable, self.bin / "python3")

    def env(self, **overrides):
        """Return an entry environment that uses this test's python3."""
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "PYTHONUNBUFFERED": "1",
            "SWITCHBOARD_BULK": str(self.bulk),
            "DOTA_REPLAY_DIR": str(self.replays),
            "DOTA_DB_COPY_DIR": str(self.copy_dir),
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
        for name in ("bulk", "replays", "copy", "run"):
            directories[name] = case_root / name
            directories[name].mkdir()
        env = self.env(
            SWITCHBOARD_BULK=str(directories["bulk"]),
            DOTA_REPLAY_DIR=str(directories["replays"]),
            DOTA_DB_COPY_DIR=str(directories["copy"]),
        )
        return env, directories

    def assert_empty(self, directories):
        for name in ("bulk", "replays", "copy", "run"):
            self.assertEqual(os.listdir(directories[name]), [], name)

    def assert_nothing_created(self):
        self.assertEqual(os.listdir(self.run_dir), [])
        self.assertEqual(os.listdir(self.bulk), [])
        self.assertEqual(os.listdir(self.replays), [])
        self.assertEqual(os.listdir(self.copy_dir), [])

    def test_a_missing_variable_fails_and_creates_nothing(self):
        for name in ("SWITCHBOARD_BULK", "DOTA_REPLAY_DIR", "DOTA_DB_COPY_DIR"):
            with self.subTest(name=name):
                env, directories = self.case_env(f"missing-variable-{name}")
                del env[name]
                result = self.run_entry(UPDATE_ENTRY, env, cwd=directories["run"])
                self.assertNotEqual(result.returncode, 0)
                self.assert_empty(directories)

    def test_a_missing_directory_fails_and_is_not_created(self):
        cases = [
            (UPDATE_ENTRY, "SWITCHBOARD_BULK"),
            (UPDATE_ENTRY, "DOTA_REPLAY_DIR"),
            (UPDATE_ENTRY, "DOTA_DB_COPY_DIR"),
            (VERIFY_ENTRY, "SWITCHBOARD_BULK"),
            (VERIFY_ENTRY, "DOTA_REPLAY_DIR"),
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

    def test_update_entry_fetches_matches_and_copies_the_database(self):
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

        result = self.run_entry(
            UPDATE_ENTRY, self.env(OPENDOTA_BASE_URL=f"{opendota.base_url}/api")
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(opendota.requests[0], ("GET", f"/api/players/{player_id}"))
        self.assertIn(("GET", f"/api/matches/{match_id}"), opendota.requests)
        self.assertEqual(os.listdir(self.run_dir), [])

        database = Database(str(self.bulk / "dota_replays.db"))
        self.addCleanup(database.close)
        self.assertEqual(
            [player["player_id"] for player in database.get_all_players()],
            [player_id],
        )
        self.assertEqual(database.get_match_ids_for_player(player_id), {match_id})

        copy_path = self.copy_dir / "dota_replays.db"
        self.assertTrue(copy_path.is_file())
        copy_connection = sqlite3.connect(
            copy_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        try:
            self.assertEqual(
                copy_connection.execute("PRAGMA integrity_check").fetchall(),
                [("ok",)],
            )
            copied = copy_connection.execute(
                "SELECT player_id, persona_name FROM players ORDER BY player_id"
            ).fetchall()
        finally:
            copy_connection.close()
        original = database.conn.execute(
            "SELECT player_id, persona_name FROM players ORDER BY player_id"
        ).fetchall()
        self.assertEqual(copied, [tuple(row) for row in original])
        self.assertFalse((self.copy_dir / "dota_replays.db.part").exists())
        self.assertFalse((self.bulk / "dota_replays.db.copy").exists())
        self.assertIn("Copied", result.stderr)

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
        for task, backstop in (("update", "10800"), ("verify", "1800")):
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
                self.assertEqual(self.value_after(argv, "--quiet"), "600")
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
        self.copy_dir = self.root / "copy"
        self.copy_dir.mkdir()
        self.database = Database(str(self.bulk / "dota_replays.db"))
        self.addCleanup(self.database.close)
        self.database.upsert_player(1, "First Player")
        self.database.upsert_player(2, "Second Player")
        environment = mock.patch.dict(
            os.environ,
            {
                "SWITCHBOARD_BULK": str(self.bulk),
                "DOTA_DB_COPY_DIR": str(self.copy_dir),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_copy_is_checked_and_holds_the_same_players(self):
        with self.assertLogs("db_copy", level="INFO") as captured:
            self.assertEqual(db_copy.main(), 0)

        target = self.copy_dir / "dota_replays.db"
        self.assertTrue(target.is_file())
        connection = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)]
            )
            copied = connection.execute(
                "SELECT player_id, persona_name FROM players ORDER BY player_id"
            ).fetchall()
        finally:
            connection.close()
        original = self.database.conn.execute(
            "SELECT player_id, persona_name FROM players ORDER BY player_id"
        ).fetchall()
        self.assertEqual(copied, [tuple(row) for row in original])
        self.assertFalse((self.bulk / "dota_replays.db.copy").exists())
        self.assertFalse((self.copy_dir / "dota_replays.db.part").exists())
        self.assertTrue(any("Copied" in line for line in captured.output))

    def test_an_unwritable_copy_folder_leaves_the_earlier_copy(self):
        if os.geteuid() == 0:
            self.skipTest("root writes into a directory with no write permission")
        with self.assertLogs("db_copy", level="INFO"):
            self.assertEqual(db_copy.main(), 0)
        target = self.copy_dir / "dota_replays.db"
        earlier = target.read_bytes()
        self.database.upsert_player(3, "Third Player")
        self.copy_dir.chmod(0o555)
        self.addCleanup(self.copy_dir.chmod, 0o755)

        with self.assertLogs("db_copy", level="ERROR"):
            self.assertEqual(db_copy.main(), 1)

        self.assertEqual(target.read_bytes(), earlier)
        connection = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            names = [
                row[0]
                for row in connection.execute(
                    "SELECT persona_name FROM players ORDER BY player_id"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(names, ["First Player", "Second Player"])


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
        for script in (UPDATE_ENTRY, VERIFY_ENTRY, SUBMIT_HELPER):
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
