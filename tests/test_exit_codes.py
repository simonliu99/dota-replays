"""Exit codes for a run whose fetches failed in whole or in part."""

import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dota_replays
import opendota_client
from database import Database
from tests.support import FIXTURE_PATH, RecordedOpenDota, ReplayServer, load_fixture

PLAYER_A = 101
PLAYER_B = 202
MATCH_IDS = (111, 222, 333)
EXPIRED_MATCH = 444


def profile(player_id: int) -> dict:
    """Return a player answer shaped as OpenDota's."""
    return {"profile": {"account_id": player_id, "personaname": f"Player {player_id}"}}


def match(match_id: int) -> dict:
    """Return a match list entry shaped as OpenDota's."""
    return {
        "match_id": match_id,
        "start_time": int(time.time()),
        "duration": 1200,
        "hero_id": 1,
        "kills": 0,
        "deaths": 0,
        "assists": 0,
        "radiant_win": True,
        "player_slot": 0,
    }


class FakeClient:
    """An OpenDotaClient stand-in answering only what the test records.

    A player, match list or match details the test left out is answered None,
    which is what the real client answers for a failed fetch.
    """

    def __init__(
        self,
        players: dict,
        matches: dict | None = None,
        details: dict | None = None,
    ):
        self.players = players
        self.matches = matches or {}
        self.details = details or {}
        self.player_calls = []
        self.detail_calls = []

    def get_player(self, player_id: int) -> dict | None:
        self.player_calls.append(player_id)
        return self.players.get(player_id)

    def get_player_matches(
        self, player_id: int, limit: int | None = None
    ) -> list[dict] | None:
        return self.matches.get(player_id)

    def get_match_details(self, match_id: int) -> dict | None:
        self.detail_calls.append(match_id)
        return self.details.get(match_id)

    def request_parse(self, match_id: int) -> int | None:
        return None


class ExitCodeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "dota_replays.db"
        self.replay_dir = self.root / "replays"

    def database(self) -> Database:
        db = Database(str(self.db_path))
        self.addCleanup(db.close)
        return db

    def seed_players(self, player_ids: list[int]) -> Database:
        db = self.database()
        for player_id in player_ids:
            db.upsert_player(player_id, f"Player {player_id}")
        return db

    def run_main(self, *argv: str, client=None) -> tuple[int, list[str]]:
        """Run the CLI with argv against client; return its exit code and log."""
        client = client if client is not None else FakeClient({})
        arguments = ["--db", str(self.db_path), "--replay-dir", str(self.replay_dir)]
        with mock.patch.object(sys, "argv", ["dota_replays.py", *arguments, *argv]):
            with mock.patch.object(dota_replays, "OpenDotaClient", lambda **_: client):
                with self.assertLogs("dota_replays", level="INFO") as captured:
                    with contextlib.redirect_stdout(io.StringIO()):
                        try:
                            dota_replays.main()
                            code = 0
                        except SystemExit as exit_status:
                            code = exit_status.code or 0
        return code, list(captured.output)

    def recorded(self) -> tuple[RecordedOpenDota, int]:
        """Start the recorded OpenDota fake and return it with its player id."""
        fixture = load_fixture()
        server = RecordedOpenDota(FIXTURE_PATH)
        self.addCleanup(server.close)
        return server, fixture["player_id"]

    def test_a_player_that_answers_nothing_fails_the_run(self):
        self.seed_players([PLAYER_A, PLAYER_B])
        client = FakeClient(
            players={PLAYER_A: profile(PLAYER_A), PLAYER_B: None},
            matches={PLAYER_A: [], PLAYER_B: []},
        )

        code, output = self.run_main(client=client)

        self.assertEqual(code, 1)
        self.assertEqual(sorted(client.player_calls), [PLAYER_A, PLAYER_B])
        self.assertIn("Player 202 not found", "\n".join(output))

    def test_two_players_that_answer_exit_zero(self):
        self.seed_players([PLAYER_A, PLAYER_B])
        client = FakeClient(
            players={PLAYER_A: profile(PLAYER_A), PLAYER_B: profile(PLAYER_B)},
            matches={PLAYER_A: [], PLAYER_B: []},
        )

        code, _ = self.run_main(client=client)

        self.assertEqual(code, 0)
        self.assertEqual(sorted(client.player_calls), [PLAYER_A, PLAYER_B])

    def test_a_server_error_on_the_match_list_fails_the_run(self):
        server, player_id = self.recorded()
        server.fail_next(f"/api/players/{player_id}/matches", [500] * 4)
        self.seed_players([player_id])
        client = opendota_client.OpenDotaClient(base_url=f"{server.base_url}/api")

        with mock.patch.object(opendota_client.time, "sleep"):
            code, output = self.run_main(str(player_id), client=client)

        self.assertEqual(code, 1)
        self.assertIn(
            "Failed to fetch the match list for player", "\n".join(output)
        )

    def test_an_empty_match_list_exits_zero(self):
        server, player_id = self.recorded()
        server.responses[f"/api/players/{player_id}/matches"] = []
        self.seed_players([player_id])
        client = opendota_client.OpenDotaClient(base_url=f"{server.base_url}/api")

        code, output = self.run_main(str(player_id), client=client)

        self.assertEqual(code, 0)
        self.assertIn("No matches found", "\n".join(output))

    def test_a_failed_detail_fetch_is_counted_and_the_rest_are_stored(self):
        self.seed_players([PLAYER_A])
        client = FakeClient(
            players={PLAYER_A: profile(PLAYER_A)},
            matches={PLAYER_A: [match(match_id) for match_id in MATCH_IDS]},
            details={
                MATCH_IDS[0]: {"match_id": MATCH_IDS[0], "version": 21},
                MATCH_IDS[2]: {"match_id": MATCH_IDS[2], "version": 21},
            },
        )

        code, output = self.run_main(client=client)

        self.assertEqual(code, 1)
        self.assertEqual(sorted(client.detail_calls), list(MATCH_IDS))
        db = self.database()
        self.assertEqual(db.get_match_details(MATCH_IDS[0]), {"match_id": MATCH_IDS[0], "version": 21})
        self.assertIsNone(db.get_match_details(MATCH_IDS[1]))
        self.assertEqual(db.get_match_details(MATCH_IDS[2]), {"match_id": MATCH_IDS[2], "version": 21})
        self.assertIn("1 failed match detail fetch", output[-1])

    def test_a_replay_answering_not_found_exits_zero(self):
        server = ReplayServer(b"", status=404)
        self.addCleanup(server.close)
        db = self.seed_players([PLAYER_A])
        db.insert_matches(PLAYER_A, [match(EXPIRED_MATCH)])
        db.upsert_match_details(
            EXPIRED_MATCH,
            {
                "match_id": EXPIRED_MATCH,
                "version": 21,
                "replay_url": f"{server.base_url}/570/{EXPIRED_MATCH}.dem.bz2",
            },
        )
        client = FakeClient(
            players={PLAYER_A: profile(PLAYER_A)},
            matches={PLAYER_A: [match(EXPIRED_MATCH)]},
        )

        code, output = self.run_main(client=client)

        self.assertEqual(code, 0)
        self.assertIn("Downloaded 0 replays, 1 failed", "\n".join(output))
        self.assertEqual(len(server.requests), 1)

    def test_a_body_cut_short_counts_as_a_failed_fetch(self):
        server, player_id = self.recorded()
        server.truncate_next(f"/api/players/{player_id}")
        self.seed_players([player_id])
        client = opendota_client.OpenDotaClient(base_url=f"{server.base_url}/api")

        code, output = self.run_main(str(player_id), client=client)

        log = "\n".join(output)
        self.assertEqual(code, 1)
        self.assertIn(f"Player {player_id} not found", log)

    def test_a_truncated_player_answer_logs_its_error(self):
        server, player_id = self.recorded()
        server.truncate_next(f"/api/players/{player_id}")
        client = opendota_client.OpenDotaClient(base_url=f"{server.base_url}/api")

        with self.assertLogs("opendota_client", level="ERROR") as client_log:
            player = client.get_player(player_id)

        self.assertIsNone(player)
        self.assertIn("IncompleteRead", "\n".join(client_log.output))


if __name__ == "__main__":
    unittest.main()
