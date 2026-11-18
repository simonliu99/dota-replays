#!/usr/bin/env python3
"""Rename replays whose bytes are zstd but whose name says .dem.bz2 (#32).

Usage:
    python3 repair_replay_names.py --db DB --replay-dir DIR [--apply]

Downloads since 2026-11-10 saved zstd bodies under the replay URL's .dem.bz2
name. This script renames each such file in place, without rewriting its bytes,
and updates the downloads row that names it. It also repairs the row an
interrupted earlier run left behind: a row naming a missing X.dem.bz2 while
X.dem.zst holds zstd bytes.

The default is a dry run: it prints what it would do and changes nothing. It
opens the database read-only and immutable, so SQLite creates no -wal or -shm
file beside it, prints whether a -wal file is already there, and exits 1 when
work remains.

--apply copies the database to <DB>.before-repair-<UTC stamp>, checks the copy
with PRAGMA integrity_check and stops before touching any file when that fails,
then renames and commits one row per item. A destination that already exists is
a conflict: it is reported and skipped, never overwritten. Re-running is safe
and exits 0 when nothing is left to repair.

Run it as the account that owns the database folder, with no dota-replays job
queued or running.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import replay_format

OLD_SUFFIX = ".dem.bz2"
NEW_SUFFIX = ".dem.zst"


def _scan_folder(folder: Path) -> tuple[list[str], list[tuple[str, bytes]]]:
    """Return the .dem.bz2 names holding zstd, and those holding neither format."""
    renames = []
    unknown = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if not entry.name.endswith(OLD_SUFFIX):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            head = replay_format.read_head(Path(entry.path))
            suffix = replay_format.suffix_for(head)
            if suffix == NEW_SUFFIX:
                renames.append(entry.name)
            elif suffix != OLD_SUFFIX:
                unknown.append((entry.name, head))
    return sorted(renames), sorted(unknown)


def _row_for(connection: sqlite3.Connection, filename: str) -> int | None:
    """Return the match ID of the downloads row naming filename, or None."""
    row = connection.execute(
        "SELECT match_id FROM downloads WHERE filename = ?", (filename,)
    ).fetchone()
    return row[0] if row else None


def _describe_row(match_id: int | None) -> str:
    return "no row" if match_id is None else f"row: match {match_id}"


def _interrupted_rows(
    connection: sqlite3.Connection, folder: Path
) -> list[tuple[int, str]]:
    """Rows naming a missing X.dem.bz2 whose X.dem.zst holds zstd bytes."""
    items = []
    rows = connection.execute(
        "SELECT match_id, filename FROM downloads ORDER BY match_id"
    ).fetchall()
    for match_id, filename in rows:
        if not filename or not filename.endswith(OLD_SUFFIX):
            continue
        if (folder / filename).exists():
            continue
        new_name = replay_format.replay_stem(filename) + NEW_SUFFIX
        new_path = folder / new_name
        if not new_path.exists():
            continue
        if replay_format.suffix_for(replay_format.read_head(new_path)) != NEW_SUFFIX:
            continue
        items.append((match_id, new_name))
    return items


def _copy_database(db: Path) -> Path:
    """Write <DB>.before-repair-<stamp>, check it and return its path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(f"{db}.before-repair-{stamp}")
    target.unlink(missing_ok=True)
    source = sqlite3.connect(db)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
            rows = copy.execute("PRAGMA integrity_check").fetchall()
            # A plain file is the copy: it needs no -shm file beside it.
            copy.execute("PRAGMA journal_mode=DELETE")
        finally:
            copy.close()
    finally:
        source.close()
    if rows != [("ok",)]:
        raise sqlite3.DatabaseError(f"integrity check failed: {rows[:3]}")
    return target


def _print_unknown(unknown: list[tuple[str, bytes]]) -> None:
    for name, head in unknown:
        print(f"left alone: {name} (unknown {head.hex() or 'empty'})")


def _dry_run(db: Path, folder: Path) -> int:
    print(f"wal file present: {Path(str(db) + '-wal').exists()}")
    connection = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        renames, unknown = _scan_folder(folder)
        renamed = 0
        conflicts = 0
        for name in renames:
            target = replay_format.replay_stem(name) + NEW_SUFFIX
            match_id = _row_for(connection, name)
            if (folder / target).exists():
                print(
                    f"conflict: {target} already exists, so {name} is left alone "
                    f"({_describe_row(match_id)})"
                )
                conflicts += 1
            else:
                print(f"would rename {name} -> {target} ({_describe_row(match_id)})")
                renamed += 1
        updates = _interrupted_rows(connection, folder)
        for match_id, new_name in updates:
            print(f"would update row {match_id} -> {new_name}")
    finally:
        connection.close()
    _print_unknown(unknown)
    print(
        f"dry run: {renamed} renames, {len(updates)} row updates, {conflicts} conflicts"
    )
    return 1 if renamed or updates or conflicts else 0


def _apply(db: Path, folder: Path) -> int:
    try:
        copy_path = _copy_database(db)
    except (OSError, sqlite3.Error) as error:
        print(f"error: database copy failed, so nothing was renamed: {error}")
        return 1
    print(f"database copy: {copy_path}")

    connection = sqlite3.connect(db)
    try:
        renames, unknown = _scan_folder(folder)
        renamed = 0
        conflicts = 0
        for name in renames:
            target = replay_format.replay_stem(name) + NEW_SUFFIX
            match_id = _row_for(connection, name)
            if (folder / target).exists():
                print(
                    f"conflict: {target} already exists, so {name} is left alone "
                    f"({_describe_row(match_id)})"
                )
                conflicts += 1
                continue
            os.rename(folder / name, folder / target)
            if match_id is not None:
                connection.execute(
                    "UPDATE downloads SET filename = ? WHERE filename = ?",
                    (target, name),
                )
                connection.commit()
            print(f"renamed {name} -> {target} ({_describe_row(match_id)})")
            renamed += 1
        updated = 0
        for match_id, new_name in _interrupted_rows(connection, folder):
            connection.execute(
                "UPDATE downloads SET filename = ? WHERE match_id = ?",
                (new_name, match_id),
            )
            connection.commit()
            print(f"updated row {match_id} -> {new_name}")
            updated += 1
    finally:
        connection.close()
    _print_unknown(unknown)
    print(f"applied: {renamed} renames, {updated} row updates, {conflicts} conflicts")
    return 1 if conflicts else 0


def main(argv: list[str] | None = None) -> int:
    """Repair the replay names; return 0 when nothing is left to repair."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path, help="the database file")
    parser.add_argument(
        "--replay-dir", required=True, type=Path, help="the replay folder"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rename the files and update the rows; the default is a dry run",
    )
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1
    if not args.replay_dir.is_dir():
        print(f"not a directory: {args.replay_dir}", file=sys.stderr)
        return 1
    if args.apply:
        return _apply(args.db, args.replay_dir)
    return _dry_run(args.db, args.replay_dir)


if __name__ == "__main__":
    sys.exit(main())
