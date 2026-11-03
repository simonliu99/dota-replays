# Deployment

A switchboard worker on the Maryland node runs the jobs in `jobs/`. The
database lives on the node's local disk at `$SWITCHBOARD_BULK/dota_replays.db`,
because SQLite's locking is not safe on a network share. The replays and
dated backups of the database are on TrueNAS. A failed backup leaves earlier
backups in place. The dev Mac submits `update` once a week.

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
TrueNAS replay folder. The publish step reads `SWITCHBOARD_HUB` and the task secret
`SWITCHBOARD_MODULE_TOKEN`. An entry exits 1 when one of these folder variables
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
any top-level file whose name matches its pattern). Until switchboard#274
schedules `verify`, both checks run only when `verify` is submitted.

Add the entries in `switchboard/policy.example.toml` to the worker's policy on
the hub's Worker page, set `max_backstop_s` to at least 10,800, and set the
`bulk-dota-replays` resource to 1.

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

## Weekly trigger

`switchboard/dev.dota-replays.update.plist` is a LaunchAgent for the dev Mac
that submits `update` every Thursday at 05:30 Pacific. launchd runs a time
missed while the Mac slept at its next wake, and `--not-before 05:00` refuses a
wake before 05:00, so that week's run waits for the following Thursday.

1. Mint an `sba_` agent token for the Mac on the hub's Access page and save it to `~/.config/switchboard/dota-replays-submit.token`, mode `0600`.
2. Edit the plist: `--hub` names the hub, and the token and script paths must
   match the Mac.
3. Install it:

   ```bash
   cp switchboard/dev.dota-replays.update.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.dota-replays.update.plist
   ```

Its output goes to `~/Library/Logs/dota-replays-update.log`. To submit a job by
hand:

```bash
scripts/switchboard-submit --hub https://<hub> --token-file ~/.config/switchboard/dota-replays-submit.token update
```

## Catching up after a gap

`update` lists each player's 100 newest matches. Fetch a longer gap, such as a
skipped week, by hand on the node, with no `-n`:

```bash
python3 dota_replays.py --db "$SWITCHBOARD_BULK/dota_replays.db" --replay-dir "$DOTA_REPLAY_DIR"
```
