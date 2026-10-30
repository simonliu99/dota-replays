# Agent workflow

Shared rules and skills: the user-level install of `simonliu99/agent-workflow` (its README). This file holds only this project's block.

## This project

- **Project:** `simonliu99/dota-replays`: a Python CLI (`dota_replays.py`, `opendota_client.py`, `database.py`) that tracks players' matches through the OpenDota API, stores them in SQLite and downloads replays to a TrueNAS share, with `publish.py` (the match index), `db_copy.py` (the database copy), the switchboard job entries in `jobs/` and `scripts/switchboard-submit`. Sibling: `simonliu99/switchboard`, whose dota-replays module runs these jobs and shows the match list (switchboard #29).
- **Engineering rules:** none beyond the shared rules. Python 3.10 or later, standard library only (user decision 2026-10-28, switchboard #29); no OpenDota API key. Tests never write to the working directory or call OpenDota.
- **Records:** state file `/Users/simonliu/Code/scratch/dota-replays/state.md`. Queue: this repository's issues. The design record is switchboard #29's approved plan, named in the state file.
- **PR check:** `gh pr view <n> -R simonliu99/dota-replays --json headRefOid --jq .headRefOid`. **Sequence:** `python3.13 -m unittest discover -v` from the repository root under a 570 s bound, run directly (no run-queue resources). The worker runs it after its last commit; the integrator runs it on the merge tree. **Merge steps:** AGENTS.md §6 (the coordinator's scripts are not versioned yet).
- **Integration branch:** `master`.
- **Coordinator ops:** `<scratch root>/ops/<coordinator id>/`. **Scratch root:** `/Users/simonliu/Code/scratch/dota-replays`. **Paseo project:** `dota-replays`, on the Mac daemon.
- **Hosts:** the Mac runs development and every agent; the Maryland node runs the jobs as a switchboard worker, which reads the database from the worker's local folder (`SWITCHBOARD_BULK`, switchboard #29 level 1); TrueNAS holds the replays. Agents never run commands on the Maryland node or TrueNAS.
- **Risk tiers:** high: credentials or tokens, and anything that deletes or overwrites replays or the database; medium: other code changes; low: docs and records.
- **Issue domains:** none; titles are `type: description`.
- **Cross-repository:** switchboard owns the module contract (manifest, document store, job environment); this repository adopts it after the switchboard side merges.
- **Aggregate facts at integration:** none.
