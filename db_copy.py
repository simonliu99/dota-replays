#!/usr/bin/env python3
"""Keep dated database backups in the replay folder on the share.

The backups live in $DOTA_REPLAY_DIR/db-backups as dota_replays-YYYYMMDDTHHMMSSZ.db.
Up to 14 backups are kept, pruned oldest first. A shrink guard refuses to write
or prune if the live database has fewer players or matches than the newest
earlier backup, and a clock guard stops pruning if any backup is dated after the
current run's start time.

Each backup is written locally to $SWITCHBOARD_BULK/dota_replays.db.copy with
SQLite's backup API, verified with PRAGMA integrity_check, copied to a .part
file in db-backups and fsynced before atomic rename: no SQLite connection opens
a file on the share.
"""

import argparse
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DATABASE_NAME = "dota_replays.db"
LOCAL_COPY_NAME = "dota_replays.db.copy"
BACKUP_DIR_NAME = "db-backups"
BACKUP_PATTERN = re.compile(r"dota_replays-([0-9]{8}T[0-9]{6}Z)\.db")
PART_PATTERN = re.compile(r"dota_replays-[0-9]{8}T[0-9]{6}Z\.db\.part")
BACKUPS_KEPT = 14


def _directory(name: str) -> Path:
    """Return the directory an environment variable names, or exit 1."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set")
    path = Path(value)
    if not path.is_dir():
        raise SystemExit(f"{name} is not a directory: {path}")
    return path


def _backup_dir(replay_dir: Path) -> Path:
    """Return the db-backups directory inside replay_dir, creating it if needed."""
    path = replay_dir / BACKUP_DIR_NAME
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path):
            raise OSError(f"{path} is a symlink or not a directory")
    else:
        os.mkdir(path)
    return path


def _candidates(backup_dir: Path) -> list[str]:
    """Return sorted candidate backup filenames in backup_dir."""
    candidates = []
    with os.scandir(backup_dir) as entries:
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            match = BACKUP_PATTERN.fullmatch(entry.name)
            if not match:
                continue
            stamp = match.group(1)
            try:
                time.strptime(stamp, "%Y%m%dT%H%M%SZ")
            except ValueError:
                continue
            candidates.append(entry.name)
    candidates.sort()
    return candidates


def _counts(db_path: Path) -> tuple[int, int]:
    """Return (players_count, matches_count) from a SQLite database."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        players = connection.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        matches = connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        return int(players), int(matches)
    finally:
        connection.close()


def _write_checked_copy(source: Path, local_copy: Path) -> None:
    """Write a local backup of source at local_copy and check it."""
    local_copy.unlink(missing_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True)
    try:
        copy_connection = sqlite3.connect(local_copy)
        try:
            source_connection.backup(copy_connection)
            rows = copy_connection.execute("PRAGMA integrity_check").fetchall()
            # A plain database file is the backup: it needs no -shm file beside
            # it to be read, so the copy on the share opens read-only.
            copy_connection.execute("PRAGMA journal_mode=DELETE")
        finally:
            copy_connection.close()
    finally:
        source_connection.close()
    if rows != [("ok",)]:
        raise sqlite3.DatabaseError(f"integrity check failed: {rows[:3]}")


def _shrink_guard(
    bulk: Path,
    backup_dir: Path,
    candidates: list[str],
    new_players: int,
    new_matches: int,
) -> None:
    """Refuse to shrink the database compared to the newest earlier backup."""
    if not candidates:
        return
    newest_earlier = candidates[-1]
    share_file = backup_dir / newest_earlier
    with tempfile.TemporaryDirectory(dir=bulk) as tmp_dir:
        temp_copy = Path(tmp_dir) / newest_earlier
        shutil.copyfile(share_file, temp_copy)
        share_size = share_file.stat().st_size
        temp_size = temp_copy.stat().st_size
        if share_size != temp_size:
            raise OSError(
                f"Size mismatch copying {share_file} ({share_size} != {temp_size})"
            )
        uri = temp_copy.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
        if rows != [("ok",)]:
            raise sqlite3.DatabaseError(
                f"Integrity check failed for earlier backup {share_file}: {rows[:3]}"
            )
        earlier_players, earlier_matches = _counts(temp_copy)

    if new_players < earlier_players or new_matches < earlier_matches:
        msg = (
            f"Refusing to shrink database: live has {new_players} players and "
            f"{new_matches} matches, but newest backup {newest_earlier} has "
            f"{earlier_players} players and {earlier_matches} matches; "
            f"to accept the smaller database, move the backups with more rows "
            f"into db-backups/held/"
        )
        logger.error(msg)
        raise ValueError(msg)


def _prune(backup_dir: Path, backup_name: str) -> None:
    """Remove oldest candidate backups beyond BACKUPS_KEPT."""
    candidates = _candidates(backup_dir)
    if len(candidates) > BACKUPS_KEPT:
        excess = len(candidates) - BACKUPS_KEPT
        for old_name in candidates[:excess]:
            if old_name == backup_name:
                continue
            prune_path = backup_dir / old_name
            os.unlink(prune_path)
            logger.info(f"Pruned old backup {prune_path}")


def write_backup() -> int:
    """Write the backup; return 0 on success and 1 on any failure."""
    bulk = _directory("SWITCHBOARD_BULK")
    replay_dir = _directory("DOTA_REPLAY_DIR")
    source = bulk / DATABASE_NAME
    local_copy = bulk / LOCAL_COPY_NAME
    started = time.monotonic()

    part = None
    try:
        backup_dir = _backup_dir(replay_dir)

        # 2. Stale parts: remove regular files matching PART_PATTERN
        with os.scandir(backup_dir) as entries:
            for entry in entries:
                if PART_PATTERN.fullmatch(entry.name) and entry.is_file(
                    follow_symlinks=False
                ):
                    try:
                        os.unlink(entry.path)
                        logger.info(f"Removed stale part file {entry.name}")
                    except OSError as error:
                        logger.warning(
                            f"Failed to remove stale part {entry.name}: {error}"
                        )

        # 3. No overwrite: compute name and check lexists
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_name = f"dota_replays-{stamp}.db"
        target = backup_dir / backup_name
        if os.path.lexists(target):
            logger.error(f"Backup already exists: {target}")
            return 1

        # 4. Checked local copy
        _write_checked_copy(source, local_copy)
        new_players, new_matches = _counts(local_copy)

        # 5. Shrink guard: compare with newest earlier candidate
        candidates = _candidates(backup_dir)
        _shrink_guard(bulk, backup_dir, candidates, new_players, new_matches)

        # 6. Write to .part, fsync, compare size, atomic rename
        part = backup_dir / f"{backup_name}.part"
        shutil.copyfile(local_copy, part)
        with open(part, "rb+") as handle:
            os.fsync(handle.fileno())
        if part.stat().st_size != local_copy.stat().st_size:
            logger.error(
                f"Size mismatch writing {part}: {part.stat().st_size} != {local_copy.stat().st_size}"
            )
            part.unlink(missing_ok=True)
            return 1
        os.replace(part, target)

        # 7. Clock guard: if any candidate sorts after new name, log, prune nothing, fail
        candidates_after = _candidates(backup_dir)
        if any(c > backup_name for c in candidates_after):
            logger.error(
                f"Candidate backup sorts after current backup {backup_name}; skipping prune"
            )
            return 1

        # 8. Prune
        _prune(backup_dir, backup_name)

    except (OSError, sqlite3.Error, ValueError) as error:
        logger.error(f"Backup failed: {error}")
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        return 1
    finally:
        try:
            local_copy.unlink(missing_ok=True)
        except OSError:
            pass

    logger.info(
        f"Copied {target.stat().st_size} bytes to {target} in "
        f"{time.monotonic() - started:.1f}s"
    )
    return 0


def check_backups(max_age_days: int) -> int:
    """Verify newest database backup and check pruning; return 0 or 1."""
    bulk = _directory("SWITCHBOARD_BULK")
    replay_dir = _directory("DOTA_REPLAY_DIR")
    backup_dir = replay_dir / BACKUP_DIR_NAME

    if not os.path.lexists(backup_dir) or os.path.islink(backup_dir) or not os.path.isdir(backup_dir):
        logger.error(f"{backup_dir} is missing, a symlink or not a directory")
        return 1

    # Warn about stale .part files (exit unaffected)
    with os.scandir(backup_dir) as entries:
        for entry in entries:
            if PART_PATTERN.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                logger.warning(f"Stale part file found: {entry.name}")

    candidates = _candidates(backup_dir)
    if not candidates:
        logger.error(f"No candidate backups found in {backup_dir}")
        return 1
    if len(candidates) > BACKUPS_KEPT:
        logger.error(
            f"Too many candidate backups ({len(candidates)} > {BACKUPS_KEPT}) in {backup_dir}"
        )
        return 1

    newest_name = candidates[-1]
    match = BACKUP_PATTERN.fullmatch(newest_name)
    stamp_str = match.group(1)
    stamp_time = datetime.strptime(stamp_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    now_time = datetime.now(timezone.utc)
    age_seconds = (now_time - stamp_time).total_seconds()
    if age_seconds < -3600.0:
        logger.error(
            f"Newest backup {newest_name} is more than one hour in the future ({stamp_str})"
        )
        return 1
    age_days = age_seconds / 86400.0
    if age_days > max_age_days:
        logger.error(
            f"Newest backup {newest_name} is {age_days:.1f} days old (max {max_age_days} days)"
        )
        return 1

    share_file = backup_dir / newest_name
    with tempfile.TemporaryDirectory(dir=bulk) as tmp_dir:
        temp_copy = Path(tmp_dir) / newest_name
        try:
            shutil.copyfile(share_file, temp_copy)
        except OSError as error:
            logger.error(f"Failed to copy newest backup {share_file}: {error}")
            return 1
        if share_file.stat().st_size != temp_copy.stat().st_size:
            logger.error(f"Size mismatch copying {share_file}")
            return 1
        uri = temp_copy.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.Error as error:
            logger.error(f"Integrity check failed for newest backup {newest_name}: {error}")
            return 1
        finally:
            conn.close()
        if rows != [("ok",)]:
            logger.error(
                f"Integrity check failed for newest backup {newest_name}: {rows[:3]}"
            )
            return 1
        try:
            backup_players, backup_matches = _counts(temp_copy)
        except (sqlite3.Error, OSError) as error:
            logger.error(f"Failed to read counts from newest backup {newest_name}: {error}")
            return 1

    live_db = bulk / DATABASE_NAME
    try:
        live_players, live_matches = _counts(live_db)
    except (sqlite3.Error, OSError) as error:
        logger.error(f"Failed to read counts from live database {live_db}: {error}")
        return 1
    if backup_players > live_players or backup_matches > live_matches:
        logger.error(
            f"Database lost records: live has {live_players} players and {live_matches} matches, "
            f"but newest backup {newest_name} has {backup_players} players and {backup_matches} matches"
        )
        return 1

    logger.info(
        f"Verified newest backup {newest_name} (age {age_days:.1f} days): "
        f"{backup_players} players, {backup_matches} matches"
    )
    return 0


def main(argv=None) -> int:
    """Parse arguments and write or check database backups."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check database backups")
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=8,
        help="Maximum age of newest backup in days",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check_backups(args.max_age_days)
    return write_backup()


if __name__ == "__main__":
    sys.exit(main())
