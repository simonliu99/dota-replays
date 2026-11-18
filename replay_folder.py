#!/usr/bin/env python3
"""Check that the replay folder holds only replay files and db-backups.

Each replay is a regular file whose name matches the hub's pattern and ends
with a stored format's suffix. Its first bytes are read, so a replay whose
bytes are not the format its name claims is reported: zstd bodies saved as
.dem.bz2 since 2026-11-10 are the case this catches (#32).
"""

import logging
import os
import stat
import sys
from pathlib import Path

import publish
import replay_format

logger = logging.getLogger(__name__)


def _head_format(head: bytes) -> str:
    """Return the name of the format head starts with, or its hex."""
    suffix = replay_format.suffix_for(head)
    if suffix is None:
        return f"unknown {head.hex() or 'empty'}"
    return replay_format.FORMAT_NAMES[suffix]


def main() -> int:
    """Check the replay folder; return 0 when clean, 1 on any stray entry or
    replay whose bytes do not match its name."""
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
    mismatches = []

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
                    replay_format.replay_suffix(entry.name) is not None
                    and publish.BULK_FILE_PATTERN.fullmatch(entry.name)
                ):
                    try:
                        if entry.is_file(follow_symlinks=False):
                            replay_count += 1
                            suffix = replay_format.replay_suffix(entry.name)
                            head = replay_format.read_head(Path(entry.path))
                            if replay_format.suffix_for(head) != suffix:
                                mismatches.append((entry.name, _head_format(head)))
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

    if mismatches:
        mismatches.sort(key=lambda item: item[0])
        for name, head_format in mismatches:
            logger.error(
                f"Replay whose bytes do not match its name: {name!r} ({head_format})"
            )
        logger.error(
            f"Replays whose bytes do not match their names: {len(mismatches)}"
        )

    if strays or mismatches:
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
