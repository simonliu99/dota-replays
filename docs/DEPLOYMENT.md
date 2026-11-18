# Deployment

A switchboard worker on the Maryland node runs the jobs in `jobs/`. The
database lives on the node's local disk at `$SWITCHBOARD_BULK/dota_replays.db`,
because SQLite's locking is not safe on a network share. The replays and
dated backups of the database are on TrueNAS. A failed backup leaves earlier
backups in place. The hub schedule `dota-replays-update` submits `update` once
a week.

## Jobs

| Job | Entry | What it does | Backstop | Quiet limit |
| --- | --- | --- | --- | --- |
| `update` | `jobs/update` | Updates every tracked player's 100 newest matches, rechecks 30 unparsed matches, writes a dated database backup and publishes the match index | 10,800 s | 600 s |
| `verify` | `jobs/verify` | Prints `--status`, checks every recorded replay on disk, checks the database backups and the replay folder, and publishes the match index | 1,800 s | 600 s |
| `check` | `jobs/check` | Compares the published match index with the database | 600 s | 300 s |

`check` exits 1 when the published index differs from the database. After the
hub is restored from a backup, the next `update` or `verify` run repairs the
published index.

## Worker setup

Each entry reads `SWITCHBOARD_BULK`, the node's local folder holding
`dota_replays.db`. `update` and `verify` also read `DOTA_REPLAY_DIR`, the
TrueNAS replay folder. The publish step reads `SWITCHBOARD_HUB` and
`SWITCHBOARD_MODULE_TOKEN`, which the hub sets for each job; the tasks list no
`secrets`. An entry exits 1 when one of these folder variables
is unset or not a directory, when `$SWITCHBOARD_BULK/dota_replays.db` is missing,
or when `python3` is below 3.10. The entries never create a folder outside an
existing one on the share, so a missing mount fails the job instead of writing
to the local disk.

`DOTA_REPLAY_DIR` must name a folder inside the mounted share, never the
mount point itself: an unmounted mount point is an empty local directory, which
the entries would accept.

Backups live in `$DOTA_REPLAY_DIR/db-backups` as `dota_replays-YYYYMMDDTHHMMSSZ.db`.
The job keeps 14 backups and prunes older ones. Backups in subfolders are never
served by the hub. A shrink guard refuses to write or prune if the live database
has fewer players or matches than the newest earlier backup.

The `verify` job checks the newest backup with `db_copy.py --check` and checks
the replay folder with `replay_folder.py`. `backup-check` exits 1 if
`db-backups` is missing, holds no backup, holds more than 14 backups, if the
newest backup is more than 8 days old or more than one hour in the future, is
corrupt, or holds more rows than the live database. `folder-check` exits 1 if
the replay folder holds any entry at its top level that is neither a replay
file matching the hub's pattern nor the `db-backups` directory (the hub serves
any top-level file whose name matches its pattern). A replay file is a name
matching that pattern whose suffix is `.dem.bz2` or `.dem.zst`. `folder-check`
reads the first four bytes of every replay and also exits 1, naming each such
file, when the bytes are not the format its suffix claims: a `.dem.bz2` file
holding zstd or the reverse. Until switchboard#274
schedules `verify`, both checks run only when `verify` is submitted.

Add the entries in `switchboard/policy.example.toml` to the worker's policy on
the hub's Worker page, set `max_backstop_s` to at least 10,800, and set the
`bulk-dota-replays` resource to 1.

Set the worker's memory kill limit (`max_mem_mb`, on its Worker page) to 0 or at
least 3,072 MB. It is the only memory limit since switchboard 0.20.0, and
`update` peaked at 2,221 MB in job `20261110T071920660000Z-b089` (92 replays, a
765,624,320-byte database).

## The database's folder

`SWITCHBOARD_BULK` names the folder already holding `dota_replays.db` on the
node's local disk (on tiny-md, the folder the hand runs used). Stop hand runs
before the first job. The jobs run from the worker's mirror, never from that
folder, and the worker's account must own the folder. The folder may be an old
clone of this repository; never run `git clean -x` or `git clean -X` in it,
which deletes the database and its `-wal` file (`.gitignore` lists `*.db`,
`*.db-wal`, `*.db-shm`).

## Restoring a backup

To restore from a backup by hand:
1. Stop jobs on the hub.
2. Copy the chosen backup to the node's local disk:
   `cp "$DOTA_REPLAY_DIR/db-backups/<name>" "$SWITCHBOARD_BULK/dota_replays.db.restore"`
3. Check its integrity; the command must print `ok`:
   `sqlite3 "$SWITCHBOARD_BULK/dota_replays.db.restore" 'PRAGMA integrity_check;'`
4. Move the live database and its `-wal` and `-shm` files aside under one new name:
   `aside="$SWITCHBOARD_BULK/dota_replays.db.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"`
   `mv "$SWITCHBOARD_BULK/dota_replays.db" "$aside"`
   `for suffix in -wal -shm; do [ ! -e "$SWITCHBOARD_BULK/dota_replays.db$suffix" ] || mv "$SWITCHBOARD_BULK/dota_replays.db$suffix" "$aside$suffix"; done`
5. Rename the restored file into place:
   `mv "$SWITCHBOARD_BULK/dota_replays.db.restore" "$SWITCHBOARD_BULK/dota_replays.db"`
6. Resume jobs.

### Accepting a smaller database

Accepting a smaller database (a restore from a backup older than the newest, a hand edit that removed rows, or a newest backup that fails its check): create `db-backups/held/` and move into it every backup the next `update` would refuse against, that is, each newer than the one you accept, or the one that fails its check. Never move a backup into the replay folder itself: its name matches the hub's pattern and the hub would serve it. A folder inside `db-backups` is never counted, pruned or served. The next `update` then backs up and prunes normally. Delete `held/` by hand when you no longer need it.

## Repair 2026-11 replay names

Downloads from 2026-11-10 to the fix saved zstd replay bodies under the replay
URL's `.dem.bz2` name. `repair_replay_names.py` renames each such file in place,
without rewriting its bytes, and updates the `downloads` row that names it. It
never changes a bzip2 file, a `.part` file or a file of an unknown format. Run
it as the worker's account, with no dota-replays job queued or running.

The dry run changes nothing and exits 1 while work remains:

```bash
python3 repair_replay_names.py --db "$SWITCHBOARD_BULK/dota_replays.db" --replay-dir "$DOTA_REPLAY_DIR"
```

`N renames` is the number of files it will rename and `M row updates` the rows
an interrupted earlier run left. `conflicts` counts a name whose `.dem.zst`
file already exists, which it reports and never overwrites.

Apply it:

```bash
python3 repair_replay_names.py --db "$SWITCHBOARD_BULK/dota_replays.db" --replay-dir "$DOTA_REPLAY_DIR" --apply
```

`--apply` first copies the database to
`$SWITCHBOARD_BULK/dota_replays.db.before-repair-<UTC stamp>` beside it, and it
stops without renaming anything unless the copy passes `PRAGMA integrity_check`.
It exits 0 when nothing is left to repair, so a second run is the check. If
`--apply` stops early, run it again before any job. After restoring a database
backup taken before the repair, run the dry run and `--apply` again. `check`
reports differences until the following `verify` publishes the new names.

The hub schedule `dota-replays-update` stays removed until the repair ran.
switchboard's `bulk-mount-check` checks its sample with `bzip2 -t`, so pick a
`.dem.bz2` sample until switchboard accepts a zstd sample.

## Weekly trigger

The hub schedule `dota-replays-update` submits `update` every 604,800 seconds
from a Thursday 05:30 Pacific slot: 13:30 UTC, which is 05:30 PST and 06:30
PDT. It is pinned to one commit of `master`, so a merge to `master` reaches
the weekly run only when the coordinator re-adds the schedule pinned to the new
head. Re-adding under the same name replaces the schedule.

The coordinator re-adds it from the dev Mac, whose agent binding becomes the
schedule's creator. Approve that binding once, by running
`switchboard client request --agent --hub https://<hub> --wait` and approving its
code on the hub's Access page. Revoking the binding pauses the schedule, and a
paused schedule whose creator is revoked cannot be resumed: re-add it instead.

To re-add the schedule, set the two values at the top and run the script with
`sh`:

```sh
#!/bin/sh
# Re-add the weekly update schedule, pinned to master's head.
set -eu
HUB=      # the hub's URL
FIRST=    # the next Thursday 13:30 UTC, in the future, e.g. 2026-11-26T13:30:00Z
REPO=simonliu99/dota-replays
[ -n "$HUB" ] || { echo "set HUB" >&2; exit 2; }
[ -n "$FIRST" ] || { echo "set FIRST" >&2; exit 2; }
HEAD=$(gh api "repos/$REPO/commits/master" --jq .sha)
TREE=$(gh api "repos/$REPO/git/commits/$HEAD" --jq .tree.sha)
switchboard schedule add --hub "$HUB" --name dota-replays-update \
    --every 604800 --first "$FIRST" \
    --project dota-replays --task update --code git \
    --repo "$REPO" --url https://github.com/simonliu99/dota-replays.git \
    --base "$HEAD" --commit "$HEAD" --tree "$TREE" \
    --entry jobs/update --gres bulk-dota-replays=1 \
    --backstop 10800 --quiet 600
switchboard schedule list --hub "$HUB"
```

`add` prints `dota-replays-update`, and `list` prints a line beginning
`dota-replays-update task=update every_s=604800 next=<a Thursday>T13:30:00Z paused=false`.
The hub refuses a `FIRST` in the past.

To submit a job by hand:

```bash
scripts/switchboard-submit --hub https://<hub> update
```

## Catching up after a gap

`update` lists each player's 100 newest matches. Fetch a longer gap, such as a
skipped week, by hand on the node, with no `-n`:

```bash
python3 dota_replays.py --db "$SWITCHBOARD_BULK/dota_replays.db" --replay-dir "$DOTA_REPLAY_DIR"
```
