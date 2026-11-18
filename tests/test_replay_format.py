"""Cases for the replay format table and its helpers."""

import tempfile
import unittest
from pathlib import Path

import replay_format

BZIP2_MAGIC = b"BZh"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class ReplayFormatTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name: str, body: bytes) -> Path:
        path = self.root / name
        path.write_bytes(body)
        return path

    def test_suffix_for_names_the_format_a_head_starts_with(self):
        self.assertEqual(replay_format.suffix_for(BZIP2_MAGIC + b"9blocks"), ".dem.bz2")
        self.assertEqual(replay_format.suffix_for(ZSTD_MAGIC + b"\x00"), ".dem.zst")
        self.assertIsNone(replay_format.suffix_for(b"PBDEMS2\x00"))
        self.assertIsNone(replay_format.suffix_for(b""))

    def test_replay_suffix_names_the_stored_format_of_a_name(self):
        self.assertEqual(replay_format.replay_suffix("123_456.dem.bz2"), ".dem.bz2")
        self.assertEqual(replay_format.replay_suffix("123_456.dem.zst"), ".dem.zst")
        self.assertIsNone(replay_format.replay_suffix("123_456.dem"))
        self.assertIsNone(replay_format.replay_suffix("123_456.dem.bz2.part"))

    def test_replay_stem_drops_the_stored_format_suffix_only(self):
        self.assertEqual(replay_format.replay_stem("123_456.dem.bz2"), "123_456")
        self.assertEqual(replay_format.replay_stem("123_456.dem.zst"), "123_456")
        self.assertEqual(replay_format.replay_stem("123_456.dem"), "123_456.dem")
        self.assertEqual(
            replay_format.replay_stem("123_456.dem.bz2.part"), "123_456.dem.bz2.part"
        )
        self.assertEqual(
            replay_format.replay_stem("8793153210_1613536125.dem.bz2") + ".dem.zst",
            "8793153210_1613536125.dem.zst",
        )

    def test_read_head_returns_the_first_four_bytes(self):
        path = self.write("short.dem.zst", ZSTD_MAGIC + b"payload")
        self.assertEqual(replay_format.read_head(path), ZSTD_MAGIC)

        three = self.write("three.dem.bz2", b"BZh")
        self.assertEqual(replay_format.read_head(three), b"BZh")

        empty = self.write("empty.dem.bz2", b"")
        self.assertEqual(replay_format.read_head(empty), b"")

        self.assertEqual(replay_format.read_head(self.root / "missing.dem.bz2"), b"")


if __name__ == "__main__":
    unittest.main()
