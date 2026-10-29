#!/usr/bin/env python3
"""Write a checked copy of the replay database to the share.

The copy is written on the local disk with SQLite's backup API and checked
with PRAGMA integrity_check, then copied byte for byte to the share and
renamed: no SQLite connection opens a file on the share.
"""

import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DATABASE_NAME = "dota_replays.db"
LOCAL_COPY_NAME = "dota_replays.db.copy"


def _directory(name: str) -> Path:
    """Return the directory an environment variable names, or exit 1."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set")
    path = Path(value)
    if not path.is_dir():
        raise SystemExit(f"{name} is not a directory: {path}")
    return path


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


def main() -> int:
    """Write the copy; return 0 on success and 1 on any failure."""
    bulk = _directory("SWITCHBOARD_BULK")
    copy_dir = _directory("DOTA_DB_COPY_DIR")
    source = bulk / DATABASE_NAME
    local_copy = bulk / LOCAL_COPY_NAME
    part = copy_dir / f"{DATABASE_NAME}.part"
    target = copy_dir / DATABASE_NAME
    started = time.monotonic()

    try:
        _write_checked_copy(source, local_copy)
        shutil.copyfile(local_copy, part)
        with open(part, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(part, target)
    except (OSError, sqlite3.Error) as error:
        logger.error(f"Copy failed: {error}")
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


if __name__ == "__main__":
    sys.exit(main())
