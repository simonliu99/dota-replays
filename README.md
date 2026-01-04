# DotA Replay Downloader

A script to download match replays and cache OpenDota parsed data for Dota 2 matches.

## Features

- **SQLite storage** - Fast, efficient local database
- **Multi-player support** - Track multiple players, update all at once
- **Parse monitoring** - Request and wait for OpenDota to parse matches
- **Disk verification** - Verify downloads exist and re-download missing files
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

### Verification

```bash
# Check all downloaded files exist on disk
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

| Argument           | Default             | Description                                                   |
| ------------------ | ------------------- | ------------------------------------------------------------- |
| `player_id`        | -                   | DotA player ID (optional, omit to update all tracked players) |
| `-n, --limit`      | None                | Limit number of matches to process                            |
| `--wait-for-parse` | False               | Wait for parse jobs to complete before fetching details       |
| `--verify`         | False               | Verify all downloaded files exist on disk                     |
| `--redownload`     | False               | Re-download missing files (requires `--verify`)               |
| `--api-key`        | `$OPENDOTA_API_KEY` | OpenDota API key for higher rate limits                       |
| `--db`             | `./dota_replays.db` | Path to SQLite database                                       |
| `--replay-dir`     | `./replays`         | Directory to store replay files                               |
| `-v, --verbose`    | False               | Enable debug logging                                          |

## Finding Your Player ID

1. Log into [OpenDota](https://www.opendota.com/)
2. Go to **My Profile**
3. Your URL will be: `https://www.opendota.com/players/<player_id>`

## Migration from v1

If you have existing `.pkl` files from the old version:

```bash
# Preview what will be migrated
python migrate_pkl_to_sqlite.py --dry-run

# Run migration
python migrate_pkl_to_sqlite.py

# Consolidate replay folders (moves replays-* into /replays)
python migrate_pkl_to_sqlite.py --consolidate-replays

# Re-discover downloaded files
python dota_replays.py --verify
```

## API Key (Optional)

Register for a free [OpenDota API key](https://www.opendota.com/api-keys) for higher rate limits:

```bash
# Via environment variable
export OPENDOTA_API_KEY=your-key-here
python dota_replays.py

# Or via command line
python dota_replays.py --api-key your-key-here
```
