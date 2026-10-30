# Deployment

A switchboard worker on the Maryland node runs the jobs in `jobs/`. The
database lives on the node's local disk at `$SWITCHBOARD_BULK/dota_replays.db`,
because SQLite's locking is not safe on a network share. The replays and a
checked copy of the database are on TrueNAS.
A failed copy leaves the previous one in place. The dev Mac submits `update`
once a week.

## Jobs

| Job | Entry | What it does | Backstop | Quiet limit |
| --- | --- | --- | --- | --- |
| `update` | `jobs/update` | Updates every tracked player's 100 newest matches, rechecks 30 unparsed matches, writes the database copy and publishes the match index | 10,800 s | 600 s |
| `verify` | `jobs/verify` | Prints `--status`, checks every recorded replay on disk and publishes the match index | 1,800 s | 600 s |
| `check` | `jobs/check` | Compares the published match index with the database | 600 s | 300 s |

`check` exits 1 when the published index differs from the database. After the
hub is restored from a backup, the next `update` or `verify` run repairs the
published index.

## Worker setup

Each entry reads `SWITCHBOARD_BULK`, the node's local folder holding
`dota_replays.db`. `update` and `verify` also read `DOTA_REPLAY_DIR`, the
TrueNAS replay folder, and `update` reads `DOTA_DB_COPY_DIR`, a TrueNAS folder
for the copy. The publish step reads `SWITCHBOARD_HUB` and the task secret
`SWITCHBOARD_MODULE_TOKEN`. An entry exits 1 when a folder is unset or not a
directory, or when `python3` is below 3.10. The entries never create a folder,
so a missing mount fails the job instead of writing to the local disk.

`DOTA_REPLAY_DIR` and `DOTA_DB_COPY_DIR` must each name a folder inside the
mounted share, never the mount point itself: an unmounted mount point is an
empty local directory, which the entries would accept.

Add the entries in `switchboard/policy.example.toml` to the worker's policy on
the hub's Worker page, set `max_backstop_s` to at least 10,800, and set the
`bulk-dota-replays` resource to 1.

## Moving the database to the node's local disk

1. On the node, as the worker's user: `mkdir -p <local>/dota-replays`.
2. Stop anything writing the current database on the share.
3. Copy it while nothing writes: `sqlite3 <old>/dota_replays.db ".backup '<local>/dota-replays/dota_replays.db'"`, then check it with `sqlite3 '<local>/dota-replays/dota_replays.db' 'PRAGMA integrity_check;'` and expect `ok`.
4. Run `jobs/update` once: it adds the matches played since the last run, downloads missing replays and writes the first copy. Then run `jobs/verify`.
5. Keep the share's original until both jobs pass. The copy folder is a
   separate path, so the original is never overwritten.

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
