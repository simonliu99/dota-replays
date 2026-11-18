#!/usr/bin/env python3
"""Read-only probe: count replay files by name suffix and leading bytes (#32).

Usage:
    python3 scripts/probe_replay_formats.py --replay-dir DIR [--db DB]
        [--newest N] [--head-request]

Prints the Python version, a count per (suffix, format) pair, every file whose
format does not match its suffix, the format of the N newest files, the first
8 bytes inside the newest zstd file (when Python 3.14's compression.zstd or the
`zstd` command is present) and, with --db, how many downloads rows name a
mislabeled file. --head-request also sends one HEAD request to the newest
mislabeled file's recorded replay URL and prints its response headers.
Writes nothing; opens the database read-only. Exits 0 when every file's format
matches its suffix, 1 otherwise, 2 on a usage error.
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

MAGIC = {b"BZh": "bzip2", b"\x28\xb5\x2f\xfd": "zstd"}
SUFFIX_FORMAT = {".dem.bz2": "bzip2", ".dem.zst": "zstd"}


def file_format(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError as error:
        return f"unreadable ({error.strerror})"
    for magic, name in MAGIC.items():
        if head.startswith(magic):
            return name
    return f"other ({head.hex() or 'empty'})"


def suffix_of(name: str) -> str:
    for suffix in SUFFIX_FORMAT:
        if name.endswith(suffix):
            return suffix
    return "".join(Path(name).suffixes) or "(none)"


def inner_head(path: Path) -> str:
    try:
        from compression import zstd  # Python 3.14 and later
        with zstd.open(path, "rb") as handle:
            return handle.read(8).hex() + " (compression.zstd)"
    except ImportError:
        pass
    except Exception as error:  # report, never crash the probe
        return f"compression.zstd failed: {error}"
    tool = shutil.which("zstd")
    if tool is None:
        return "unknown: no compression.zstd and no zstd command"
    with open(path, "rb") as source:
        process = subprocess.Popen(
            [tool, "-dc"], stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        head = process.stdout.read(8)
        process.kill()
        process.wait()
    return head.hex() + " (zstd -dc)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replay-dir", required=True, type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--newest", type=int, default=120)
    parser.add_argument("--head-request", action="store_true")
    args = parser.parse_args()
    if not args.replay_dir.is_dir():
        print(f"not a directory: {args.replay_dir}", file=sys.stderr)
        return 2
    if args.db is not None and not args.db.is_file():
        print(f"no database at {args.db}", file=sys.stderr)
        return 2

    print(f"python {sys.version.split()[0]}")
    files = []
    with os.scandir(args.replay_dir) as entries:
        for entry in entries:
            if ".dem" in entry.name and entry.is_file(follow_symlinks=False):
                path = Path(entry.path)
                files.append((entry.stat().st_mtime, path, suffix_of(entry.name), file_format(path)))
    files.sort(key=lambda item: item[0])

    counts = {}
    mismatched = []
    for _, path, suffix, fmt in files:
        counts[(suffix, fmt)] = counts.get((suffix, fmt), 0) + 1
        if SUFFIX_FORMAT.get(suffix) != fmt:
            mismatched.append((path, fmt))
    print(f"files: {len(files)}")
    for (suffix, fmt), count in sorted(counts.items()):
        print(f"count {suffix} {fmt}: {count}")
    for path, fmt in mismatched:
        print(f"mismatch: {path.name} is {fmt}")
    newest = files[-args.newest:] if args.newest > 0 else []
    newest_counts = {}
    for _, _, _, fmt in newest:
        newest_counts[fmt] = newest_counts.get(fmt, 0) + 1
    print(f"newest {len(newest)}: " + ", ".join(f"{k} {v}" for k, v in sorted(newest_counts.items())))
    if newest:
        print(f"newest file: {newest[-1][1].name}, oldest of those: {newest[0][1].name}")
    zstd_files = [path for _, path, _, fmt in files if fmt == "zstd"]
    if zstd_files:
        print(f"inner head of {zstd_files[-1].name}: {inner_head(zstd_files[-1])}")

    if args.db is not None:
        print(f"wal file present: {Path(str(args.db) + '-wal').exists()}")
        connection = sqlite3.connect(f"file:{args.db}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        names = {path.name for path, _ in mismatched}
        rows = connection.execute(
            "SELECT d.match_id, d.filename, md.replay_url FROM downloads d "
            "LEFT JOIN match_details md ON md.match_id = d.match_id"
        ).fetchall()
        affected = [row for row in rows if row["filename"] in names]
        print(f"downloads rows: {len(rows)}, naming a mismatched file: {len(affected)}")
        if args.head_request and affected and mismatched:
            newest_name = mismatched[-1][0].name
            row = next((r for r in affected if r["filename"] == newest_name), affected[-1])
            url = row["replay_url"]
            print(f"HEAD {url}")
            request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "dota-replays/1.0"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    print(f"status {response.status}")
                    for key, value in response.headers.items():
                        print(f"header {key}: {value}")
            except Exception as error:
                print(f"HEAD failed: {error}")
            local = args.replay_dir / row["filename"]
            print(f"local size {local.stat().st_size if local.exists() else 'missing'}")
        connection.close()
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
