"""Cases for scripts/sequence, the repository's one copy of the suite run.

Each case builds a small repository that holds the script, the progress helper
and the repository's .gitignore, commits it, and runs the script by its path so
the shebang's interpreter discovery is the thing under test. Every run builds
its environment from nothing, so a record or a variable of the outer job never
reaches a fixture.
"""

import hashlib
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEQUENCE = ROOT / "scripts" / "sequence"
HELPER = ROOT / "scripts" / "lib" / "run_queue_progress.py"
GIT = shutil.which("git")

PASSING = """\
import unittest


class Fixture(unittest.TestCase):
    def test_passes(self):
        pass

    @unittest.skip("the fixture skips this one")
    def test_skipped(self):
        pass
"""

FAILING = """\
import unittest


class Fixture(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2)

    def test_errors(self):
        raise RuntimeError("the fixture raises")
"""

HANGING = """\
import threading
import unittest


class Fixture(unittest.TestCase):
    def test_hangs(self):
        # Bounded, so a run whose timer never fires still ends by itself.
        threading.Event().wait(120)
"""

NESTED_FAILURE = '''\
import unittest

NESTED = (
    "\\nSlowest test durations\\n"
    + "-" * 70
    + "\\n0.001s     test_inner (inner.Inner.test_inner)\\n\\n"
    + "-" * 70
    + "\\nRan 5 tests in 0.001s\\n\\nOK (skipped=4)\\n"
)


class Fixture(unittest.TestCase):
    def test_fails_with_nested_output(self):
        self.fail(NESTED)
'''


def tokens(line):
    """Return a record's key=value tokens; detail= and verdict= end the line."""
    fields = {}
    for token in line.split():
        if token.startswith("detail=") or token.startswith("verdict="):
            break
        key, _, value = token.partition("=")
        if key and value:
            fields[key] = value
    return fields


class SequenceTestCase(unittest.TestCase):
    """A fixture repository and the runs scripts/sequence makes in it."""

    HANG_GUARD = 60

    def setUp(self):
        if not GIT:
            self.fail("git is not on PATH")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.make_bin(("python3.13",))
        self.progress = self.root / "progress"
        self.progress.write_text("", encoding="utf-8")
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def make_bin(self, names):
        """Return a new directory holding git and the named interpreters."""
        self.binaries = getattr(self, "binaries", 0) + 1
        directory = self.root / ("bin-%d" % self.binaries)
        directory.mkdir()
        (directory / "git").symlink_to(GIT)
        for name in names:
            (directory / name).symlink_to(sys.executable)
        return directory

    def env(self, bin_dir):
        """Return an environment built from nothing for one run."""
        return {
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": str(bin_dir),
            "RUN_QUEUE_PROGRESS": str(self.progress),
        }

    def git(self, *arguments):
        """Run git in the fixture repository."""
        return subprocess.run(
            [str(self.bin / "git")] + list(arguments),
            cwd=str(self.repo),
            env=self.env(self.bin),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=self.HANG_GUARD,
        )

    def build(self, module=None):
        """Commit a repository holding the script, the helper and one test module."""
        (self.repo / "scripts" / "lib").mkdir(parents=True)
        shutil.copy2(SEQUENCE, self.repo / "scripts" / "sequence")
        shutil.copy2(HELPER, self.repo / "scripts" / "lib" / "run_queue_progress.py")
        shutil.copy2(ROOT / ".gitignore", self.repo / ".gitignore")
        (self.repo / "tests").mkdir()
        shutil.copy2(ROOT / "tests" / "__init__.py", self.repo / "tests" / "__init__.py")
        if module is not None:
            (self.repo / "tests" / "test_fixture.py").write_text(module, encoding="utf-8")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "-q", "-m", "fixture")
        return self.repo

    def start(self, arguments, bin_dir=None):
        """Start the script in its own process group."""
        return subprocess.Popen(
            [str(self.repo / "scripts" / "sequence")] + list(arguments),
            cwd=str(self.repo),
            env=self.env(bin_dir or self.bin),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def finish(self, process):
        """Wait for the script, and end its process group at the hang guard."""
        try:
            output, _ = process.communicate(timeout=self.HANG_GUARD)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail("scripts/sequence did not finish within %d s" % self.HANG_GUARD)
        return output

    def code(self, arguments, bin_dir=None):
        """Run the script and return its completed process."""
        process = self.start(arguments, bin_dir)
        output = self.finish(process)
        return process.returncode, output

    def summary_lines(self, run_dir):
        """Return the run's summary lines."""
        return (run_dir / "summary.txt").read_text(encoding="utf-8").splitlines()

    def records(self):
        """Return the progress records this case's runs wrote."""
        return [line for line in self.progress.read_text(encoding="utf-8").splitlines()
                if line.startswith("step=unittest ")]

    def test_a_passing_suite_exits_zero_and_names_the_tree(self):
        self.build(PASSING)
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir)])
        self.assertEqual(code, 0, output)
        head = self.git("rev-parse", "HEAD").stdout.strip()
        tree = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        lines = self.summary_lines(run_dir)
        self.assertEqual(lines[0], "revision %s tree %s" % (head, tree))
        step = [line for line in lines if line.startswith("step unittest ")]
        self.assertEqual(len(step), 1, lines)
        fields = tokens(step[0])
        self.assertEqual(fields["rc"], "0")
        self.assertEqual(fields["ran"], "2")
        self.assertEqual(fields["skipped"], "1")
        self.assertEqual(lines[-1], "result PASS")
        records = self.records()
        self.assertEqual([tokens(line)["state"] for line in records], ["pending", "run", "ok"])
        last = tokens(records[-1])
        self.assertEqual((last["done"], last["total"], last["failing"]), ("2", "2", "0"))

    def test_a_failing_suite_exits_one(self):
        self.build(FAILING)
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir)])
        self.assertEqual(code, 1, output)
        step = [line for line in self.summary_lines(run_dir) if line.startswith("step unittest ")]
        self.assertEqual(len(step), 1)
        fields = tokens(step[0])
        self.assertEqual((fields["failures"], fields["errors"]), ("1", "1"))
        self.assertEqual(self.summary_lines(run_dir)[-1], "result FAIL unittest")
        records = self.records()
        self.assertEqual(tokens(records[-1])["state"], "fail")
        self.assertEqual(tokens(records[-1])["failing"], "2")

    def test_the_summary_reads_the_suites_own_verdict_and_durations(self):
        self.build(NESTED_FAILURE)
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir)])
        self.assertEqual(code, 1, output)
        lines = self.summary_lines(run_dir)
        step = [line for line in lines if line.startswith("step unittest ")]
        self.assertEqual(len(step), 1, lines)
        fields = tokens(step[0])
        self.assertEqual(
            (fields["ran"], fields["failures"], fields["errors"], fields["skipped"]),
            ("1", "1", "0", "0"),
            step[0],
        )
        slowest = [line for line in lines if line.startswith("slowest ")]
        self.assertEqual(len(slowest), 1, lines)
        self.assertIn("test_fails_with_nested_output", slowest[0])

    def test_no_tests_fails(self):
        self.build()
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir)])
        self.assertEqual(code, 1, output)
        self.assertEqual(self.summary_lines(run_dir)[-1], "result FAIL unittest")

    def test_the_bound_stops_a_hung_suite(self):
        self.build(HANGING)
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir), "--bound", "1"])
        self.assertEqual(code, 1, output)
        self.assertEqual(self.summary_lines(run_dir)[-1], "result FAIL timeout")
        last = self.records()[-1]
        self.assertEqual(tokens(last)["state"], "fail")
        self.assertTrue(last.endswith("detail=timed out after 1 s"), last)

    def test_a_term_settles_the_step_and_dies_by_the_signal(self):
        self.build(HANGING)
        run_dir = self.root / "run"
        process = subprocess.Popen(
            [str(self.repo / "scripts" / "sequence"), "--run", str(run_dir), "--bound", "60"],
            cwd=str(self.repo),
            env=self.env(self.bin),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if "step=unittest state=run" in self.progress.read_text(encoding="utf-8"):
                    break
                time.sleep(0.05)
            else:
                self.fail("scripts/sequence never recorded its running step")
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=self.HANG_GUARD)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        self.assertEqual(process.returncode, -signal.SIGTERM)
        last = self.records()[-1]
        self.assertEqual(tokens(last)["state"], "fail")
        self.assertTrue(last.endswith("detail=stopped: signal TERM"), last)
        self.assertFalse([line for line in self.summary_lines(run_dir)
                          if line.startswith("result")])

    def test_a_dirty_tree_is_refused(self):
        self.build(PASSING)
        (self.repo / "untracked.txt").write_text("not committed\n", encoding="utf-8")
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir)])
        self.assertEqual(code, 2, output)
        self.assertFalse((run_dir / "summary.txt").exists())
        self.assertEqual(self.progress.read_text(encoding="utf-8"), "")

    def test_a_nonempty_run_directory_is_refused(self):
        self.build(PASSING)
        run_dir = self.root / "run"
        run_dir.mkdir()
        note = run_dir / "notes.txt"
        note.write_text("keep me\n", encoding="utf-8")
        code, output = self.code(["--run", str(run_dir)])
        self.assertEqual(code, 2, output)
        self.assertEqual(note.read_text(encoding="utf-8"), "keep me\n")
        self.assertFalse((run_dir / "summary.txt").exists())

    def test_a_python3_only_path_runs(self):
        self.build(PASSING)
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir)], self.make_bin(("python3",)))
        self.assertEqual(code, 0, output)
        python = [line for line in self.summary_lines(run_dir) if line.startswith("python ")]
        self.assertEqual(len(python), 1)
        parts = python[0].split(None, 2)
        self.assertEqual(parts[2], platform.python_version(), python[0])
        self.assertTrue(os.path.samefile(parts[1], sys.executable), python[0])
        self.assertEqual(self.summary_lines(run_dir)[-1], "result PASS")

    def test_python313_is_preferred_over_python3(self):
        self.build(PASSING)
        both = self.make_bin(("python3.13",))
        decoy = both / "python3"
        decoy.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in -c) printf '3.12.0\\n'; exit 0 ;; esac\n"
            "exit 3\n",
            encoding="utf-8",
        )
        decoy.chmod(0o755)
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir)], both)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.summary_lines(run_dir)[-1], "result PASS")

    def test_without_a_python312_or_newer_the_job_refuses(self):
        self.build(PASSING)
        run_dir = self.root / "run"
        code, output = self.code(["--run", str(run_dir)], self.make_bin(()))
        self.assertEqual(code, 2, output)
        self.assertIn("no Python 3.12 or newer", output)
        self.assertFalse((run_dir / "summary.txt").exists())
        old = self.make_bin(())
        stub = old / "python3"
        stub.write_text("#!/bin/sh\nprintf '3.9.6\\n'\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        code, output = self.code(["--run", str(run_dir)], old)
        self.assertEqual(code, 2, output)
        self.assertIn("python3 3.9.6", output)

    def test_the_progress_helper_is_agent_workflows_version_1(self):
        data = HELPER.read_bytes()
        blob = b"blob %d\0" % len(data)
        self.assertEqual(
            hashlib.sha1(blob + data).hexdigest(),
            "fc3b11822873bb96af02f2108429599baf3997bf",
        )


if __name__ == "__main__":
    unittest.main()
