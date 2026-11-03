#!/usr/bin/env python3
"""Check that the replay folder holds only replay files and db-backups."""

import logging
import os
import stat
import sys
from pathlib import Path

import publish

logger = logging.getLogger(__name__)


def main() -> int:
    """Check the replay folder; return 0 when clean, 1 on any stray entry."""
    replay_dir_str = os.environ.get("DOTA_REPLAY_DIR")
    if not replay_dir_str:
        logger.error("DOTA_REPLAY_DIR is not set")
        return 1
    replay_dir = Path(replay_dir_str)
    if not replay_dir.is_dir():
        logger.error(f"DOTA_REPLAY_DIR is not a directory: {replay_dir}")
        return 1

    replay_count = 0
    has_db_backups = False
    strays = []

    try:
        with os.scandir(replay_dir) as entries:
            for entry in entries:
                if entry.name == "db-backups":
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            has_db_backups = True
                            continue
                    except OSError:
                        pass
                elif (
                    entry.name.endswith(".dem.bz2")
                    and publish.BULK_FILE_PATTERN.fullmatch(entry.name)
                ):
                    try:
                        if entry.is_file(follow_symlinks=False):
                            replay_count += 1
                            continue
                    except OSError:
                        pass

                # Stray entry
                try:
                    st = os.lstat(entry.path)
                    if stat.S_ISLNK(st.st_mode):
                        kind = "symlink"
                        size = None
                    elif stat.S_ISDIR(st.st_mode):
                        kind = "directory"
                        size = None
                    elif stat.S_ISREG(st.st_mode):
                        kind = "file"
                        size = st.st_size
                    else:
                        kind = "other"
                        size = None
                except OSError:
                    kind = "other"
                    size = None
                strays.append((entry.name, kind, size))
    except OSError as error:
        logger.error(f"Failed to scan {replay_dir}: {error}")
        return 1

    strays.sort(key=lambda item: item[0])

    if strays:
        for name, kind, size in strays:
            if size is not None:
                logger.error(
                    f"Stray entry in the replay folder: {name!r} ({kind}, {size} bytes)"
                )
            else:
                logger.error(
                    f"Stray entry in the replay folder: {name!r} ({kind})"
                )
        logger.error(f"Stray entries in the replay folder: {len(strays)}")
        return 1

    if has_db_backups:
        logger.info(
            f"The replay folder holds {replay_count} replay files and db-backups"
        )
    else:
        logger.info(
            f"The replay folder holds {replay_count} replay files and no db-backups"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
