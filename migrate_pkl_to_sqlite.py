#!/usr/bin/env python3
"""
Migration script to convert existing .pkl files to SQLite database.

Usage:
    python migrate_pkl_to_sqlite.py [--dry-run]
    python migrate_pkl_to_sqlite.py --consolidate-replays
"""

import os
import sys
import shutil
import pickle
import argparse
import logging
from pathlib import Path
from glob import glob

from database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def consolidate_replay_folders(target_dir: Path, dry_run: bool = False) -> int:
    """
    Consolidate all replays-* folders into a single target directory.
    
    Returns number of files moved.
    """
    # Find all replays-* directories
    replay_dirs = glob("./replays-*")
    if not replay_dirs:
        logger.info("No replays-* folders found to consolidate")
        return 0
    
    logger.info(f"Found {len(replay_dirs)} replay folder(s) to consolidate")
    
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
    
    moved = 0
    skipped = 0
    
    for dir_path in replay_dirs:
        dir_path = Path(dir_path)
        logger.info(f"Processing: {dir_path}")
        
        for file_path in dir_path.glob("*.dem.bz2"):
            target_path = target_dir / file_path.name
            
            if target_path.exists():
                logger.debug(f"  Skipping (exists): {file_path.name}")
                skipped += 1
                continue
            
            if dry_run:
                logger.info(f"  [DRY RUN] Would move: {file_path.name}")
            else:
                shutil.move(str(file_path), str(target_path))
                logger.debug(f"  Moved: {file_path.name}")
            moved += 1
    
    logger.info(f"Moved {moved} files, skipped {skipped} duplicates")
    
    if not dry_run and moved > 0:
        # Check if old directories are empty and can be removed
        for dir_path in replay_dirs:
            dir_path = Path(dir_path)
            remaining = list(dir_path.glob("*"))
            if not remaining:
                dir_path.rmdir()
                logger.info(f"Removed empty directory: {dir_path}")
            else:
                logger.info(f"Directory not empty, keeping: {dir_path} ({len(remaining)} files remain)")
    
    return moved


def migrate_pkl_file(pkl_path: Path, db: Database, dry_run: bool = False) -> bool:
    """
    Migrate a single .pkl file to the database.
    
    Returns True if successful, False otherwise.
    """
    logger.info(f"Processing: {pkl_path}")
    
    try:
        with open(pkl_path, "rb") as f:
            data = pickle.load(f)
    except Exception as e:
        logger.error(f"Failed to load {pkl_path}: {e}")
        return False
    
    # Extract player_id from filename (dr-{player_id}.pkl) or data
    player_id = data.get("player_id")
    if not player_id:
        # Try to extract from filename
        try:
            player_id = int(pkl_path.stem.split("-")[1])
        except (IndexError, ValueError):
            logger.error(f"Could not determine player_id from {pkl_path}")
            return False
    
    logger.info(f"  Player ID: {player_id}")
    
    matches = data.get("matches", [])
    cache = data.get("cache", {})
    downloaded = data.get("downloaded", [])
    
    logger.info(f"  Matches: {len(matches)}")
    logger.info(f"  Cached details: {len(cache)}")
    logger.info(f"  Downloads tracked: {len(downloaded)}")
    
    if dry_run:
        logger.info("  [DRY RUN] Would migrate this data")
        return True
    
    # Migrate player
    db.upsert_player(player_id)
    
    # Migrate matches
    if matches:
        new_count = db.insert_matches(player_id, matches)
        logger.info(f"  Inserted {new_count} new matches")
    
    # Migrate cached match details
    for match_id, details in cache.items():
        match_id = int(match_id) if isinstance(match_id, str) else match_id
        db.upsert_match_details(match_id, details)
    logger.info(f"  Migrated {len(cache)} match details")
    
    # Note: downloaded list in old format is just filenames, not structured
    # We'll skip migrating downloads as they'll be re-discovered on --verify
    if downloaded:
        logger.info(f"  Note: {len(downloaded)} download records will be re-discovered with --verify")
    
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate .pkl files to SQLite database"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without making changes",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="./dota_replays.db",
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--pkl-pattern",
        type=str,
        default="./dr-*.pkl",
        help="Glob pattern to find .pkl files",
    )
    parser.add_argument(
        "--consolidate-replays",
        action="store_true",
        help="Consolidate replays-* folders into single /replays folder",
    )
    parser.add_argument(
        "--replay-dir",
        type=str,
        default="./replays",
        help="Target directory for consolidated replays",
    )
    args = parser.parse_args()
    
    # Handle replay consolidation
    if args.consolidate_replays:
        consolidate_replay_folders(Path(args.replay_dir), dry_run=args.dry_run)
        if not args.dry_run:
            logger.info("\nNext: Run 'python dota_replays.py --verify' to update database")
        return
    
    # Find .pkl files
    pkl_files = glob(args.pkl_pattern)
    if not pkl_files:
        logger.info(f"No .pkl files found matching: {args.pkl_pattern}")
        sys.exit(0)
    
    logger.info(f"Found {len(pkl_files)} .pkl file(s) to migrate")
    
    if args.dry_run:
        logger.info("[DRY RUN MODE]")
        db = None
    else:
        db = Database(args.db)
    
    success = 0
    failed = 0
    
    for pkl_path in pkl_files:
        if migrate_pkl_file(Path(pkl_path), db, dry_run=args.dry_run):
            success += 1
        else:
            failed += 1
    
    if db:
        db.close()
    
    logger.info(f"\nMigration complete: {success} succeeded, {failed} failed")
    
    if success > 0 and not args.dry_run:
        logger.info("\nNext steps:")
        logger.info("1. Run 'python migrate_pkl_to_sqlite.py --consolidate-replays' to merge replay folders")
        logger.info("2. Run 'python dota_replays.py --verify' to re-discover downloads")
        logger.info("3. Backup and remove old .pkl files if migration looks correct")


if __name__ == "__main__":
    main()
