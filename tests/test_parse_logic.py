import json
import tempfile
import time
import unittest
from unittest.mock import MagicMock, call, patch
from datetime import datetime, timedelta
from opendota_client import OpenDotaClient
from database import Database
from dota_replays import DotaReplays
from pathlib import Path
from tests.support import DAY, HOUR, daylight_time_began, daylight_time_ended

class TestParseLogic(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.mock_db = MagicMock(spec=Database)
        self.mock_client = MagicMock(spec=OpenDotaClient)
        self.replay_dir = Path(self.temp_dir.name) / "replays"
        self.app = DotaReplays(
            db=self.mock_db,
            client=self.mock_client,
            replay_dir=self.replay_dir,
            wait_for_parse=True
        )

    def test_fetch_match_details_already_parsed(self):
        """Test that a match already parsed (version > 0) skips request_parse."""
        match_id = 12345
        match_data = {
            "match_id": match_id,
            "start_time": int(datetime.now().timestamp()),
            "version": 21
        }
        
        self.mock_db.get_matches_without_details.return_value = [{"match_id": match_id, "start_time": match_data["start_time"]}]
        self.mock_client.get_match_details.return_value = match_data
        
        self.app._fetch_match_details(player_id=1)
        
        # Should NOT call request_parse
        self.mock_client.request_parse.assert_not_called()
        # Should call upsert_match_details
        self.mock_db.upsert_match_details.assert_called_once_with(match_id, match_data)

    def test_fetch_match_details_unparsed_triggers_request(self):
        """Test that a match with version=None triggers a parse request."""
        match_id = 67890
        unparsed_data = {
            "match_id": match_id,
            "start_time": int(datetime.now().timestamp()),
            "version": None
        }
        parsed_data = {
            "match_id": match_id,
            "start_time": unparsed_data["start_time"],
            "version": 21
        }
        
        self.mock_db.get_matches_without_details.return_value = [{"match_id": match_id, "start_time": unparsed_data["start_time"]}]
        # First call returns unparsed data, second (after parse) returns parsed data
        self.mock_client.get_match_details.side_effect = [unparsed_data, parsed_data]
        self.mock_client.request_parse.return_value = "job_123"
        self.mock_client.poll_parse_completion.return_value = True
        
        self.app._fetch_match_details(player_id=1)
        
        # Should call request_parse
        self.mock_client.request_parse.assert_called_once_with(match_id)
        # Should call poll_parse_completion
        self.mock_client.poll_parse_completion.assert_called_once_with("job_123")
        # Should call upsert_match_details with the FINAL parsed data
        self.mock_db.upsert_match_details.assert_called_once_with(match_id, parsed_data)

    def test_an_unparsed_match_inside_21_days_across_a_spring_forward_change_is_requested(self):
        daylight_time_began(self)
        match_id = 67891
        start_time = int(time.time()) - 21 * DAY + 2 * HOUR
        self.mock_db.get_matches_without_details.return_value = [
            {"match_id": match_id, "start_time": start_time}
        ]
        self.mock_client.get_match_details.return_value = {
            "match_id": match_id,
            "start_time": start_time,
            "version": None,
        }
        self.mock_client.request_parse.return_value = "job_789"
        self.mock_client.poll_parse_completion.return_value = False

        self.app._fetch_match_details(player_id=1)

        self.mock_client.request_parse.assert_called_once_with(match_id)

    def test_an_unparsed_match_past_21_days_across_a_fall_back_change_is_not_requested(self):
        daylight_time_ended(self)
        match_id = 67892
        start_time = int(time.time()) - 21 * DAY - 2 * HOUR
        self.mock_db.get_matches_without_details.return_value = [
            {"match_id": match_id, "start_time": start_time}
        ]
        self.mock_client.get_match_details.return_value = {
            "match_id": match_id,
            "start_time": start_time,
            "version": None,
        }

        self.app._fetch_match_details(player_id=1)

        self.mock_client.request_parse.assert_not_called()
        self.mock_db.upsert_match_details.assert_called_once()

    def unparsed_matches(self, starts: dict) -> None:
        self.mock_db.get_matches_without_details.return_value = [
            {"match_id": match_id, "start_time": start} for match_id, start in starts.items()
        ]
        self.mock_client.get_match_details.side_effect = lambda match_id: {
            "match_id": match_id,
            "start_time": starts[match_id],
            "version": None,
        }
        self.mock_client.request_parse.return_value = None

    def test_only_an_unparsed_match_newer_than_the_cutoff_is_requested(self):
        now = int(time.time())
        cutoff = now - 21 * DAY
        self.unparsed_matches({67893: cutoff - 1, 67894: cutoff, 67895: cutoff + 1})

        with patch("dota_replays._now", return_value=now):
            self.app._fetch_match_details(player_id=1)

        self.assertEqual(self.mock_client.request_parse.call_args_list, [call(67895)])

    def test_the_parse_cutoff_is_taken_once_before_the_details_calls(self):
        clock = [int(time.time())]
        self.unparsed_matches({67896: clock[0] - 21 * DAY + 1})
        details = self.mock_client.get_match_details.side_effect

        def slow_details(match_id):
            # The details call takes a minute, so the match is past 21 days
            # by the time it answers.
            clock[0] += 60
            return details(match_id)

        self.mock_client.get_match_details.side_effect = slow_details

        with patch("dota_replays._now", side_effect=lambda: clock[0]):
            self.app._fetch_match_details(player_id=1)

        self.mock_client.request_parse.assert_called_once_with(67896)

    def test_recheck_unparsed_matches(self):
        """Test that recheck_unparsed_matches calls the right DB method and triggers parse."""
        match_id = 11111
        start_time = int(datetime.now().timestamp())
        self.mock_db.get_unparsed_matches.return_value = [{"match_id": match_id, "start_time": start_time}]
        self.mock_client.request_parse.return_value = "job_456"
        self.mock_client.poll_parse_completion.return_value = True
        self.mock_client.get_match_details.return_value = {"match_id": match_id, "version": 21}
        
        self.app.recheck_unparsed_matches()
        
        self.mock_db.get_unparsed_matches.assert_called_once()
        self.mock_client.request_parse.assert_called_once_with(match_id)
        self.mock_db.upsert_match_details.assert_called_once()

if __name__ == "__main__":
    unittest.main()
