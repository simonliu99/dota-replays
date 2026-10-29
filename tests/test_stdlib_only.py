"""The application and its tests run on the Python standard library alone."""

import importlib
import sys
import unittest

THIRD_PARTY = ("requests", "tqdm", "dotenv", "wget")
APP_MODULES = ("database", "opendota_client", "dota_replays", "migrate_pkl_to_sqlite")


class TestStandardLibraryOnly(unittest.TestCase):
    def test_app_modules_load_no_third_party_package(self):
        for name in APP_MODULES:
            importlib.import_module(name)

        loaded = [name for name in THIRD_PARTY if name in sys.modules]
        self.assertEqual(loaded, [])


if __name__ == "__main__":
    unittest.main()
