#!/usr/bin/env python3
"""
DotA Replay Downloader

Downloads match replays and caches OpenDota parsed data.
"""

import argparse
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from database import Database
from opendota_client import OpenDotaClient

# Seconds a replay download may stay silent before it fails.
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_CHUNK_SIZE = 8192

# Log a progress line every this many items instead of a live progress bar.
PROGRESS_INTERVAL = 25

# One parse wait polls for at most this long, as poll_parse_completion defaults.
PARSE_WAIT_TIMEOUT = 300

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load .env when python-dotenv is installed; the program runs without it."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _progress(items: list, description: str):
    """Yield items, logging a progress line every 25 items and at the end."""
    total = len(items)
    for position, item in enumerate(items, start=1):
        if position % PROGRESS_INTERVAL == 0 or position == total:
            logger.info(f"{description}: {position}/{total}")
        yield item


def _describe_download_error(error: Exception) -> str:
    """Return a short message for a failed replay download."""
    if isinstance(error, urllib.error.HTTPError):
        return f"{error.code} {error.reason}"
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError):
        return "Timeout"
    return str(error)[:50]


class PlayerUpdate(NamedTuple):
    """What one player's update fetched and how many detail fetches failed."""

    player_fetched: bool
    matches_fetched: bool
    failed_details: int = 0

    @property
    def succeeded(self) -> bool:
        """Return whether the player, its match list and every detail came in."""
        return self.player_fetched and self.matches_fetched and not self.failed_details


def _plural(noun: str, plural: str, count: int) -> str:
    """Return "1 failed player update" or "2 failed player updates"."""
    return f"{count} {noun if count == 1 else plural}"


class DotaReplays:
    """Main application class for downloading DotA replays."""

    # Only process matches from the last N days
    RECENT_DAYS = 14

    def __init__(
        self,
        db: Database,
        client: OpenDotaClient,
        replay_dir: Path,
        wait_for_parse: bool = True,
        force_refresh: bool = False,
        parse_wait_budget: int | None = None,
    ):
        self.db = db
        self.client = client
        self.replay_dir = replay_dir
        self.wait_for_parse = wait_for_parse
        self.force_refresh = force_refresh
        self.force_retry = False  # Set via main() when --force-retry is used
        self.parse_wait_budget = parse_wait_budget
        self._parse_wait_deadline: float | None = None
        self.replay_dir.mkdir(parents=True, exist_ok=True)

    def _parse_wait_remaining(self) -> int:
        """Return the whole seconds of this run's parse wait budget that remain."""
        if self._parse_wait_deadline is None:
            self._parse_wait_deadline = time.monotonic() + self.parse_wait_budget
        remaining = self._parse_wait_deadline - time.monotonic()
        if remaining <= 0:
            return 0
        return min(PARSE_WAIT_TIMEOUT, int(remaining))

    def _poll_parse(self, match_id: int, job_id: int) -> bool:
        """Wait for a parse job within this run's remaining wait budget.

        With no budget the wait is today's, one poll for up to
        PARSE_WAIT_TIMEOUT seconds. Once the budget is spent, the run requests
        the parse and moves on, so the match stays unparsed for the next
        recheck.
        """
        if self.parse_wait_budget is None:
            logger.info(f"Waiting for parse of match {match_id}...")
            return self.client.poll_parse_completion(job_id)
        timeout = self._parse_wait_remaining()
        if timeout <= 0:
            logger.info(
                f"Parse wait budget spent; match {match_id} is left for the recheck"
            )
            return False
        logger.info(f"Waiting up to {timeout}s for parse of match {match_id}...")
        return self.client.poll_parse_completion(job_id, timeout=timeout)

    def update_player(self, player_id: int, limit: int | None = None) -> PlayerUpdate:
        """Update match data for a single player and report what was fetched."""
        logger.info(f"Updating player {player_id}")

        # Validate and register player
        player = self.client.get_player(player_id)
        if not player or "profile" not in player:
            logger.error(f"Player {player_id} not found")
            return PlayerUpdate(player_fetched=False, matches_fetched=False)

        persona_name = player.get("profile", {}).get("personaname")
        self.db.upsert_player(player_id, persona_name)
        logger.info(f"Player: {persona_name} ({player_id})")

        # Fetch matches
        logger.info("Fetching match list...")
        matches = self.client.get_player_matches(player_id, limit=limit)
        if matches is None:
            logger.error(f"Failed to fetch the match list for player {player_id}")
            return PlayerUpdate(player_fetched=True, matches_fetched=False)

        if not matches:
            logger.warning("No matches found")
            return PlayerUpdate(player_fetched=True, matches_fetched=True)

        new_count = self.db.insert_matches(player_id, matches)
        logger.info(f"Found {len(matches)} matches, {new_count} new")

        # Get details for matches without cache
        failed_details = self._fetch_match_details(player_id, limit)

        # Download replays
        self._download_replays(force_retry=self.force_retry)

        return PlayerUpdate(
            player_fetched=True,
            matches_fetched=True,
            failed_details=failed_details,
        )

    def _fetch_match_details(self, player_id: int, limit: int | None = None) -> int:
        """Fetch and cache match details, returning how many fetches failed."""
        if self.force_refresh:
            logger.info(f"Force refreshing details for last {limit or 'all'} matches...")
            matches = self.db.get_recent_matches(player_id, limit)
        else:
            matches = self.db.get_matches_without_details(player_id, limit)

        if not matches:
            logger.info("All matches have cached details" if not self.force_refresh else "No matches found")
            return 0

        logger.info(f"Fetching details for {len(matches)} matches...")
        # OpenDota recommends 21 days as the threshold for replay availability
        cutoff = datetime.now() - timedelta(days=21)
        failed = 0

        for match in _progress(matches, "Fetching details"):
            match_id = match["match_id"]
            start_time = datetime.fromtimestamp(match["start_time"])

            # 1. Fetch match details
            details = self.client.get_match_details(match_id)
            if not details:
                failed += 1
                logger.error(f"Failed to fetch details for match {match_id}")
                continue

            # 2. Check if parsed (version field)
            version = details.get("version")
            if version is None and start_time > cutoff:
                logger.info(f"Match {match_id} is unparsed, requesting parse...")
                job_id = self.client.request_parse(match_id)
                if job_id:
                    self.db.record_parse_request(match_id, job_id)
                    if self.wait_for_parse:
                        if self._poll_parse(match_id, job_id):
                            self.db.mark_parse_complete(match_id)
                            # 3. Final fetch after parse
                            details = self.client.get_match_details(match_id) or details

            # 4. Save to database
            self.db.upsert_match_details(match_id, details)

        return failed

    def recheck_unparsed_matches(self, limit: int | None = None) -> None:
        """Re-check matches that are in the database but not fully parsed."""
        matches = self.db.get_unparsed_matches(limit=limit, max_age_days=21)
        if not matches:
            logger.info("No unparsed matches found within 21-day threshold")
            return

        logger.info(f"Re-checking {len(matches)} unparsed matches...")
        for match in _progress(matches, "Re-checking parses"):
            match_id = match["match_id"]
            
            # Request parse
            job_id = self.client.request_parse(match_id)
            if job_id:
                self.db.record_parse_request(match_id, job_id)
                if self.wait_for_parse:
                    if self._poll_parse(match_id, job_id):
                        self.db.mark_parse_complete(match_id)
                        details = self.client.get_match_details(match_id)
                        if details:
                            self.db.upsert_match_details(match_id, details)
                else:
                    # If not waiting, we still want to refresh details later
                    # but for now we've triggered the parse
                    pass

    def _download_replay(self, match_id: int, replay_url: str, filepath: Path) -> int:
        """Download one replay to filepath and return the bytes written.

        The body is written to <filename>.part and renamed only once the whole
        body is written, so a failed download leaves no replay file behind.
        """
        part_path = filepath.with_name(f"{filepath.name}.part")
        started = time.monotonic()
        written = 0
        try:
            request = urllib.request.Request(
                replay_url, headers={"User-Agent": "dota-replays/1.0"}
            )
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                expected_header = response.headers.get("Content-Length")
                expected = (
                    int(expected_header)
                    if expected_header and expected_header.isdigit()
                    else None
                )
                with open(part_path, "wb") as handle:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
            if expected is not None and expected != written:
                raise OSError(f"incomplete download: {written} of {expected} bytes")
            os.replace(part_path, filepath)
        except BaseException:
            part_path.unlink(missing_ok=True)
            raise

        logger.info(
            f"Downloaded replay for match {match_id}: {written} bytes in "
            f"{time.monotonic() - started:.1f}s"
        )
        return written

    def _download_replays(self, force_retry: bool = False) -> None:
        """Download replay files for matches with replay URLs."""
        matches = self.db.get_matches_with_replay_url(downloaded=False)
        if not matches:
            logger.info("No new replays to download")
            return

        # Filter by retry limits (unless force_retry within 21 days)
        eligible = []
        skipped = 0
        cutoff = datetime.now() - timedelta(days=21)
        
        for match in matches:
            match_id = match["match_id"]
            start_time = match.get("start_time", 0)
            match_date = datetime.fromtimestamp(start_time) if start_time else datetime.min
            is_within_threshold = match_date > cutoff
            
            # Skip matches older than 21 days (replays expire)
            if not is_within_threshold:
                skipped += 1
                continue
            
            if force_retry or self.db.should_retry_download(match_id, start_time):
                eligible.append(match)
            else:
                skipped += 1

        if skipped > 0:
            logger.info(f"Skipped {skipped} matches (retry limit reached or too old)")
        
        if not eligible:
            logger.info("No eligible replays to download")
            return

        logger.info(f"Downloading {len(eligible)} replays...")
        success = 0
        failed = 0
        errors = []  # Collect errors to print after the downloads

        for match in _progress(eligible, "Downloading"):
            match_id = match["match_id"]
            replay_url = match["replay_url"]

            if not replay_url:
                continue

            filename = replay_url.split("/")[-1]
            filepath = self.replay_dir / filename

            if filepath.exists():
                # Already on disk, just record it
                file_size = filepath.stat().st_size
                self.db.record_download(match_id, filename, file_size)
                self.db.clear_download_attempts(match_id)
                logger.info(
                    f"Replay for match {match_id} already on disk: {file_size} bytes"
                )
                continue

            try:
                file_size = self._download_replay(match_id, replay_url, filepath)
                self.db.record_download(match_id, filename, file_size)
                self.db.clear_download_attempts(match_id)
                success += 1
            except Exception as e:
                errors.append((match_id, _describe_download_error(e)))
                self.db.record_download_attempt(match_id, str(e)[:200])
                failed += 1

        # Print summary after the downloads
        logger.info(f"Downloaded {success} replays, {failed} failed")
        
        # Show failed downloads if any
        if errors:
            logger.warning("Failed downloads:")
            for match_id, error in errors:
                logger.warning(f"  Match {match_id}: {error}")

    def verify_downloads(self, redownload: bool = False) -> None:
        """Verify all downloads exist on disk."""
        downloads = self.db.get_downloads()
        if not downloads:
            logger.info("No downloads to verify")
            return

        logger.info(f"Verifying {len(downloads)} downloads...")
        missing = []

        for dl in _progress(downloads, "Verifying"):
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

        for filepath in _progress(replay_files, "Scanning"):
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
        failed_stats = self.db.get_failed_downloads_stats()
        
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
        print(f"\nFailed Downloads:")
        print(f"  Total failed attempts:  {failed_stats['total_failed']}")
        print(f"  Exhausted retries:      {failed_stats['exhausted_retries']}")
        print("=" * 50 + "\n")
        
        # Suggestions
        if stats['matches_without_details'] > 0:
            print(f"TIP: Run 'python dota_replays.py' to fetch {stats['matches_without_details']} missing match details")
        if stats['matches_without_replay_url'] > 0:
            print(f"TIP: Run 'python dota_replays.py --refresh' to re-fetch {stats['matches_without_replay_url']} matches without replay URLs")
        if failed_stats['exhausted_retries'] > 0:
            print(f"TIP: Run 'python dota_replays.py --force-retry' to retry {failed_stats['exhausted_retries']} failed downloads (within 21 days)")

    def refresh_missing_replay_urls(self, limit: int | None = None) -> None:
        """Re-fetch match details for matches missing replay URLs."""
        matches = self.db.get_matches_without_replay_url(limit=limit)
        if not matches:
            logger.info("All matches have replay URLs")
            return

        logger.info(f"Re-fetching {len(matches)} matches without replay URLs...")
        success = 0
        still_missing = 0

        for match in _progress(matches, "Refreshing"):
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

        for dl in _progress(missing, "Re-downloading"):
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
                file_size = self._download_replay(match_id, replay_url, filepath)
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
        "--no-wait-for-parse",
        dest="wait_for_parse",
        action="store_false",
        default=True,
        help="Do not wait for parse jobs to complete (default: wait is enabled)",
    )
    parser.add_argument(
        "--parse-wait-budget",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Cap the total parse wait of one run in seconds (default: no cap)",
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
        "--recheck-parsing",
        action="store_true",
        help="Re-check unparsed matches within 21-day threshold",
    )
    parser.add_argument(
        "--force-retry",
        action="store_true",
        help="Retry failed downloads within 21-day threshold (resets attempt counts)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force re-fetch match details for the last N matches (use with -n)",
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
    _load_dotenv()
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.redownload and not args.verify:
        logger.error("--redownload requires --verify")
        sys.exit(1)

    # Initialize components
    db = Database(args.db)
    client = OpenDotaClient(
        api_key=args.api_key,
        base_url=os.environ.get("OPENDOTA_BASE_URL"),
    )
    replay_dir = Path(args.replay_dir)

    app = DotaReplays(
        db=db,
        client=client,
        replay_dir=replay_dir,
        wait_for_parse=args.wait_for_parse,
        force_refresh=args.force_refresh,
        parse_wait_budget=args.parse_wait_budget,
    )
    app.force_retry = args.force_retry

    updates: list[PlayerUpdate] = []

    try:
        if args.status:
            # Status mode - show database stats
            app.show_status()
        elif args.refresh:
            # Refresh mode - re-fetch matches without replay URLs
            app.refresh_missing_replay_urls(limit=args.limit)
        elif args.recheck_parsing:
            # Re-check parsing for unparsed matches
            app.recheck_unparsed_matches(limit=args.limit)
        elif args.scan:
            # Scan mode - discover existing files
            app.scan_replay_directory()
        elif args.verify:
            # Verification mode
            app.verify_downloads(redownload=args.redownload)
        elif args.player_id:
            # Update specific player
            updates.append(app.update_player(args.player_id, limit=args.limit))
        else:
            # Update all tracked players
            players = db.get_all_players()
            if not players:
                logger.info("No players in database. Add a player by running:")
                logger.info("  python dota_replays.py <player_id>")
                sys.exit(0)

            logger.info(f"Updating {len(players)} tracked players...")
            for player in players:
                updates.append(app.update_player(player["player_id"], limit=args.limit))

    finally:
        db.close()

    failed_players = sum(1 for update in updates if not update.succeeded)
    failed_details = sum(update.failed_details for update in updates)
    if failed_players or failed_details:
        players_text = _plural(
            "failed player update", "failed player updates", failed_players
        )
        details_text = _plural(
            "failed match detail fetch", "failed match detail fetches", failed_details
        )
        logger.error(f"Finished with {players_text} and {details_text}")
        sys.exit(1)

    logger.info("Done!")


if __name__ == "__main__":
    main()
