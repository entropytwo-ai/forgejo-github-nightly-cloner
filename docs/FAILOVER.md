# Failover

<a id="contents"></a>
## Contents

1. [When to failover](#1-when-to-failover)
2. [Failover procedure](#2-failover-procedure)
3. [Re-enabling sync after development](#3-re-enabling-sync-after-development)
4. [Preventing later jobs from overwriting local changes](#4-preventing-later-jobs-from-overwriting-local-changes)
5. [Safety notes](#5-safety-notes)

## 1. When to failover

- GitHub is unavailable for an extended period (outage, account suspension, network block).
- You need to keep developing while GitHub access is revoked or under investigation.
- A GitHub repository was compromised or force-pushed and you must work from the last known-good mirror.

Do **not** failover for short GitHub blips: the nightly job retries and simply catches up later.

[↑ Back to contents](#contents)

## 2. Failover procedure

1. **Disable scheduled sync**

   ```bash
   docker compose exec worker python -m scripts.run failover disable
   ```

   The lock `/state/failover.lock` is written *before* any API call, so the scheduler is blocked even if some mirrors cannot be updated. Every mirror gets `mirror_interval=0` (Forgejo stops pulling from GitHub).

2. **Verify sync is disabled and the lock is in place**

   ```bash
   docker compose exec worker python -m scripts.run failover status
   docker compose exec worker python -m scripts.run sync     # must exit 2 with "sync REFUSED"
   ```

3. **Convert a repository to writable**

   ```bash
   docker compose exec worker python -m scripts.run failover convert-repo backup-user/api-server
   ```

   If the API conversion is unsupported by your Forgejo version, complete it in **Settings → Danger Zone → Convert to regular repository**.

4. **Point your local clone at Forgejo**

   ```bash
   git remote add forgejo https://git.example.com/backup-user/api-server.git
   git fetch forgejo
   git branch -u forgejo/main main
   ```

5. **Test push/pull**

   ```bash
   git commit --allow-empty -m "failover test" && git push forgejo main && git pull forgejo main
   ```

[↑ Back to contents](#contents)

## 3. Re-enabling sync after development

1. Push local work back to GitHub first (from your clone: `git push origin <branch>`), or open PRs there.
2. Re-enable mirrors and remove the lock:

   ```bash
   docker compose exec worker python -m scripts.run failover enable
   ```

   Only repositories that are still mirrors are re-enabled; the lock is removed only if all of them succeeded.
3. Converted (writable) repositories are not mirrors anymore. To return one to mirror mode: rename or delete it in Forgejo **after** its changes are on GitHub, mark it for re-creation by removing its row (`DELETE FROM mirrors WHERE repo_full_name='owner/repo'` in `/state/state.db`), and let the next `sync` create a fresh mirror.

[↑ Back to contents](#contents)

## 4. Preventing later jobs from overwriting local changes

- `failover.lock` blocks every scheduled `sync`.
- Converted repositories are stored with status `writable`; provisioning skips them and `failover enable` never re-enables them.
- Provisioning never modifies or deletes an existing **non-mirror** repository with the same name.
- Discovery never deletes mirrors, even when repositories vanish from GitHub.

[↑ Back to contents](#contents)

## 5. Safety notes

- Do not delete `failover.lock` by hand while people are pushing to Forgejo; use `failover enable`.
- Keep `FORGEJO_SECRET_KEY` unchanged; Forgejo needs it to decrypt stored mirror credentials.
- Pull mirrors that are still in mirror mode reject pushes; that is expected.
- Metadata backups continue to work during failover via `python -m scripts.run backup` (manual), as long as GitHub's API is reachable.

[↑ Back to contents](#contents)
