# forgejo-github-nightly-cloner

Run [Forgejo](https://forgejo.org) next to GitHub as a warm standby: repositories in your GitHub accounts and organizations are discovered automatically, kept as Forgejo pull mirrors, and their issues, pull requests, releases and other supported metadata are backed up nightly as verified JSON snapshots.

<a id="contents"></a>
## Table of contents

- [Overview](#overview)
- [Design principles](#design-principles)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration reference](#configuration-reference)
- [Deployment](#deployment)
- [Commands](#commands)
- [Security notes](#security-notes)
- [Metadata coverage](#metadata-coverage)
- [Failover and restore](#failover-and-restore)
- [Development and testing](#development-and-testing)
- [Validated technical assumptions](#validated-technical-assumptions)
- [Project layout](#project-layout)
- [License](#license)

## Overview

A nightly job in the `worker` container:

1. **Discovers** every repository of the configured GitHub users and organizations (with include/exclude filters).
2. **Provisions** a Forgejo pull mirror for each new repository (private repositories included). Forgejo then keeps git data (branches, tags, history, LFS) in sync on its own interval.
3. **Backs up** issues, pull requests, labels, milestones, releases, comments, topics and collaborators to `/backups/YYYYMMDD/` using a staging → validate → promote strategy.
4. **Verifies** the backup (manifest, item counts, SHA-256 checksums) and applies retention.

When GitHub is unavailable you can freeze the mirrors, convert repositories to writable and keep working on Forgejo.

[↑ Back to contents](#contents)

## Design principles

- **Never destroy good data.** A failed backup never replaces the previous one; retention never runs after a failed run and always keeps at least two snapshots. Mirrors are never deleted automatically.
- **Distrust empty answers.** If GitHub suddenly returns zero repositories (or a source fails) while repositories were known before, discovery aborts with `DiscoveryError` instead of treating it as "everything was deleted".
- **Idempotent.** Re-running `sync` is always safe: existing mirrors are skipped, existing non-mirror repositories are never touched, `409 Conflict` is treated as "already exists".
- **One job at a time.** A PID lock file with stale-lock detection prevents overlapping runs; a failover lock blocks the scheduler entirely.
- **Secrets stay secret.** Tokens come from the environment only and are redacted from every log line and exception.
- **Boring dependencies.** Python standard library + `requests` + `PyYAML`, SQLite for state, cron for scheduling.

[↑ Back to contents](#contents)

## Architecture

```
            ┌──────────────────────── docker compose (bridge network "internal") ────────────────────────┐
 GitHub ◄───┤ worker (python:3.12-slim + cron)                     forgejo (codeberg.org/forgejo/forgejo:10)│
  API  ───► │  discover → mirror → backup → verify  ──REST /api/v1──►  pull mirrors ◄── git fetch ── GitHub │
            │  state: /state/state.db (SQLite)                       data: forgejo-data, forgejo-repos     │
            │  backups: /backups/YYYYMMDD/                           db: postgres:15 (postgres-data)        │
            └──────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Component | Purpose |
|---|---|
| `forgejo` | Standby forge holding the pull mirrors (Actions disabled, sign-in required) |
| `postgres` | Forgejo database |
| `worker` | Cron + Python jobs (`scripts/`) |
| `state-data` volume | SQLite state (`mirrors`, `backups`, `known_repos`, `discovery_runs`) and `failover.lock` |
| `backup-data` volume | Dated metadata snapshots |

[↑ Back to contents](#contents)

## Quick start

```bash
git clone https://github.com/entropytwo-ai/forgejo-github-nightly-cloner.git
cd forgejo-github-nightly-cloner

cp .env.example .env                       # fill in every placeholder
cp config/config.example.yml config/config.yml   # list your accounts / orgs
# generate secrets
openssl rand -hex 32                        # -> FORGEJO_SECRET_KEY
docker run --rm codeberg.org/forgejo/forgejo:10 forgejo generate secret INTERNAL_TOKEN   # -> FORGEJO_INTERNAL_TOKEN

# 1. start Forgejo + PostgreSQL
docker compose up -d postgres forgejo

# 2. create the admin user and the API token for the worker
docker compose exec -u git forgejo forgejo admin user create --admin \
  --username "$FORGEJO_ADMIN_USER" --password "$FORGEJO_ADMIN_PASSWORD" --email "$FORGEJO_ADMIN_EMAIL"
docker compose exec -u git forgejo forgejo admin user generate-access-token \
  --username "$FORGEJO_ADMIN_USER" --token-name worker --scopes all --raw
# put the printed token into FORGEJO_TOKEN in .env

# 3. create the owner for mirrors (an organization named like forgejo.owner), e.g. in the UI
#    or: curl -X POST -H "Authorization: token $FORGEJO_TOKEN" -H 'Content-Type: application/json' \
#          -d '{"username":"backup-user","visibility":"private"}' http://127.0.0.1:3000/api/v1/orgs

# 4. start the worker and try a dry run
docker compose up -d --build worker
docker compose exec worker python -m scripts.run --dry-run sync
docker compose exec worker python -m scripts.run sync          # first real run
docker compose exec worker python -m scripts.run status
```

(Load `.env` into your shell first, e.g. `set -a; . ./.env; set +a`, so the `$FORGEJO_ADMIN_*` variables resolve.)

[↑ Back to contents](#contents)

## Configuration reference

### `config/config.yml`

| Key | Default | Description |
|---|---|---|
| `github.accounts` | `[]` | GitHub users to mirror. For the token owner, private repos are listed via `/user/repos`; other users expose public repos only. |
| `github.organizations` | `[]` | Organizations to mirror (private repos need `repo` + `read:org`). |
| `github.exclude_repos` | `[]` | Case-insensitive `fnmatch` globs on `owner/repo`, e.g. `*/archived-*`. |
| `github.include_private` | `true` | Mirror private repositories. |
| `github.include_forks` | `false` | Mirror forks. |
| `github.include_archived` | `false` | Mirror archived repositories. |
| `forgejo.owner` | – (required) | Forgejo user/org owning the mirrors (must exist). |
| `forgejo.mirror_interval` | `24h` | Forgejo auto-sync interval (Go duration, ≥ `mirror.MIN_INTERVAL`). |
| `forgejo.lfs` | `true` | Fetch LFS objects. |
| `forgejo.force_private` | `false` | Create every mirror as private. |
| `forgejo.repo_name_template` | `{repo}` | Forgejo repo name; use `{owner}-{repo}` if names collide across owners (collisions are detected and reported). |
| `backup.retention_days` | `30` | Delete backups older than this (after a successful run only). |
| `backup.min_keep` | `2` | Minimum number of backups always kept (floored at 2). |
| `backup.max_failed_repos` | `0` | Failed repositories tolerated before the run is not promoted. |
| `backup.metadata_types` | all | Subset of `issues, pull_requests, labels, milestones, releases, comments, topics, collaborators`. |
| `sync.max_retries` | `3` | Retries for 429/5xx/network errors (exponential backoff). |
| `sync.retry_delay_seconds` | `30` | Base backoff delay. |
| `sync.rate_limit_buffer` | `100` | Sleep until reset when `X-RateLimit-Remaining` drops below this. |
| `sync.batch_size` | `50` | Reserved for batching; pagination uses 100 items per page. |
| `sync.lock_timeout_minutes` | `120` | Age after which a lock held by a live process is reported as possibly hung. |

### Environment (`.env`)

| Variable | Used by | Description |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | postgres, forgejo | Database credentials |
| `FORGEJO_ADMIN_USER`, `FORGEJO_ADMIN_PASSWORD`, `FORGEJO_ADMIN_EMAIL` | you (bootstrap) | Initial admin account |
| `FORGEJO_SECRET_KEY`, `FORGEJO_INTERNAL_TOKEN` | forgejo | Forgejo security secrets |
| `FORGEJO_DOMAIN`, `FORGEJO_ROOT_URL`, `FORGEJO_HTTP_PORT`, `FORGEJO_SSH_PORT` | forgejo | Public URL and localhost ports |
| `FORGEJO_URL` | worker | Forgejo base URL inside the network (`http://forgejo:3000`) |
| `FORGEJO_TOKEN` | worker | Forgejo API token |
| `FORGEJO_OWNER` | worker | Optional override of `forgejo.owner` |
| `GITHUB_TOKEN` | worker, forgejo (stored per mirror) | GitHub PAT |
| `BACKUP_RETENTION_DAYS` | worker | Overrides `backup.retention_days` |
| `SYNC_SCHEDULE` | worker | Cron expression, default `0 2 * * *` |
| `DRY_RUN` | worker | `true` = plan only |
| `LOG_LEVEL`, `LOG_FORMAT` | worker | `INFO`…; `text` or `json` |
| `TZ` | worker | Time zone for the schedule |

[↑ Back to contents](#contents)

## Deployment

- **Pin versions.** `codeberg.org/forgejo/forgejo:10` pins the major version; check <https://codeberg.org/forgejo/forgejo/releases> and read release notes before bumping.
- **TLS.** Ports are bound to `127.0.0.1`. Put a reverse proxy in front (see `docker-compose.override.example.yml`) and set `FORGEJO_ROOT_URL=https://…`.
- **Backups of the standby itself.** Snapshot the Docker volumes (`forgejo-data`, `forgejo-repos`, `postgres-data`, `backup-data`, `state-data`) to off-site storage; `docker compose exec -u git forgejo forgejo dump` produces a full Forgejo archive.
- **Monitoring.** `docker compose logs -f worker` shows job output (`LOG_FORMAT=json` for log shippers). Exit codes: `0` ok, `1` errors, `2` refused (lock / failover), `3` discovery safety stop, `4` configuration error.
- **Upgrades.** `git pull && docker compose build worker && docker compose up -d`.
- **bare-metal Forgejo.** `forgejo/app.ini.example` mirrors the compose settings.

[↑ Back to contents](#contents)

## Commands

All commands run inside the worker: `docker compose exec worker python -m scripts.run [--dry-run] [--log-level LEVEL] [--config PATH] <command>`.

| Command | Description |
|---|---|
| `sync` | Discovery + mirror provisioning + backup + verification under the execution lock. Refused while `/state/failover.lock` exists. |
| `discover` | Discovery only; prints included/excluded repositories. |
| `backup [--date YYYYMMDD]` | Metadata backup of all tracked repositories. |
| `verify [--date YYYYMMDD]` | Verify a backup (latest by default). |
| `failover disable\|enable\|status` | Freeze / resume mirror syncing. |
| `failover convert-repo OWNER/REPO` | Convert a Forgejo mirror into a writable repository. |
| `status` | Mirror, discovery, backup and failover summary. |

Metadata restore: `python -m scripts.restore_metadata --backup /backups/DATE --source GH_OWNER/REPO --target FJ_OWNER/REPO [--apply]`.

[↑ Back to contents](#contents)

## Security notes

- Secrets only in `.env` → environment variables; never in the YAML config, images, URLs or logs.
- Forgejo requires sign-in to view anything, registration is disabled, and mirrors of private repositories are private.
- Mirror creation is restricted to `github.com` (`migrations.ALLOWED_DOMAINS`) with local networks blocked.
- Required token scopes, rotation and log sanitization: see [docs/SECURITY.md](docs/SECURITY.md).

[↑ Back to contents](#contents)

## Metadata coverage

| Captured (JSON backups) | Git data (mirror) | Not captured |
|---|---|---|
| Issues, pull requests, labels, milestones, releases (asset metadata), issue & review comments, topics, collaborators | Branches, tags, full history, LFS, `.gitmodules`, `.github/workflows/` | Discussions, Projects, security alerts, Packages, environments/secrets, CI history, webhooks, branch protection, Pages, wikis |

Full details and file layout: [docs/METADATA_COVERAGE.md](docs/METADATA_COVERAGE.md).

[↑ Back to contents](#contents)

## Failover and restore

- **Failover:** `failover disable` → `failover convert-repo OWNER/REPO` → point your git remote at Forgejo. See [docs/FAILOVER.md](docs/FAILOVER.md).
- **Restore:** verify a snapshot, then import labels/milestones/issues/releases with `scripts/restore_metadata.py`; practise with `scripts/restore-drill.sh`. See [docs/RESTORE_RUNBOOK.md](docs/RESTORE_RUNBOOK.md).
- **CI on the standby:** disabled by default; enabling Forgejo Runner is described in [docs/RUNNER_SETUP.md](docs/RUNNER_SETUP.md).

[↑ Back to contents](#contents)

## Development and testing

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r worker/requirements.txt
pytest tests/ -v
```

The suite mocks all HTTP with `responses` / `unittest.mock` and covers rate limiting, pagination, retries, auth errors, token redaction, discovery safety, idempotent provisioning, per-repo failure isolation, staging → final promotion, failed-run protection, retention, manifest structure, verification of corrupt data, execution lock and failover lock. Tests can also run in the container: `docker compose run --rm worker pytest tests/ -v`.

Conventions: every Python file carries an SPDX GPL-3.0 header, type hints and docstrings on public functions.

[↑ Back to contents](#contents)

## Validated technical assumptions

Forgejo REST endpoints used (all under `/api/v1`, `Authorization: token …`):

| Endpoint | Used for | Notes |
|---|---|---|
| `GET /version` | Connectivity / server info | |
| `GET /api/healthz` (no `/v1`) | Container health check | Returns `{"status":"pass"}` |
| `POST /repos/migrate` | Create pull mirror | Body: `clone_addr`, `repo_name`, `repo_owner`, `mirror: true`, `mirror_interval`, `private`, `lfs`, `service: "github"`, `auth_token`; metadata flags off (not allowed for mirrors). `409` if the name exists. |
| `GET /repos/{owner}/{repo}` | Existence / `mirror` / `original_url` check | `404` → not present |
| `GET /orgs/{org}/repos`, `GET /users/{user}/repos` | Listing (`page`, `limit`) | |
| `POST /repos/{owner}/{repo}/mirror-sync` | Trigger immediate sync | |
| `PATCH /repos/{owner}/{repo}` `{"mirror_interval": "0"}` | Disable periodic sync | `0` disables, any duration re-enables |
| `PATCH /repos/{owner}/{repo}` `{"mirror": false}` | Convert to writable | **Not part of the documented `EditRepoOption` in all versions.** The client re-reads the repo and, if it is still a mirror, raises an error pointing to the UI action *Convert to regular repository* (sync is already disabled). |
| `POST /users/{user}/tokens` (basic auth) | Optional token bootstrap | |
| `DELETE /repos/{owner}/{repo}` | Never called by scheduled jobs | |
| Labels/milestones/issues/comments/releases endpoints | `restore_metadata.py` | Standard Gitea-compatible API |

GitHub REST (`X-GitHub-Api-Version: 2022-11-28`): repository listing (`/user/repos`, `/users/{u}/repos`, `/orgs/{o}/repos`), `/repos/{o}/{r}/{issues,pulls,labels,milestones,releases,issues/comments,pulls/comments,topics,collaborators}`, `/rate_limit`. Pagination follows `Link: rel="next"` with `per_page=100`; `/issues` includes PRs, which are filtered out.

What was verified in this repository:

- ✅ Unit tests (HTTP mocked) for all client behaviour and safety rules listed above.
- ✅ `docker compose config` renders the stack with the example `.env`.
- ⚠️ Not verified here: a live run against real Forgejo 10 / GitHub, and building the worker image (no Docker daemon available during development). Do a `--dry-run sync` and a restore drill after deploying.
- Forgejo environment-to-ini mapping: `FORGEJO__section__KEY`; dots in section names are written as `_0X2E_` (e.g. `FORGEJO__cron_0X2E_update_mirrors__SCHEDULE`).

[↑ Back to contents](#contents)

## Project layout

```
├── docker-compose.yml / docker-compose.override.example.yml / .env.example
├── config/config.example.yml       # what to mirror
├── forgejo/app.ini.example         # reference Forgejo configuration
├── worker/                         # Dockerfile, entrypoint.sh, crontab, requirements.txt
├── scripts/
│   ├── run.py                      # CLI
│   ├── discover.py mirror.py backup.py verify.py failover.py
│   ├── restore_metadata.py restore-drill.sh
│   └── common/                     # config, github_client, forgejo_client, lock, state, logging_utils
├── tests/                          # pytest suite + fixtures
└── docs/                           # coverage, restore, failover, runner, security
```

[↑ Back to contents](#contents)

## License

[GPL-3.0](LICENSE).

[↑ Back to contents](#contents)
