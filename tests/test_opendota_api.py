"""Cases that run the app against the recorded OpenDota answers."""

import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import opendota_client
from database import Database
from dota_replays import DotaReplays
from tests.support import (
    FIXTURE_PATH,
    PARSE_FIXTURE_PATH,
    RecordedOpenDota,
    WorkingDirectory,
    closed_port,
    load_fixture,
)


class StalledAnswer:
    """A response whose headers arrived and whose body never finishes."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        raise TimeoutError("the read timed out")


class FakeOpenDotaTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = load_fixture()
        self.player_id = self.fixture["player_id"]
        self.match_id = self.fixture["match_ids"][0]
        self.server = RecordedOpenDota(FIXTURE_PATH)
        self.addCleanup(self.server.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def player_path(self) -> str:
        return f"/api/players/{self.player_id}"

    def matches_path(self) -> str:
        return f"/api/players/{self.player_id}/matches"

    def match_path(self) -> str:
        return f"/api/matches/{self.match_id}"

    def client(self) -> opendota_client.OpenDotaClient:
        return opendota_client.OpenDotaClient(base_url=f"{self.server.base_url}/api")

    def test_match_list_and_details_are_stored(self):
        db = Database(str(Path(self.temp.name) / "dota_replays.db"))
        self.addCleanup(db.close)
        app = DotaReplays(
            db=db,
            client=self.client(),
            replay_dir=Path(self.temp.name) / "replays",
            wait_for_parse=False,
        )
        run_dir = Path(self.temp.name) / "run"
        run_dir.mkdir()

        with WorkingDirectory(run_dir):
            app.update_player(self.player_id, limit=1)

        self.assertEqual(os.listdir(run_dir), [])
        players = db.get_all_players()
        self.assertEqual([player["player_id"] for player in players], [self.player_id])
        self.assertEqual(
            players[0]["persona_name"],
            self.fixture["responses"][0]["body"]["profile"]["personaname"],
        )
        self.assertEqual(db.get_match_ids_for_player(self.player_id), {self.match_id})
        recorded_details = next(
            entry["body"]
            for entry in self.fixture["responses"]
            if entry["path"] == self.match_path()
        )
        self.assertEqual(db.get_match_details(self.match_id), recorded_details)
        self.assertEqual(
            self.server.requests,
            [
                ("GET", self.player_path()),
                ("GET", self.matches_path()),
                ("GET", self.match_path()),
            ],
        )

    def test_a_read_timeout_answers_a_failed_fetch(self):
        client = self.client()
        with mock.patch.object(
            opendota_client.urllib.request, "urlopen", return_value=StalledAnswer()
        ):
            self.assertIsNone(client.get_player(self.player_id))
            self.assertIsNone(client.get_player_matches(self.player_id))

    def test_a_refused_connection_answers_a_failed_fetch(self):
        client = opendota_client.OpenDotaClient(
            base_url=f"http://127.0.0.1:{closed_port()}/api"
        )
        with mock.patch.object(opendota_client.time, "sleep"):
            self.assertIsNone(client.get_player(self.player_id))

    def test_every_request_sets_a_thirty_second_timeout(self):
        client = self.client()
        with mock.patch.object(
            opendota_client.urllib.request, "urlopen", wraps=urllib.request.urlopen
        ) as opener:
            self.assertIsNotNone(client.get_player(self.player_id))

        self.assertEqual(opener.call_args.kwargs.get("timeout"), 30)

    def test_rate_limited_answer_is_retried_once(self):
        self.server.fail_next(self.player_path(), [429])
        client = self.client()
        with mock.patch.object(opendota_client.time, "sleep") as sleep:
            player = client.get_player(self.player_id)

        self.assertIsNotNone(player)
        self.assertEqual(
            [method for method, path in self.server.requests if path == self.player_path()],
            ["GET", "GET"],
        )
        delays = [call.args[0] for call in sleep.call_args_list if call.args]
        self.assertIn(1, delays)
        self.assertNotIn(60, delays)

    def test_a_rate_limit_after_the_wait_raises_rate_limited(self):
        self.server.fail_next(self.player_path(), [429] * 8)
        client = self.client()
        with mock.patch.object(opendota_client.time, "sleep") as sleep:
            with self.assertRaises(opendota_client.RateLimited):
                client.get_player(self.player_id)

        self.assertEqual(
            [method for method, path in self.server.requests if path == self.player_path()],
            ["GET"] * 8,
        )
        delays = [call.args[0] for call in sleep.call_args_list if call.args]
        self.assertEqual(delays.count(60), 1)
        self.assertEqual(client.failed_requests, 1)

    def test_an_answer_of_the_wrong_json_type_is_a_failed_fetch(self):
        self.server.responses[self.player_path()] = ["not", "a", "profile"]
        self.server.answer_raw(self.matches_path(), b"{}", "application/json")
        client = self.client()

        self.assertIsNone(client.get_player(self.player_id))
        self.assertIsNone(client.get_player_matches(self.player_id))
        self.assertEqual(client.failed_requests, 2)

    def test_a_rate_limit_the_wait_clears_answers_the_fetch(self):
        self.server.fail_next(self.player_path(), [429] * 4)
        client = self.client()
        with mock.patch.object(opendota_client.time, "sleep") as sleep:
            player = client.get_player(self.player_id)

        self.assertIsNotNone(player)
        self.assertEqual(
            [method for method, path in self.server.requests if path == self.player_path()],
            ["GET"] * 5,
        )
        delays = [call.args[0] for call in sleep.call_args_list if call.args]
        self.assertEqual(delays.count(60), 1)
        self.assertEqual(client.failed_requests, 0)

    def test_the_match_list_asks_for_its_limit(self):
        client = self.client()

        self.assertIsNotNone(client.get_player_matches(self.player_id, limit=100))

        self.assertEqual(
            [target for _, target, _ in self.server.targets],
            [f"{self.matches_path()}?limit=100"],
        )

    def test_repeated_server_errors_fail_the_fetch(self):
        self.server.fail_next(self.match_path(), [500, 500, 500, 500])
        client = self.client()
        with mock.patch.object(opendota_client.time, "sleep") as sleep:
            details = client.get_match_details(self.match_id)

        self.assertIsNone(details)
        self.assertEqual(
            [method for method, path in self.server.requests if path == self.match_path()],
            ["GET", "GET", "GET", "GET"],
        )
        delays = [call.args[0] for call in sleep.call_args_list if call.args]
        self.assertIn(1, delays)
        self.assertIn(2, delays)
        self.assertIn(4, delays)


class ParsePathTestCase(unittest.TestCase):
    """The parse request and its polls, against the recorded parse answers."""

    def setUp(self):
        self.server = RecordedOpenDota(FIXTURE_PATH)
        self.addCleanup(self.server.close)
        self.parse = load_fixture(PARSE_FIXTURE_PATH)
        self.match_id = self.parse["match_id"]
        self.job_id = self.parse["job_id"]
        self.polls = [
            entry["body"] for entry in self.parse["responses"] if entry["method"] == "GET"
        ]

    def client(self) -> opendota_client.OpenDotaClient:
        return opendota_client.OpenDotaClient(base_url=f"{self.server.base_url}/api")

    def test_a_parse_request_answers_the_recorded_job_id_and_waits_ten_seconds(self):
        client = self.client()
        with mock.patch.object(opendota_client.time, "sleep") as sleep:
            job_id = client.request_parse(self.match_id)

        self.assertEqual(job_id, self.job_id)
        self.assertEqual(self.server.requests, [("POST", f"/api/request/{self.match_id}")])
        delays = [call.args[0] for call in sleep.call_args_list if call.args]
        self.assertIn(10.0, delays)
        self.assertEqual(client.failed_requests, 0)

    def test_the_poll_waits_until_the_recorded_job_is_gone(self):
        # tests/record-opendota --parse polls until OpenDota answers null.
        polls = self.polls.index(None) + 1
        client = self.client()
        with mock.patch.object(opendota_client.time, "sleep") as sleep:
            done = client.poll_parse_completion(self.job_id, timeout=300, poll_interval=20)

        self.assertTrue(done)
        self.assertEqual(
            self.server.requests, [("GET", f"/api/request/{self.job_id}")] * polls
        )
        delays = [call.args[0] for call in sleep.call_args_list if call.args]
        self.assertEqual(delays.count(20), polls - 1)


if __name__ == "__main__":
    unittest.main()
