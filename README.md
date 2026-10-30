# DotA Replay Downloader

A script to download match replays and cache OpenDota parsed data for Dota 2 matches.

## Features

- **SQLite storage** - Fast, efficient local database
- **Multi-player support** - Track multiple players, update all at once
- **Parse monitoring** - Request and wait for OpenDota to parse matches
- **Disk verification** - Verify downloads exist and re-download missing files
- **Configurable storage** - Store replays on NFS via .env configuration
- **Rate limiting** - Respects OpenDota API limits with retry logic
- **Standard library only** - No packages to install

## Requirements

Python 3.10 or later. The program uses the Python standard library alone.

## Installation

```bash
git clone https://github.com/simonliu99/dota-replays.git
cd dota-replays
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

### Troubleshooting

```bash
# Show database statistics
python dota_replays.py --status

# Re-fetch match details for matches missing replay URLs
python dota_replays.py --refresh
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
| `--parse-wait-budget SECONDS` | None                   | Cap the total parse wait of one run                           |
| `--status`         | False                             | Show database statistics and status                           |
| `--refresh`        | False                             | Re-fetch match details for matches missing replay URLs        |
| `--force-retry`    | False                             | Retry failed downloads within 21-day threshold                |
| `--db`             | `$DB_PATH` or `./dota_replays.db` | Path to SQLite database                                       |
| `--replay-dir`     | `$REPLAY_DIR` or `./replays`      | Directory to store replay files                               |
| `-v, --verbose`    | False                             | Enable debug logging                                          |

## Configuration

Copy `.env.example` to `.env` and customize:

```bash
cp .env.example .env
```

A `.env` file is read when the optional `python-dotenv` package is installed;
without it, set the same variables in the environment or pass the CLI flags.

| Variable           | Default             | Description                    |
| ------------------ | ------------------- | ------------------------------ |
| `REPLAY_DIR`       | `./replays`         | Replay storage (can be NFS)    |
| `DB_PATH`          | `./dota_replays.db` | SQLite database path           |

> **Note**: SQLite doesn't work well on NFS due to file locking. Keep `DB_PATH` on local storage and only use NFS for `REPLAY_DIR`.

The `OPENDOTA_BASE_URL` environment variable overrides the API root; the tests
point it at a local fake, and a normal run leaves it unset.

## Run under switchboard

A switchboard worker on the Maryland node runs these jobs from `jobs/`:
`update` on a weekly trigger, `verify` when the replays on disk need
checking, and `check` when the published match index needs comparing with
the database. The worker supplies the dataset's paths, and the entries never
create one, so a missing mount fails the job instead of writing to the local
disk.

| Job | Entry | What it does | Backstop | Quiet limit |
| --- | --- | --- | --- | --- |
| `update` | `jobs/update` | Updates every tracked player's 100 newest matches, rechecks 30 unparsed matches, writes a checked copy of the database, then publishes the match index | 10,800 s | 600 s |
| `verify` | `jobs/verify` | Prints `--status`, checks every recorded replay on disk, then publishes the match index | 1,800 s | 600 s |
| `check` | `jobs/check` | Compares the published match index with the database and exits 1 on a difference | 600 s | 300 s |

Each entry reads `SWITCHBOARD_BULK` (the node's local folder holding
`dota_replays.db`); `update` and `verify` also read `DOTA_REPLAY_DIR` (the
TrueNAS replay folder), and `update` reads `DOTA_DB_COPY_DIR` (a TrueNAS
folder for the copy). It exits 1 when one is unset or not a directory, and
when `python3` is below 3.10. The publish step also reads `SWITCHBOARD_HUB`
and the task secret `SWITCHBOARD_MODULE_TOKEN`.

`publish.py` writes the match index to the module's document store on the
hub: one document per player per month (`matches.<player_id>`, key
`y:<999999 - yyyymm>`), newest month first, plus `meta/players`,
`meta/heroes` and `meta/summary`, which it writes last. A month document
holds at most 256 KiB, so the index keeps whole months, newest first, while
the module's stored bytes stay at or under 12 MiB; a month over the value
limit is skipped and the run exits 1 naming it. Each run lists the store's
keys, writes only the documents whose value changed, and deletes the keys
the database no longer indexes, so a hub restored from an older copy is
repaired on the next run. `check` (`python3 publish.py --check`) lists the
same documents and exits 1 unless the stored index equals the database's,
including each match's `replay_file`.

`DOTA_REPLAY_DIR` and `DOTA_DB_COPY_DIR` must each name a folder inside the
mounted share, never the mount point itself: an unmounted mount point is an
empty local directory, which the entries would accept and write to.

`db_copy.py` writes `dota_replays.db.copy` on the local disk with SQLite's
backup API, checks it with `PRAGMA integrity_check`, copies its bytes to
`$DOTA_DB_COPY_DIR/dota_replays.db.part` and renames that to
`dota_replays.db`. No SQLite connection opens a file on the share, and a
failed run leaves the previous copy in place.

A gap longer than the 100 matches `update` lists, such as a skipped week, is
fetched by hand on the node: `python3 dota_replays.py --db "$SWITCHBOARD_BULK/dota_replays.db" --replay-dir "$DOTA_REPLAY_DIR"`
with no `-n`.

`switchboard/policy.example.toml` holds the worker's policy entries; add them
on the hub's Worker page, set `max_backstop_s` to at least 10,800 and the
`bulk-dota-replays` resource to 1.

### Moving the database to the node's local disk

The jobs keep the database at `$SWITCHBOARD_BULK/dota_replays.db` on the
Maryland node's local disk, because SQLite's locking is not safe on a network
share, and TrueNAS keeps a checked copy instead.

1. On the node, as the worker's user: `mkdir -p <local>/dota-replays`.
2. Stop anything writing the current database on the share.
3. Copy it while nothing writes: `sqlite3 <old>/dota_replays.db ".backup '<local>/dota-replays/dota_replays.db'"`, then check it with `sqlite3 '<local>/dota-replays/dota_replays.db' 'PRAGMA integrity_check;'` and expect `ok`.
4. Run `jobs/update` once: it adds the matches played since the last run, downloads missing replays and writes the first copy. Then run `jobs/verify`.
5. Keep the share's original until both jobs pass; the copy folder is a
   separate path, so the original is never overwritten.

### The weekly trigger

`switchboard/dev.dota-replays.update.plist` is a LaunchAgent for the dev Mac
that submits `update` every Thursday at 05:30 Pacific and never before 05:00.
launchd runs a calendar time missed while the Mac slept at its next wake, and
`--not-before` refuses a wake in the small hours, so that week's run waits for
the following Thursday.

1. Mint an `sba_` agent token for the Mac on the hub's Access page and save it to `~/.config/switchboard/dota-replays-submit.token`, mode `0600`.
2. Edit `switchboard/dev.dota-replays.update.plist`: `--hub` names the hub, and
   the token and script paths must match the Mac.
3. Install it:

   ```bash
   cp switchboard/dev.dota-replays.update.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.dota-replays.update.plist
   ```

Its output goes to `~/Library/Logs/dota-replays-update.log`. The same submit
by hand:

```bash
scripts/switchboard-submit --hub https://<hub> --token-file ~/.config/switchboard/dota-replays-submit.token update
```

The helper keeps a bare clone in `~/.cache/dota-replays-submit/repo.git`,
fetches `master` before every submit, and runs the commit `master` points at.

## Finding Your Player ID

1. Log into [OpenDota](https://www.opendota.com/)
2. Go to **My Profile**
3. Your URL will be: `https://www.opendota.com/players/<player_id>`
