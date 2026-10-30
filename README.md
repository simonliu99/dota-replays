# dota-replays

dota-replays downloads Dota 2 match replays for the players you track and keeps
their match history in a SQLite database, using the free OpenDota API. A
switchboard worker runs it every week and publishes the match list to
switchboard's dota-replays page.

## Features

- Tracks several players and updates them all in one run.
- Asks OpenDota to parse new matches and waits for the parse before fetching
  match details.
- Downloads each match's replay, and retries failed downloads and unparsed
  matches for 21 days.
- Checks the replays on disk and downloads missing ones again.
- Stays within OpenDota's free rate limits, with retries.
- Uses the Python standard library only.

## Getting started

Requires Python 3.10 or later as `python3`; macOS's own `python3` is 3.9, so
check `python3 --version` first.

```bash
git clone https://github.com/simonliu99/dota-replays.git
cd dota-replays
python3 dota_replays.py <player_id>   # add a player and fetch their matches
python3 dota_replays.py               # update every tracked player
python3 dota_replays.py --status      # show database statistics
```

The player ID is the number at the end of your OpenDota profile URL,
`https://www.opendota.com/players/<player_id>`. `python3 dota_replays.py --help`
lists every option, such as `-n` to limit a run to the newest matches and
`--verify --redownload` to fetch replay files missing from disk.

The database defaults to `./dota_replays.db` and replays to `./replays`. Set
`DB_PATH` and `REPLAY_DIR`, or pass `--db` and `--replay-dir`. `.env.example`
lists the variables; a `.env` file is read only when the optional
`python-dotenv` package is installed.

## Technical notes

- The switchboard jobs, the worker's setup, the weekly trigger and moving the
  database to the worker's disk are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- Keep the database on a local disk: SQLite's file locking is not safe on NFS.
  Only the replay folder may be on a network share.
- `dota_replays.py` is the command line, `opendota_client.py` calls the API,
  `database.py` holds the SQLite schema, `publish.py` writes the match index
  to switchboard and `db_copy.py` writes the checked copy of the database.
- Run the tests with `python3 -m unittest discover -v` under Python 3.10 or
  later. They point `OPENDOTA_BASE_URL` at a local fake and never call
  OpenDota.
- `migrate_pkl_to_sqlite.py` converts the `.pkl` files of version 1; run it
  with `--dry-run` first.
