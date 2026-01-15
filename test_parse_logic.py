import json
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from opendota_client import OpenDotaClient
from database import Database
from dota_replays import DotaReplays
from pathlib import Path

class TestParseLogic(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock(spec=Database)
        self.mock_client = MagicMock(spec=OpenDotaClient)
        self.replay_dir = Path("./test_replays")
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
