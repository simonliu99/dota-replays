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
    RecordedOpenDota,
    WorkingDirectory,
    load_fixture,
)


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
        # The recorded details carry the real Valve replay URL; the download
        # paths are tested against a local fake instead.
        app._download_replays = lambda force_retry=False: None
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


if __name__ == "__main__":
    unittest.main()
