"""The CLI takes no OpenDota API key."""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

import dota_replays


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


if __name__ == "__main__":
    unittest.main()
