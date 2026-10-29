# Restore runbook

<a id="contents"></a>
## Contents

1. [Prerequisites check](#1-prerequisites-check)
2. [Start the Forgejo stack](#2-start-the-forgejo-stack)
3. [Verify Forgejo is healthy](#3-verify-forgejo-is-healthy)
4. [Check available backups](#4-check-available-backups)
5. [Verify backup integrity](#5-verify-backup-integrity)
6. [Disable sync](#6-disable-sync)
7. [Git data](#7-git-data)
8. [Metadata restore](#8-metadata-restore)
9. [Rollback to a previous snapshot](#9-rollback-to-a-previous-snapshot)
10. [Restore drill](#10-restore-drill)

All commands run on the Docker host from the repository directory. `W` is shorthand for
`docker compose exec worker`.

## 1. Prerequisites check

- [ ] Docker Engine with the Compose plugin (`docker compose version`).
- [ ] `.env` present with the same `FORGEJO_SECRET_KEY` as the original deployment (needed to decrypt stored mirror credentials).
- [ ] `config/config.yml` present.
- [ ] Volumes `forgejo-standby_forgejo-data`, `forgejo-standby_forgejo-repos`, `forgejo-standby_postgres-data`, `forgejo-standby_backup-data`, `forgejo-standby_state-data` exist (`docker volume ls`), or a host-level copy of them.
- [ ] A Forgejo API token with write access to the target owner (`FORGEJO_TOKEN`).

[↑ Back to contents](#contents)

## 2. Start the Forgejo stack

```bash
docker compose up -d postgres forgejo
docker compose up -d worker
```

[↑ Back to contents](#contents)

## 3. Verify Forgejo is healthy

```bash
docker compose ps                       # forgejo and postgres should be "healthy"
curl -fsS http://127.0.0.1:3000/api/healthz
curl -fsS http://127.0.0.1:3000/api/v1/version
```

Expected: `{"status":"pass",...}` and `{"version":"10.x.y"}`.

[↑ Back to contents](#contents)

## 4. Check available backups

```bash
docker compose exec worker python -m scripts.run status
```

Expected output (example):

```
Failover active : False
Mirrors         : {"active": 42}
Last discovery  : 2026-10-28T02:00:04+00:00 (42 repos)
Known repos     : 42
Backups (3)    : 20261028, 20261027, 20261026
  20261028: {"total": 42, "succeeded": 41, "failed": 0, "inaccessible": 1, "skipped": 0}
```

A listed `*-staging` directory is a failed or in-progress run and must not be used for restore.

[↑ Back to contents](#contents)

## 5. Verify backup integrity

```bash
docker compose exec worker python -m scripts.run verify --date 20261028
```

Checks manifest structure, per-type item counts, JSON validity and SHA-256 checksums (manifest and `checksums.sha256`). Expected: `Backup 20261028: OK`. If it reports `FAILED`, pick the previous date.

[↑ Back to contents](#contents)

## 6. Disable sync

```bash
docker compose exec worker python -m scripts.run failover disable
docker compose exec worker python -m scripts.run failover status   # "failover_active": true
```

This writes `/state/failover.lock` (the scheduler refuses to run while it exists) and sets `mirror_interval=0` on every mirror.

[↑ Back to contents](#contents)

## 7. Git data

Mirrors already contain the full git history, branches, tags and LFS objects. To develop on a repository, convert it to writable:

```bash
docker compose exec worker python -m scripts.run failover convert-repo backup-user/api-server
```

If your Forgejo version does not support conversion via API, the command says so; finish in the web UI under **Settings → Danger Zone → Convert to regular repository** (sync is already disabled at that point).

[↑ Back to contents](#contents)

## 8. Metadata restore

Forgejo's migration API cannot import from local JSON, so `scripts/restore_metadata.py` re-creates objects through the regular API:

| Restored | How |
|---|---|
| Labels | Created if missing (matched by name) |
| Milestones | Created if missing (matched by title) |
| Issues + conversation comments | Created with a provenance header and a hidden `<!-- github-import: owner/repo#N -->` marker (re-runs skip already imported items) |
| Pull requests | Imported as issues labelled `imported-pull-request` (code is in git) |
| Releases | Created for tags that exist in the mirror; assets listed as links, not re-uploaded |

Limitations: original authors, numbers and timestamps cannot be set via the API; they are recorded in the body. Review comments are kept in the JSON only.

```bash
# Dry-run first (default)
docker compose exec worker python -m scripts.restore_metadata \
  --backup /backups/20261028 --source example-org/api-server --target backup-user/api-server
# Then apply
docker compose exec worker python -m scripts.restore_metadata \
  --backup /backups/20261028 --source example-org/api-server --target backup-user/api-server --apply
```

The script prints a JSON summary such as `{"mode": "apply", "stats": {"created_issues": 12, "created_labels": 5, ...}}`.

[↑ Back to contents](#contents)

## 9. Rollback to a previous snapshot

Backups are immutable dated directories, so rolling back means choosing an older date:

1. `python -m scripts.run status` – list dates.
2. `python -m scripts.run verify --date <older>`.
3. Re-run `restore_metadata` with `--backup /backups/<older>` into a fresh target repository (imports are additive; to undo an import, delete and re-create the target or close the imported issues).
4. For git data, Forgejo keeps the mirror state at the last successful sync. To roll back a branch, use `git push --force-with-lease` on the writable repository from a known-good commit.

Retention never deletes the two newest backups and never runs after a failed backup.

[↑ Back to contents](#contents)

## 10. Restore drill

Run at least quarterly:

```bash
scripts/restore-drill.sh                     # latest backup, first succeeded repo, dry-run
DRILL_APPLY=1 scripts/restore-drill.sh 20261028 example-org/api-server backup-user/restore-drill-api-server
```

The drill verifies the backup, selects a repository, and either dry-runs the import or (with `DRILL_APPLY=1`) creates a private throwaway repository and imports labels, milestones and issues into it. Expected final line:

```
== Restore drill finished successfully for example-org/api-server (20261028)
```

Delete the drill repository afterwards in the Forgejo UI.

[↑ Back to contents](#contents)
