# Working with this repo

This is the production codebase for the MOEX Assistant trading bot. The
service runs continuously on the server (supervisor-managed `moex`
process) and trades in the Tinkoff sandbox via cron jobs. Any change you
push here ships to live cron within seconds — there is no staging env.

## Branch model

- **`main`** — what the production bot is running. Every commit on main
  triggers a `post-commit` hook that backs up `moex.db` and bundles the
  tree. Treat every main commit as production traffic.
- **`feature/<name>`** — anything that is not a one-line hotfix. Even a
  single-file change with logic goes here.

## How to ship a change

1. Create a feature branch from the current main:
   ```bash
   sudo -C /root/moex git checkout -b feature/short-name
   ```
2. Make the change. The pre-commit hook will run `pytest tests/ -x` and
   block the commit if anything breaks. Run tests manually too:
   ```bash
   cd /root/moex && sudo venv/bin/pytest tests/ -x
   ```
3. Commit. Use a message that says *why* the change exists, not *what*
   it does (the diff already says what). Example:
   ```
   fix: gate intraday signals by broker price to drop stale ISS candles

   ISS 1m candles lag the broker price by minutes; intraday_monitor was
   producing proposals at 916.95 while the broker traded 940.30. New
   INTRADAY_PRICE_DRIFT_PCT=1.0 gate drops the signal when drift exceeds
   1%.
   ```
4. Verify on the running bot by tailing the log:
   ```bash
   sudo tail -f /root/moex-app/logs/moex_*.log
   ```
5. Merge to main when you are confident. From /root/moex:
   ```bash
   sudo -C /root/moex git checkout main
   sudo -C /root/moex git merge --no-ff feature/short-name
   ```
   The post-commit hook fires on the merge commit. After the merge,
   supervisor keeps running the old code; **restart only if** the change
   is in code that runs at import time (e.g. `main.py`, scheduler setup,
   new module wired into `core/`). For changes inside an existing
   scheduler cron job (e.g. `run_intraday_monitor`), the next cron tick
   picks up the new code automatically.
6. Restart the service when needed:
   ```bash
   sudo supervisorctl restart moex
   curl -s http://localhost:8080/health
   ```

## When to bypass the pre-commit hook

Only for emergencies: server down, hotfix in flight, broken CI blocking a
rollback. Use `git commit --no-verify` and follow up by fixing the tests
in the next commit. Never leave `--no-verify` commits in main without a
follow-up fix.

## What NOT to commit

- Real broker token (`/root/moex/.env.tinkoff`) — already in .gitignore
- Any file matching `id_*` (SSH keys)
- Any `*.bak*` (use the working tree; old backups stay on disk for a
  reason and git history is the right place to look)

## Recovery

- **Database snapshot**: `sudo sqlite3 new.db ".backup /root/backups/moex-db/moex-<ts>.db"`
  or simply `cp` — sqlite3 .backup handles WAL consistency.
- **Code snapshot**: `git clone /root/backups/code/code-<ts>.bundle restored/`
- **Worst case (disk wipe)**: restore the latest bundle, then drop the
  latest DB backup into `/root/moex/data/moex.db`.

## Questions?

Read `project_moex_how_it_works_2026-08-01.md` and the memory entries for
recent fixes (search for "MOEX Assistant" in the memory index).
