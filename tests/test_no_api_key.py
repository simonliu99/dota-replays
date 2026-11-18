"""The CLI takes no OpenDota API key."""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

import dota_replays
import opendota_client
from tests.support import RecordedOpenDota, load_fixture


class NoApiKeyTestCase(unittest.TestCase):
    def test_the_cli_rejects_the_api_key_option(self):
        with mock.patch.object(sys, "argv", ["dota_replays.py", "--api-key", "a-key"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    dota_replays.parse_args()
        self.assertEqual(raised.exception.code, 2)

    def test_the_environment_variable_takes_no_effect(self):
        with mock.patch.object(sys, "argv", ["dota_replays.py"]):
            with mock.patch.dict(os.environ, {"OPENDOTA_API_KEY": "a-key"}):
                arguments = dota_replays.parse_args()
        self.assertNotIn("api_key", vars(arguments))

    def test_a_request_carries_no_api_key(self):
        server = RecordedOpenDota()
        self.addCleanup(server.close)
        with mock.patch.dict(os.environ, {"OPENDOTA_API_KEY": "a-key"}):
            client = opendota_client.OpenDotaClient(base_url=f"{server.base_url}/api")
            self.assertIsNotNone(client.get_player(load_fixture()["player_id"]))

        self.assertEqual(len(server.targets), 1)
        _, target, headers = server.targets[0]
        self.assertNotIn("api_key", target)
        self.assertNotIn("a-key", target)
        for name, value in headers.items():
            self.assertNotIn("api_key", name.lower())
            self.assertNotIn("a-key", value)


if __name__ == "__main__":
    unittest.main()
