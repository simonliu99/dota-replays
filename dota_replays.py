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
from dotenv import load_dotenv

from database import Database
from opendota_client import OpenDotaClient

# Load environment variables from .env file
load_dotenv()

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

    def scan_replay_directory(self) -> None:
        """Scan replay directory and register existing files in database."""
        if not self.replay_dir.exists():
            logger.warning(f"Replay directory does not exist: {self.replay_dir}")
            return

        # Get all .dem.bz2 files
        replay_files = list(self.replay_dir.glob("*.dem.bz2"))
        if not replay_files:
            logger.info(f"No replay files found in {self.replay_dir}")
            return

        logger.info(f"Found {len(replay_files)} replay files, matching to database...")

        # Get all match_ids we know about
        all_match_ids = set()
        for player in self.db.get_all_players():
            all_match_ids.update(self.db.get_match_ids_for_player(player["player_id"]))

        # Also build URL map as fallback
        matches_with_urls = self.db.get_matches_with_replay_url(downloaded=True)
        url_to_match = {}
        for m in matches_with_urls:
            if m["replay_url"]:
                filename = m["replay_url"].split("/")[-1]
                url_to_match[filename] = m["match_id"]

        matched = 0
        unmatched = 0
        unmatched_files = []

        for filepath in tqdm(replay_files, desc="Scanning"):
            filename = filepath.name
            match_id = None

            # Method 1: Extract match_id from filename prefix (e.g., "8634561787_1891250204.dem.bz2")
            try:
                match_id_str = filename.split("_")[0]
                potential_id = int(match_id_str)
                if potential_id in all_match_ids:
                    match_id = potential_id
            except (ValueError, IndexError):
                pass

            # Method 2: Fall back to URL matching
            if match_id is None and filename in url_to_match:
                match_id = url_to_match[filename]

            if match_id:
                file_size = filepath.stat().st_size
                self.db.record_download(match_id, filename, file_size)
                matched += 1
            else:
                unmatched += 1
                unmatched_files.append(filename)
                logger.debug(f"No match found for: {filename}")

        logger.info(f"Registered {matched} files, {unmatched} unmatched")
        if unmatched > 0:
            logger.info("Unmatched files may be from matches not in database (other players' matches)")

    def show_status(self) -> None:
        """Display database statistics."""
        stats = self.db.get_stats()
        
        print("\n" + "=" * 50)
        print("DATABASE STATUS")
        print("=" * 50)
        print(f"\nPlayers tracked:          {stats['players']}")
        print(f"\nMatches:")
        print(f"  Total in database:      {stats['matches_total']}")
        print(f"  With details cached:    {stats['matches_with_details']}")
        print(f"  Without details:        {stats['matches_without_details']}")
        print(f"\nMatch Details:")
        print(f"  With replay URL:        {stats['matches_with_replay_url']}")
        print(f"  Without replay URL:     {stats['matches_without_replay_url']}")
        print(f"  Parsed by OpenDota:     {stats['matches_parsed']}")
        print(f"\nDownloads:")
        print(f"  Tracked in database:    {stats['downloads_tracked']}")
        print(f"  Verified on disk:       {stats['downloads_on_disk']}")
        print("=" * 50 + "\n")
        
        # Suggestions
        if stats['matches_without_details'] > 0:
            print(f"TIP: Run 'python dota_replays.py' to fetch {stats['matches_without_details']} missing match details")
        if stats['matches_without_replay_url'] > 0:
            print(f"TIP: Run 'python dota_replays.py --refresh' to re-fetch {stats['matches_without_replay_url']} matches without replay URLs")

    def refresh_missing_replay_urls(self, limit: int | None = None) -> None:
        """Re-fetch match details for matches missing replay URLs."""
        matches = self.db.get_matches_without_replay_url(limit=limit)
        if not matches:
            logger.info("All matches have replay URLs")
            return

        logger.info(f"Re-fetching {len(matches)} matches without replay URLs...")
        success = 0
        still_missing = 0

        for match in tqdm(matches, desc="Refreshing"):
            match_id = match["match_id"]
            
            # Delete old details and re-fetch
            self.db.delete_match_details(match_id)
            details = self.client.get_match_details(match_id)
            
            if details:
                self.db.upsert_match_details(match_id, details)
                if details.get("replay_url"):
                    success += 1
                else:
                    still_missing += 1
                    logger.debug(f"Match {match_id} still has no replay URL")

        logger.info(f"Refreshed {len(matches)} matches: {success} now have URLs, {still_missing} still missing")
        if still_missing > 0:
            logger.info("Note: Old matches may not have replays available")

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
        "--scan",
        action="store_true",
        help="Scan replay directory and register existing files in database",
    )
    parser.add_argument(
        "--redownload",
        action="store_true",
        help="Re-download missing files (requires --verify)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show database statistics and status",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch match details for matches missing replay URLs",
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
        default=os.environ.get("DB_PATH", "./dota_replays.db"),
        help="Path to SQLite database (env: DB_PATH)",
    )
    parser.add_argument(
        "--replay-dir",
        type=str,
        default=os.environ.get("REPLAY_DIR", "./replays"),
        help="Directory to store replay files (env: REPLAY_DIR)",
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
        if args.status:
            # Status mode - show database stats
            app.show_status()
        elif args.refresh:
            # Refresh mode - re-fetch matches without replay URLs
            app.refresh_missing_replay_urls(limit=args.limit)
        elif args.scan:
            # Scan mode - discover existing files
            app.scan_replay_directory()
        elif args.verify:
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
