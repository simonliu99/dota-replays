# DotA Replay Downloader

A script to download match replays and cache OpenDota parsed data for Dota 2 matches.

## Features

- **SQLite storage** - Fast, efficient local database
- **Multi-player support** - Track multiple players, update all at once
- **Parse monitoring** - Request and wait for OpenDota to parse matches
- **Disk verification** - Verify downloads exist and re-download missing files
- **Configurable storage** - Store replays on NFS via .env configuration
- **Rate limiting** - Respects OpenDota API limits with retry logic

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Basic Commands

```bash
# Add and update a player
python dota_replays.py <player_id>

# Update all tracked players
python dota_replays.py

# Limit to N most recent matches
python dota_replays.py <player_id> -n 10
```

### Disk Management

```bash
# Scan replay directory and register existing files in database
python dota_replays.py --scan

# Verify all tracked files exist on disk
python dota_replays.py --verify

# Re-download missing files
python dota_replays.py --verify --redownload
```

### Parse Monitoring

```bash
# Wait for OpenDota to parse matches before fetching details
python dota_replays.py --wait-for-parse
```

## CLI Reference

| Argument           | Default                           | Description                                                   |
| ------------------ | --------------------------------- | ------------------------------------------------------------- |
| `player_id`        | -                                 | DotA player ID (optional, omit to update all tracked players) |
| `-n, --limit`      | None                              | Limit number of matches to process                            |
| `--wait-for-parse` | False                             | Wait for parse jobs to complete before fetching details       |
| `--scan`           | False                             | Scan replay directory and register existing files in database |
| `--verify`         | False                             | Verify all tracked files exist on disk                        |
| `--redownload`     | False                             | Re-download missing files (requires `--verify`)               |
| `--status`         | False                             | Show database statistics and status                           |
| `--refresh`        | False                             | Re-fetch match details for matches missing replay URLs        |
| `--api-key`        | `$OPENDOTA_API_KEY`               | OpenDota API key for higher rate limits                       |
| `--db`             | `$DB_PATH` or `./dota_replays.db` | Path to SQLite database                                       |
| `--replay-dir`     | `$REPLAY_DIR` or `./replays`      | Directory to store replay files                               |
| `-v, --verbose`    | False                             | Enable debug logging                                          |

## Configuration

Copy `.env.example` to `.env` and customize:

```bash
cp .env.example .env
```

| Variable           | Default             | Description                    |
| ------------------ | ------------------- | ------------------------------ |
| `OPENDOTA_API_KEY` | -                   | API key for higher rate limits |
| `REPLAY_DIR`       | `./replays`         | Replay storage (can be NFS)    |
| `DB_PATH`          | `./dota_replays.db` | SQLite database path           |

> **Note**: SQLite doesn't work well on NFS due to file locking. Keep `DB_PATH` on local storage and only use NFS for `REPLAY_DIR`.

## Finding Your Player ID

1. Log into [OpenDota](https://www.opendota.com/)
2. Go to **My Profile**
3. Your URL will be: `https://www.opendota.com/players/<player_id>`

## Migration from v1

If you have existing `.pkl` files from the old version:

```bash
# 1. Preview what will be migrated
python migrate_pkl_to_sqlite.py --dry-run

# 2. Run migration
python migrate_pkl_to_sqlite.py

# 3. Consolidate replay folders (moves replays-* into configured REPLAY_DIR)
python migrate_pkl_to_sqlite.py --consolidate-replays

# 4. Scan and register existing replay files
python dota_replays.py --scan

# 5. Verify all files (optional)
python dota_replays.py --verify
```

## API Key (Optional)

Register for a free [OpenDota API key](https://www.opendota.com/api-keys) for higher rate limits:

```bash
# Via .env file (recommended)
echo "OPENDOTA_API_KEY=your-key-here" >> .env

# Or via command line
python dota_replays.py --api-key your-key-here
```
