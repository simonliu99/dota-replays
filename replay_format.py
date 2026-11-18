"""The replay formats this project stores, recognized by leading bytes and name.

Valve serves zstd bodies at `.dem.bz2` URLs since 2026-11-10 (#32), so a
replay's name says which format its bytes are in and the two are checked
against each other. The download, `folder-check`, the directory scan and the
repair script share this table so they agree.
"""

from pathlib import Path

# The suffix of each stored format and the bytes its files start with.
FORMATS = {".dem.bz2": b"BZh", ".dem.zst": b"\x28\xb5\x2f\xfd"}
# The format names the folder check prints, keyed by the same suffixes.
FORMAT_NAMES = {".dem.bz2": "bzip2", ".dem.zst": "zstd"}
HEAD_BYTES = 4


def suffix_for(head: bytes) -> str | None:
    """Return the suffix of the format head starts with, or None."""
    for suffix, magic in FORMATS.items():
        if head.startswith(magic):
            return suffix
    return None


def replay_suffix(name: str) -> str | None:
    """Return the suffix of a stored format name ends with, or None."""
    for suffix in FORMATS:
        if name.endswith(suffix):
            return suffix
    return None


def replay_stem(name: str) -> str:
    """Return name without its stored format's suffix (X.dem.bz2 becomes X).

    A replay's name is its stem and the suffix of its format's bytes, so the
    stem is what the download, the scan and the repair compare across formats.
    """
    suffix = replay_suffix(name)
    if suffix is None:
        return name
    return name[: -len(suffix)]


def read_head(path: Path) -> bytes:
    """Return the first HEAD_BYTES bytes of path, or b"" when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return handle.read(HEAD_BYTES)
    except OSError:
        return b""
