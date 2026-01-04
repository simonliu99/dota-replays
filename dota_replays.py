#!/usr/bin/env python3
"""
DotA Replay Downloader

Downloads match replays and caches OpenDota parsed data.
"""

import os
import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta

import wget
from tqdm import tqdm

from database import Database
from opendota_client import OpenDotaClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class DotaReplays:
    """Main application class for downloading DotA replays."""

    # Only process matches from the last N days
    RECENT_DAYS = 14

    def __init__(
        self,
        db: Database,
        client: OpenDotaClient,
        replay_dir: Path,
        wait_for_parse: bool = False,
    ):
        self.db = db
        self.client = client
        self.replay_dir = replay_dir
        self.wait_for_parse = wait_for_parse
        self.replay_dir.mkdir(parents=True, exist_ok=True)

    def update_player(self, player_id: int, limit: int | None = None) -> None:
        """Update match data for a single player."""
        logger.info(f"Updating player {player_id}")

        # Validate and register player
        player = self.client.get_player(player_id)
        if not player or "profile" not in player:
            logger.error(f"Player {player_id} not found")
            return

        persona_name = player.get("profile", {}).get("personaname")
        self.db.upsert_player(player_id, persona_name)
        logger.info(f"Player: {persona_name} ({player_id})")

        # Fetch matches
        logger.info("Fetching match list...")
        matches = self.client.get_player_matches(player_id, limit=limit)
        if not matches:
            logger.warning("No matches found")
            return

        new_count = self.db.insert_matches(player_id, matches)
        logger.info(f"Found {len(matches)} matches, {new_count} new")

        # Get details for matches without cache
        self._fetch_match_details(player_id, limit)

        # Download replays
        self._download_replays()

    def _fetch_match_details(self, player_id: int, limit: int | None = None) -> None:
        """Fetch and cache match details from OpenDota."""
        matches = self.db.get_matches_without_details(player_id, limit)
        if not matches:
            logger.info("All matches have cached details")
            return

        logger.info(f"Fetching details for {len(matches)} matches...")
        cutoff = datetime.now() - timedelta(days=self.RECENT_DAYS)

        for match in tqdm(matches, desc="Fetching details"):
            match_id = match["match_id"]
            start_time = datetime.fromtimestamp(match["start_time"])

            # Request parse for recent matches
            if start_time > cutoff and self.wait_for_parse:
                job_id = self.client.request_parse(match_id)
                if job_id:
                    self.db.record_parse_request(match_id, job_id)
                    logger.info(f"Waiting for parse of match {match_id}...")
                    if self.client.poll_parse_completion(job_id):
                        self.db.mark_parse_complete(match_id)
            elif start_time > cutoff:
                # Just request parse, don't wait
                job_id = self.client.request_parse(match_id)
                if job_id:
                    self.db.record_parse_request(match_id, job_id)

            # Fetch match details
            details = self.client.get_match_details(match_id)
            if details:
                self.db.upsert_match_details(match_id, details)

    def _download_replays(self) -> None:
        """Download replay files for matches with replay URLs."""
        matches = self.db.get_matches_with_replay_url(downloaded=False)
        if not matches:
            logger.info("No new replays to download")
            return

        logger.info(f"Downloading {len(matches)} replays...")
        success = 0
        failed = 0

        for match in tqdm(matches, desc="Downloading"):
            match_id = match["match_id"]
            replay_url = match["replay_url"]

            if not replay_url:
                continue

            filename = replay_url.split("/")[-1]
            filepath = self.replay_dir / filename

            if filepath.exists():
                # Already on disk, just record it
                self.db.record_download(match_id, filename, filepath.stat().st_size)
                continue

            try:
                wget.download(replay_url, out=str(self.replay_dir), bar=None)
                file_size = filepath.stat().st_size if filepath.exists() else None
                self.db.record_download(match_id, filename, file_size)
                success += 1
            except Exception as e:
                logger.error(f"Failed to download match {match_id}: {e}")
                failed += 1

        logger.info(f"Downloaded {success} replays, {failed} failed")

    def verify_downloads(self, redownload: bool = False) -> None:
        """Verify all downloads exist on disk."""
        downloads = self.db.get_downloads()
        if not downloads:
            logger.info("No downloads to verify")
            return

        logger.info(f"Verifying {len(downloads)} downloads...")
        missing = []

        for dl in tqdm(downloads, desc="Verifying"):
            match_id = dl["match_id"]
            filename = dl["filename"]
            filepath = self.replay_dir / filename

            if filepath.exists():
                self.db.update_on_disk_status(match_id, True)
            else:
                self.db.update_on_disk_status(match_id, False)
                missing.append(dl)
                logger.warning(f"Missing: {filename} (match {match_id})")

        if missing:
            logger.warning(f"{len(missing)} files missing from disk")
            if redownload:
                self._redownload_missing(missing)
        else:
            logger.info("All files verified on disk")

    def _redownload_missing(self, missing: list[dict]) -> None:
        """Re-download missing replay files."""
        logger.info(f"Re-downloading {len(missing)} missing files...")
        success = 0

        for dl in tqdm(missing, desc="Re-downloading"):
            match_id = dl["match_id"]

            # Get replay URL from match details
            details = self.db.get_match_details(match_id)
            if not details or not details.get("replay_url"):
                logger.warning(f"No replay URL for match {match_id}")
                continue

            replay_url = details["replay_url"]
            filename = replay_url.split("/")[-1]
            filepath = self.replay_dir / filename

            try:
                wget.download(replay_url, out=str(self.replay_dir), bar=None)
                file_size = filepath.stat().st_size if filepath.exists() else None
                self.db.record_download(match_id, filename, file_size)
                success += 1
            except Exception as e:
                logger.error(f"Failed to re-download match {match_id}: {e}")

        logger.info(f"Re-downloaded {success}/{len(missing)} files")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="DotA Replay Downloader - Downloads match replays and OpenDota data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "player_id",
        type=int,
        nargs="?",
        help="DotA player ID (omit to update all tracked players)",
    )
    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=None,
        help="Limit number of matches to process",
    )
    parser.add_argument(
        "--wait-for-parse",
        action="store_true",
        help="Wait for parse jobs to complete before fetching details",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify all downloaded files exist on disk",
    )
    parser.add_argument(
        "--redownload",
        action="store_true",
        help="Re-download missing files (requires --verify)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("OPENDOTA_API_KEY"),
        help="OpenDota API key for higher rate limits",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="./dota_replays.db",
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--replay-dir",
        type=str,
        default="./replays",
        help="Directory to store replay files",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.redownload and not args.verify:
        logger.error("--redownload requires --verify")
        sys.exit(1)

    # Initialize components
    db = Database(args.db)
    client = OpenDotaClient(api_key=args.api_key)
    replay_dir = Path(args.replay_dir)

    app = DotaReplays(
        db=db,
        client=client,
        replay_dir=replay_dir,
        wait_for_parse=args.wait_for_parse,
    )

    try:
        if args.verify:
            # Verification mode
            app.verify_downloads(redownload=args.redownload)
        elif args.player_id:
            # Update specific player
            if not client.validate_player(args.player_id):
                logger.error(f"Player {args.player_id} not found")
                sys.exit(1)
            app.update_player(args.player_id, limit=args.limit)
        else:
            # Update all tracked players
            players = db.get_all_players()
            if not players:
                logger.info("No players in database. Add a player by running:")
                logger.info("  python dota_replays.py <player_id>")
                sys.exit(0)

            logger.info(f"Updating {len(players)} tracked players...")
            for player in players:
                app.update_player(player["player_id"], limit=args.limit)

    finally:
        db.close()

    logger.info("Done!")


if __name__ == "__main__":
    main()
