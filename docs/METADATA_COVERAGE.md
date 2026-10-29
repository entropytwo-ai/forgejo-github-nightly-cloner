# Metadata coverage

<a id="contents"></a>
## Contents

- [Summary](#summary)
- [Captured via the GitHub API](#captured-via-the-github-api)
- [Git data via the Forgejo mirror](#git-data-via-the-forgejo-mirror)
- [Not captured / unsupported](#not-captured--unsupported)
- [Notes on GitHub Actions workflows](#notes-on-github-actions-workflows)
- [Backup layout](#backup-layout)

## Summary

The standby has two independent layers:

| Layer | Mechanism | Frequency |
|---|---|---|
| Git data | Forgejo pull mirror (`POST /api/v1/repos/migrate`, `mirror: true`) | `forgejo.mirror_interval` (default 24h) plus nightly provisioning |
| Metadata | GitHub REST API → JSON files, staged and atomically promoted | Nightly (`SYNC_SCHEDULE`) |

Every backup's `manifest.json` repeats this coverage under `coverage.captured` / `coverage.unsupported`, so each snapshot is self-describing.

[↑ Back to contents](#contents)

## Captured via the GitHub API

Each type is written to `{backup}/{owner}/{repo}/{type}.json` as a JSON list, and its exact item count is recorded in the manifest.

| Type (`metadata_types`) | File | Contents | API endpoint |
|---|---|---|---|
| `issues` | `issues.json` | Title, body, state, labels, assignees, milestone, comment count, timestamps (PRs filtered out) | `GET /repos/{o}/{r}/issues?state=all` |
| `pull_requests` | `pull_requests.json` | Title, body, state, head/base refs and SHAs, merge status (`merged_at`, `merge_commit_sha`), requested reviewers, draft flag | `GET /repos/{o}/{r}/pulls?state=all` |
| `labels` | `labels.json` | Name, color, description | `GET /repos/{o}/{r}/labels` |
| `milestones` | `milestones.json` | Title, description, due date, state | `GET /repos/{o}/{r}/milestones?state=all` |
| `releases` | `releases.json` | Tag, name, body, draft/prerelease flags, **asset metadata** (name, size, download URL; binaries are not downloaded) | `GET /repos/{o}/{r}/releases` |
| `comments` | `comments.json` | Issue/PR conversation comments (`comment_type: "issue"`) and PR review diff comments (`comment_type: "review"`) | `GET /repos/{o}/{r}/issues/comments`, `GET /repos/{o}/{r}/pulls/comments` |
| `topics` | `topics.json` | Repository topics (list of strings) | `GET /repos/{o}/{r}/topics` |
| `collaborators` | `collaborators.json` | Collaborator logins and permissions. **May be inaccessible** (403) without push/admin rights; recorded as `null` count plus `inaccessible_types` in the manifest | `GET /repos/{o}/{r}/collaborators` |

Notes:

- PR commit lists and full review objects (approve/request-changes states) are not fetched per PR in this version, to keep API usage at O(pages) instead of O(PRs). Commits themselves are in the git mirror.
- Comment reactions are included only as the summary counts GitHub embeds in comment objects.

[↑ Back to contents](#contents)

## Git data via the Forgejo mirror

- All branches and tags
- Complete commit history
- LFS objects (if `forgejo.lfs: true`)
- Submodule definitions (`.gitmodules`; submodule repositories are mirrored only if they are themselves discovered)
- GitHub Actions workflow files (`.github/workflows/`) as static files
- `refs/pull/*` heads are **not** guaranteed: Forgejo mirrors fetch branches and tags.

[↑ Back to contents](#contents)

## Not captured / unsupported

| Item | Reason |
|---|---|
| GitHub Discussions | No REST export; GraphQL only, not implemented |
| GitHub Projects / project boards | GitHub-specific data model |
| Code scanning / Dependabot / secret scanning alerts | Security features, not exportable in a portable form |
| GitHub Packages | Separate registry, out of scope |
| Deployment environments and secrets | Intentionally excluded for security |
| Check runs / CI history / Actions logs and artifacts | Ephemeral, not mirrored |
| Commit status history | GitHub-specific |
| Webhook configurations | Contain secrets; intentionally excluded |
| Branch protection rules | Noted but not exported in this version |
| GitHub Pages configuration | Not exported |
| Wikis | Not mirrored (wiki is a separate git repository; mirror it manually if needed) |

[↑ Back to contents](#contents)

## Notes on GitHub Actions workflows

- The `.github/workflows/` directory is preserved as git data.
- Workflows require adaptation for Forgejo runner syntax differences.
- Forgejo Actions is disabled by default on this standby (`FORGEJO__actions__ENABLED=false`), so mirrored workflows never run automatically.
- See [RUNNER_SETUP.md](RUNNER_SETUP.md) for enabling local CI.

[↑ Back to contents](#contents)

## Backup layout

```
/backups/
├── 20261027/                    # promoted (complete, verified) backup
│   ├── manifest.json
│   ├── checksums.sha256         # sha256sum format, includes manifest.json
│   └── example-org/api-server/
│       ├── issues.json
│       ├── pull_requests.json
│       ├── labels.json
│       ├── milestones.json
│       ├── releases.json
│       ├── comments.json
│       ├── topics.json
│       └── collaborators.json   # absent if inaccessible
└── 20261028-staging/            # only present if the run failed or is in progress
```

Repository status values in the manifest: `succeeded`, `failed` (API/server error), `inaccessible` (every type returned 401/403/404), `skipped`.

[↑ Back to contents](#contents)
