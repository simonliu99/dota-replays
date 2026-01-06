"""SQLite database module for DotA Replays."""

import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class Database:
    """SQLite database for storing match data and download tracking."""

    def __init__(self, db_path: str = "./dota_replays.db"):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Enable WAL mode for better concurrent access
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    # Current schema version - increment when adding migrations
    SCHEMA_VERSION = 2

    def _create_tables(self) -> None:
        """Create/migrate database tables."""
        # Create schema version table first
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        
        # Get current version
        cursor = self.conn.execute("SELECT MAX(version) FROM schema_version")
        row = cursor.fetchone()
        current_version = row[0] if row[0] is not None else 0
        
        # Detect existing v1 database (has tables but no version)
        if current_version == 0:
            cursor = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='players'"
            )
            if cursor.fetchone():
                # Existing database without version tracking - assume v1
                logger.info("Detected existing v1 database, setting version")
                self.conn.execute("INSERT INTO schema_version (version) VALUES (1)")
                self.conn.commit()
                current_version = 1
        
        # Run migrations
        if current_version < self.SCHEMA_VERSION:
            logger.info(f"Migrating database from v{current_version} to v{self.SCHEMA_VERSION}")
            self._run_migrations(current_version)
    
    def _run_migrations(self, from_version: int) -> None:
        """Run all migrations from current version to latest."""
        migrations = {
            1: self._migrate_v1,  # Initial schema
            2: self._migrate_v2,  # Add download_attempts table
        }
        
        for version in range(from_version + 1, self.SCHEMA_VERSION + 1):
            if version in migrations:
                logger.info(f"Running migration v{version}...")
                migrations[version]()
                self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                self.conn.commit()
                logger.info(f"Migration v{version} complete")
    
    def _migrate_v1(self) -> None:
        """Initial schema - create all base tables."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                player_id INTEGER PRIMARY KEY,
                persona_name TEXT,
                last_updated TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL,
                start_time INTEGER NOT NULL,
                duration INTEGER,
                hero_id INTEGER,
                kills INTEGER,
                deaths INTEGER,
                assists INTEGER,
                radiant_win BOOLEAN,
                player_slot INTEGER,
                FOREIGN KEY (player_id) REFERENCES players(player_id)
            );

            CREATE TABLE IF NOT EXISTS match_details (
                match_id INTEGER PRIMARY KEY,
                data JSON NOT NULL,
                replay_url TEXT,
                is_parsed BOOLEAN DEFAULT FALSE,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );

            CREATE TABLE IF NOT EXISTS downloads (
                match_id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL,
                file_size INTEGER,
                on_disk BOOLEAN DEFAULT TRUE,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP,
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );

            CREATE TABLE IF NOT EXISTS parse_jobs (
                match_id INTEGER PRIMARY KEY,
                job_id INTEGER,
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );

            CREATE INDEX IF NOT EXISTS idx_matches_player_id ON matches(player_id);
            CREATE INDEX IF NOT EXISTS idx_matches_start_time ON matches(start_time);
            CREATE INDEX IF NOT EXISTS idx_downloads_on_disk ON downloads(on_disk);
        """)
    
    def _migrate_v2(self) -> None:
        """Add download_attempts table for tracking failed downloads."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS download_attempts (
                match_id INTEGER PRIMARY KEY,
                attempt_count INTEGER DEFAULT 0,
                last_attempt_at TIMESTAMP,
                last_error TEXT,
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );
        """)

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    # ==================== Player Operations ====================

    def get_all_players(self) -> list[dict]:
        """Get all tracked players."""
        cursor = self.conn.execute("SELECT * FROM players")
        return [dict(row) for row in cursor.fetchall()]

    def upsert_player(self, player_id: int, persona_name: str | None = None) -> None:
        """Insert or update a player."""
        self.conn.execute("""
            INSERT INTO players (player_id, persona_name, last_updated)
            VALUES (?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                persona_name = COALESCE(excluded.persona_name, persona_name),
                last_updated = excluded.last_updated
        """, (player_id, persona_name, datetime.now().isoformat()))
        self.conn.commit()

    # ==================== Match Operations ====================

    def get_match_ids_for_player(self, player_id: int) -> set[int]:
        """Get all match IDs for a player."""
        cursor = self.conn.execute(
            "SELECT match_id FROM matches WHERE player_id = ?", 
            (player_id,)
        )
        return {row[0] for row in cursor.fetchall()}

    def insert_matches(self, player_id: int, matches: list[dict]) -> int:
        """Insert matches, returns count of new matches inserted."""
        existing = self.get_match_ids_for_player(player_id)
        new_matches = [m for m in matches if m["match_id"] not in existing]
        
        for match in new_matches:
            self.conn.execute("""
                INSERT OR IGNORE INTO matches 
                (match_id, player_id, start_time, duration, hero_id, kills, deaths, assists, radiant_win, player_slot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                match["match_id"],
                player_id,
                match.get("start_time"),
                match.get("duration"),
                match.get("hero_id"),
                match.get("kills"),
                match.get("deaths"),
                match.get("assists"),
                match.get("radiant_win"),
                match.get("player_slot"),
            ))
        self.conn.commit()
        return len(new_matches)

    def get_matches_without_details(self, player_id: int | None = None, limit: int | None = None) -> list[dict]:
        """Get matches that don't have cached details."""
        query = """
            SELECT m.* FROM matches m
            LEFT JOIN match_details md ON m.match_id = md.match_id
            WHERE md.match_id IS NULL
        """
        params: list[Any] = []
        if player_id:
            query += " AND m.player_id = ?"
            params.append(player_id)
        query += " ORDER BY m.start_time DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        
        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_recent_matches(self, player_id: int, limit: int | None = None) -> list[dict]:
        """Get most recent matches for a player."""
        query = "SELECT * FROM matches WHERE player_id = ? ORDER BY start_time DESC"
        params: list[Any] = [player_id]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        
        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    # ==================== Match Details Operations ====================

    def get_match_details(self, match_id: int) -> dict | None:
        """Get cached match details."""
        cursor = self.conn.execute(
            "SELECT data FROM match_details WHERE match_id = ?",
            (match_id,)
        )
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None

    def upsert_match_details(self, match_id: int, data: dict) -> None:
        """Insert or update match details."""
        replay_url = data.get("replay_url")
        # Check if parsed (has player ability upgrades, gold/xp arrays, etc.)
        is_parsed = bool(data.get("players") and 
                        data["players"][0].get("ability_upgrades_arr"))
        
        self.conn.execute("""
            INSERT INTO match_details (match_id, data, replay_url, is_parsed, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                data = excluded.data,
                replay_url = excluded.replay_url,
                is_parsed = excluded.is_parsed,
                fetched_at = excluded.fetched_at
        """, (match_id, json.dumps(data), replay_url, is_parsed, datetime.now().isoformat()))
        self.conn.commit()

    def get_matches_with_replay_url(self, downloaded: bool = False) -> list[dict]:
        """Get matches that have replay URLs."""
        if downloaded:
            # Include downloaded
            query = """
                SELECT md.match_id, md.replay_url, m.start_time 
                FROM match_details md
                JOIN matches m ON md.match_id = m.match_id
                WHERE md.replay_url IS NOT NULL
            """
        else:
            # Exclude already downloaded
            query = """
                SELECT md.match_id, md.replay_url, m.start_time 
                FROM match_details md
                JOIN matches m ON md.match_id = m.match_id
                LEFT JOIN downloads d ON md.match_id = d.match_id
                WHERE md.replay_url IS NOT NULL AND d.match_id IS NULL
            """
        cursor = self.conn.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    # ==================== Download Operations ====================

    def record_download(self, match_id: int, filename: str, file_size: int | None = None) -> None:
        """Record a successful download."""
        self.conn.execute("""
            INSERT INTO downloads (match_id, filename, file_size, on_disk, downloaded_at, verified_at)
            VALUES (?, ?, ?, TRUE, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                filename = excluded.filename,
                file_size = excluded.file_size,
                on_disk = TRUE,
                verified_at = excluded.verified_at
        """, (match_id, filename, file_size, datetime.now().isoformat(), datetime.now().isoformat()))
        self.conn.commit()

    def get_downloads(self, on_disk_only: bool = False) -> list[dict]:
        """Get all download records."""
        query = "SELECT * FROM downloads"
        if on_disk_only:
            query += " WHERE on_disk = TRUE"
        cursor = self.conn.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def update_on_disk_status(self, match_id: int, on_disk: bool) -> None:
        """Update the on_disk status for a download."""
        self.conn.execute("""
            UPDATE downloads SET on_disk = ?, verified_at = ?
            WHERE match_id = ?
        """, (on_disk, datetime.now().isoformat(), match_id))
        self.conn.commit()

    def get_downloads_missing_from_disk(self) -> list[dict]:
        """Get downloads marked as on_disk but may need verification."""
        cursor = self.conn.execute("SELECT * FROM downloads WHERE on_disk = FALSE")
        return [dict(row) for row in cursor.fetchall()]

    # ==================== Download Attempt Tracking ====================

    def record_download_attempt(self, match_id: int, error: str | None = None) -> None:
        """Record a failed download attempt."""
        self.conn.execute("""
            INSERT INTO download_attempts (match_id, attempt_count, last_attempt_at, last_error)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                attempt_count = attempt_count + 1,
                last_attempt_at = excluded.last_attempt_at,
                last_error = excluded.last_error
        """, (match_id, datetime.now().isoformat(), error))
        self.conn.commit()

    def get_download_attempt(self, match_id: int) -> dict | None:
        """Get download attempt info for a match."""
        cursor = self.conn.execute(
            "SELECT * FROM download_attempts WHERE match_id = ?",
            (match_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def should_retry_download(self, match_id: int, match_start_time: int, max_age_days: int = 21) -> bool:
        """
        Check if a download should be retried.
        
        Rules:
        - If match is older than max_age_days: max 1 attempt
        - If match is newer: max 2 attempts
        """
        from datetime import datetime, timedelta
        
        match_date = datetime.fromtimestamp(match_start_time)
        age = datetime.now() - match_date
        is_old = age > timedelta(days=max_age_days)
        
        attempt = self.get_download_attempt(match_id)
        if not attempt:
            return True  # Never tried
        
        attempt_count = attempt["attempt_count"]
        max_attempts = 1 if is_old else 2
        
        return attempt_count < max_attempts

    def clear_download_attempts(self, match_id: int) -> None:
        """Clear download attempts for a match (after successful download)."""
        self.conn.execute("DELETE FROM download_attempts WHERE match_id = ?", (match_id,))
        self.conn.commit()

    def get_failed_downloads_stats(self) -> dict:
        """Get stats about failed downloads."""
        cursor = self.conn.execute("SELECT COUNT(*) FROM download_attempts")
        total = cursor.fetchone()[0]
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM download_attempts WHERE attempt_count >= 2")
        exhausted = cursor.fetchone()[0]
        
        return {"total_failed": total, "exhausted_retries": exhausted}

    # ==================== Parse Job Operations ====================

    def record_parse_request(self, match_id: int, job_id: int) -> None:
        """Record a parse request."""
        self.conn.execute("""
            INSERT INTO parse_jobs (match_id, job_id, requested_at)
            VALUES (?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                job_id = excluded.job_id,
                requested_at = excluded.requested_at,
                completed_at = NULL
        """, (match_id, job_id, datetime.now().isoformat()))
        self.conn.commit()

    def mark_parse_complete(self, match_id: int) -> None:
        """Mark a parse job as complete."""
        self.conn.execute("""
            UPDATE parse_jobs SET completed_at = ?
            WHERE match_id = ?
        """, (datetime.now().isoformat(), match_id))
        self.conn.commit()

    def get_pending_parse_jobs(self) -> list[dict]:
        """Get parse jobs that haven't completed."""
        cursor = self.conn.execute(
            "SELECT * FROM parse_jobs WHERE completed_at IS NULL"
        )
        return [dict(row) for row in cursor.fetchall()]

    # ==================== Stats/Audit Operations ====================

    def get_stats(self) -> dict:
        """Get database statistics for audit."""
        stats = {}
        
        # Player count
        cursor = self.conn.execute("SELECT COUNT(*) FROM players")
        stats["players"] = cursor.fetchone()[0]
        
        # Match counts
        cursor = self.conn.execute("SELECT COUNT(*) FROM matches")
        stats["matches_total"] = cursor.fetchone()[0]
        
        # Match details
        cursor = self.conn.execute("SELECT COUNT(*) FROM match_details")
        stats["matches_with_details"] = cursor.fetchone()[0]
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM match_details WHERE replay_url IS NOT NULL")
        stats["matches_with_replay_url"] = cursor.fetchone()[0]
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM match_details WHERE replay_url IS NULL")
        stats["matches_without_replay_url"] = cursor.fetchone()[0]
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM match_details WHERE is_parsed = TRUE")
        stats["matches_parsed"] = cursor.fetchone()[0]
        
        # Downloads
        cursor = self.conn.execute("SELECT COUNT(*) FROM downloads")
        stats["downloads_tracked"] = cursor.fetchone()[0]
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM downloads WHERE on_disk = TRUE")
        stats["downloads_on_disk"] = cursor.fetchone()[0]
        
        # Matches without details
        cursor = self.conn.execute("""
            SELECT COUNT(*) FROM matches m
            LEFT JOIN match_details md ON m.match_id = md.match_id
            WHERE md.match_id IS NULL
        """)
        stats["matches_without_details"] = cursor.fetchone()[0]
        
        return stats

    def get_matches_without_replay_url(self, limit: int | None = None) -> list[dict]:
        """Get matches that have details but no replay URL (candidates for re-fetch)."""
        query = """
            SELECT md.match_id, md.fetched_at FROM match_details md
            WHERE md.replay_url IS NULL
            ORDER BY md.fetched_at ASC
        """
        if limit:
            query += f" LIMIT {limit}"
        cursor = self.conn.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def delete_match_details(self, match_id: int) -> None:
        """Delete match details to allow re-fetching."""
        self.conn.execute("DELETE FROM match_details WHERE match_id = ?", (match_id,))
        self.conn.commit()

