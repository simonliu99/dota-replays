"""run_queue_progress, version 1: the shared progress helper for Python 3.8 and
later (simonliu99/agent-workflow #440). An adopting repository copies this file
byte for byte into its own scripts/lib/ and never edits its copy. It writes the
same records as scripts/lib/run-queue-progress.sh.

    plan(*names)                 a pending record per step, before any step runs
    step(name, argv, log=None)   a run record, the command, then ok or fail with
                                 secs=; returns the command's status (128 + N
                                 when signal N ended it)
    record(step, state, done=None, total=None, failing=None, secs=None,
           log=None, detail=None)
    settle(reason, names=None)   each named step (default: every step this
                                 process recorded) whose last record is pending
                                 gets `skip detail=not run: <reason>`, and one
                                 whose last record is run gets `fail
                                 detail=stopped: <reason>`
    install()                    settle with `exit` at exit and `signal <name>`
                                 on TERM, INT, HUP and ALRM, then die by that
                                 signal

Records go straight to $RUN_QUEUE_PROGRESS in the bytes `run-queue progress`
writes (docs/agents.md, Job progress); without it the arguments are checked and
nothing is written. Bad arguments raise ValueError; a write that fails prints
one line and returns, so progress never fails a job.
"""

import atexit
import os
import re
import signal
import subprocess
import sys
import time

_NAME = re.compile(r"[A-Za-z0-9._:/+-]{1,80}\Z")
_STATES = ("run", "ok", "fail", "skip", "pending")
_NUMBERS = ("done", "total", "failing", "secs")
_SIGNALS = ("TERM", "INT", "HUP", "ALRM")
_names = []
_installed = False


def _control(char):
    return ord(char) < 32 or ord(char) == 127


def _check_name(name):
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError("step must match [A-Za-z0-9._:/+-]{1,80}: %s" % (name,))


def _line(step, state, fields, at):
    _check_name(step)
    if state not in _STATES:
        raise ValueError("state must be run, ok, fail, skip or pending: %s" % (state,))
    nums = ""
    log = ""
    detail = ""
    for key, value in fields.items():
        if value is None:
            continue
        if key in _NUMBERS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 \
                    or len(str(value)) > 12:
                raise ValueError("%s must be a whole number: %s" % (key, value))
            nums += " %s=%d" % (key, value)
        elif key == "log":
            if not isinstance(value, str) or not value.startswith("/"):
                raise ValueError("log must be an absolute path: %s" % (value,))
            if len(value.encode("utf-8", "surrogateescape")) > 1024:
                raise ValueError("log is longer than 1024 bytes")
            # A path a record cannot carry as one token is left out.
            if not any(c in "  " or _control(c) for c in value):
                log = " log=" + value
        elif key == "detail":
            text = "".join(" " if _control(c) else c for c in str(value))
            if text:
                detail = " detail=" + text
        else:
            raise ValueError("unknown key %s (known: step state done total failing secs log detail)"
                             % (key,))
    return "step=%s state=%s%s at=%d%s%s\n" % (step, state, nums, at, log, detail)


def _write(lines):
    path = os.environ.get("RUN_QUEUE_PROGRESS", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8", errors="surrogateescape") as handle:
            handle.write("".join(lines))
    except OSError:
        sys.stderr.write("run-queue-progress: cannot append to %s\n" % path)


def _remember(name):
    if name not in _names:
        _names.append(name)


def record(step, state, **fields):
    """Write one record; see the module text for its fields."""
    line = _line(step, state, fields, int(time.time()))
    _remember(step)
    _write([line])


def plan(*names):
    """Declare the job's steps: one pending record each."""
    if not names:
        raise ValueError("plan needs a step name")
    at = int(time.time())
    lines = [_line(name, "pending", {}, at) for name in names]
    for name in names:
        _remember(name)
    for line in lines:
        _write([line])


def step(name, argv, log=None):
    """Run argv as one step and return its status."""
    record(name, "run", log=log)
    start = int(time.time())
    env = dict(os.environ)
    path = env.get("RUN_QUEUE_PROGRESS", "")
    if path:
        env["RUN_QUEUE_CASE_FILE"] = path
        env["RUN_QUEUE_CASE_STEP"] = name
    try:
        rc = subprocess.call(argv, env=env)
    except OSError as error:
        sys.stderr.write("run-queue-progress: %s\n" % error)
        rc = 127
    if rc < 0:
        rc = 128 - rc
    secs = max(0, int(time.time()) - start)
    record(name, "ok" if rc == 0 else "fail", secs=secs, log=log)
    return rc


def _last_states(path, want):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return {}
    last = {}
    for line in text.split("\n"):
        if not line.startswith("step="):
            continue
        name = state = ""
        # The last record per step, as the hub folds them; detail= ends a line.
        for token in line.split():
            if token.startswith("detail="):
                break
            if token.startswith("step="):
                name = token[5:]
            elif token.startswith("state="):
                state = token[6:]
        if name in want and state in _STATES:
            last[name] = state
    return last


def settle(reason, names=None):
    """Settle every named step that is still pending or running."""
    if not reason:
        raise ValueError("settle needs a reason")
    targets = list(_names) if names is None else list(names)
    path = os.environ.get("RUN_QUEUE_PROGRESS", "")
    if not path or not targets or not os.path.isfile(path):
        return
    last = _last_states(path, set(targets))
    at = int(time.time())
    lines = []
    seen = set()
    for name in targets:
        if name in seen:
            continue
        seen.add(name)
        if last.get(name) == "pending":
            lines.append(_line(name, "skip", {"detail": "not run: " + reason}, at))
        elif last.get(name) == "run":
            lines.append(_line(name, "fail", {"detail": "stopped: " + reason}, at))
    for line in lines:
        _write([line])


def _at_exit():
    settle("exit")


def _on_signal(signum, frame):
    for name in _SIGNALS:
        signal.signal(getattr(signal, "SIG" + name), signal.SIG_DFL)
    settle("signal " + signal.Signals(signum).name[3:])
    os.kill(os.getpid(), signum)


def install():
    """Settle at exit and on TERM, INT, HUP and ALRM."""
    global _installed
    if _installed:
        return
    _installed = True
    atexit.register(_at_exit)
    for name in _SIGNALS:
        signal.signal(getattr(signal, "SIG" + name), _on_signal)
