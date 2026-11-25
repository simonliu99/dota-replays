"""Cases that hold dota-replays to the files switchboard records (switchboard#758).

tests/contracts/switchboard holds byte copies of switchboard's recorded
document-store exchanges and of the sha256 lists switchboard keeps for its
copies of tests/golden. A change to the hub's answers or to a golden file fails
here until both repositories copy it again.
"""

import hashlib
import json
import re
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.fake_hub import FakeHub

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "tests" / "contracts" / "switchboard"
GOLDEN = ROOT / "tests" / "golden"
TOKEN = "sbm_test_token"
# The token switchboard's recorder sends as one the hub does not hold.
UNKNOWN_TOKEN = "sbm_aaaaaaaa_" + "a" * 32
COPY_LINE = re.compile(r"^([0-9a-f]{40})  (\S+)  (\S+)$")
DIGEST_LINE = re.compile(r"^([0-9a-f]{64})  (\S.*)$")


def blob_id(data: bytes) -> str:
    """Return the git blob id of a file's bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def digests(name: str) -> dict[str, str]:
    """Return a copied digest list as {path under tests/golden: sha256}."""
    text = (CONTRACTS / name).read_text(encoding="utf-8")
    return {
        match.group(2): match.group(1)
        for match in map(DIGEST_LINE.match, text.splitlines())
        if match
    }


def exchange_body(exchange: dict) -> bytes | None:
    """Return the bytes one exchange sends, as switchboard's recorder builds them."""
    if "body" in exchange:
        return exchange["body"].encode("utf-8")
    if "body_hex" in exchange:
        return bytes.fromhex(exchange["body_hex"])
    if "body_size" in exchange:
        filler = "x" * (exchange["body_size"] - len('{"a":""}'))
        return ('{"a":"' + filler + '"}').encode("utf-8")
    return None


def comparable(answer: str):
    """Return an answer as the fake must match it.

    A status and an error `code` are the contract; a `detail` is the hub's own
    wording (switchboard's testdata/contracts/docs-api/SOURCE). A list's
    `updated_ms` is the recorder's clock, so only its type is kept.
    """
    if not answer:
        return None
    value = json.loads(answer)
    if isinstance(value, dict) and "code" in value:
        return {"code": value["code"]}
    if isinstance(value, dict) and isinstance(value.get("docs"), list):
        for document in value["docs"]:
            document["updated_ms"] = type(document.get("updated_ms")).__name__
    return value


class CopiedContractTestCase(unittest.TestCase):
    def test_each_copy_holds_the_blob_its_source_line_names(self):
        lines = (CONTRACTS / "SOURCE").read_text(encoding="utf-8").splitlines()
        copies = [match.groups() for match in map(COPY_LINE.match, lines) if match]
        self.assertEqual(len(copies), 3, lines)
        for blob, path, source in copies:
            with self.subTest(path=path):
                self.assertEqual(blob_id((CONTRACTS / path).read_bytes()), blob, source)


class DocsContractReplayTestCase(unittest.TestCase):
    """The fake hub answers switchboard's recorded exchanges as the hub did."""

    def setUp(self):
        self.hub = FakeHub(token=TOKEN)
        self.addCleanup(self.hub.close)

    def send(self, exchange: dict) -> tuple[int, str]:
        headers = {}
        if exchange["token"] == "module":
            headers["Authorization"] = f"Bearer {TOKEN}"
        elif exchange["token"] == "unknown":
            headers["Authorization"] = f"Bearer {UNKNOWN_TOKEN}"
        request = urllib.request.Request(
            self.hub.base_url + exchange["path"],
            data=exchange_body(exchange),
            method=exchange["method"],
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def test_the_fake_hub_answers_each_recorded_exchange(self):
        contract = json.loads(
            (CONTRACTS / "docs-api" / "exchanges.json").read_text(encoding="utf-8")
        )
        exchanges = contract["exchanges"]
        # switchboard's TestDocsContract trusts the file at 20 exchanges or more.
        self.assertGreaterEqual(len(exchanges), 20)
        for exchange in exchanges:
            with self.subTest(exchange=exchange["name"]):
                self.assertIn(exchange["token"], ("module", "none", "unknown"))
                status, answer = self.send(exchange)
                self.assertEqual(status, exchange["status"], answer)
                self.assertEqual(comparable(answer), comparable(exchange["answer"]))


class GoldenDigestTestCase(unittest.TestCase):
    """tests/golden is the set of files switchboard copied, unchanged."""

    def test_the_golden_files_match_switchboards_fixture_digests(self):
        listed = digests("golden-fixtures.SOURCE")
        held = {
            path.relative_to(GOLDEN).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in GOLDEN.rglob("*")
            if path.is_file()
        }
        self.assertGreaterEqual(len(listed), 8)
        self.assertEqual(held, listed)

    def test_the_golden_files_match_switchboards_docs_digests(self):
        listed = digests("golden-docs.SOURCE")
        self.assertGreaterEqual(len(listed), 1)
        for path, digest in listed.items():
            with self.subTest(path=path):
                data = (GOLDEN / path).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
