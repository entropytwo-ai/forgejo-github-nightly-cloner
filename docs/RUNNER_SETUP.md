# Forgejo runner setup

<a id="contents"></a>
## Contents

1. [What is Forgejo Runner](#1-what-is-forgejo-runner)
2. [Why runners are disabled by default](#2-why-runners-are-disabled-by-default)
3. [Enabling the runner](#3-enabling-the-runner)
4. [Runner registration](#4-runner-registration)
5. [Adapting GitHub Actions workflows](#5-adapting-github-actions-workflows)
6. [Common syntax differences](#6-common-syntax-differences)
7. [Disabling specific workflows on mirrors](#7-disabling-specific-workflows-on-mirrors)

## 1. What is Forgejo Runner

[Forgejo Runner](https://code.forgejo.org/forgejo/runner) executes Forgejo Actions jobs, a CI system largely compatible with GitHub Actions workflow syntax. Workflows live in `.forgejo/workflows/` (and `.github/workflows/` as a fallback).

[↑ Back to contents](#contents)

## 2. Why runners are disabled by default

- Every mirror contains `.github/workflows/`. With Actions enabled, each nightly sync could trigger jobs for dozens of repositories.
- Workflows may reference GitHub-only actions, secrets or environments and would fail or misbehave.
- Running untrusted code (forks, third-party actions) on the standby host is a security risk.

So `FORGEJO__actions__ENABLED=false` in `docker-compose.yml`.

[↑ Back to contents](#contents)

## 3. Enabling the runner

1. Copy `docker-compose.override.example.yml` to `docker-compose.override.yml`.
2. Uncomment the `forgejo` environment override (`FORGEJO__actions__ENABLED: "true"`), the `docker-in-docker` and `forgejo-runner` services, and the `runner-data` volume.
3. `docker compose up -d forgejo`.

[↑ Back to contents](#contents)

## 4. Runner registration

```bash
# 1. Get a registration token (instance-wide; or per repo/org in the UI: Settings -> Actions -> Runners)
docker compose exec -u git forgejo forgejo actions generate-runner-token

# 2. Register (writes /data/.runner) and create a default config
docker compose run --rm forgejo-runner forgejo-runner register --no-interactive \
  --instance http://forgejo:3000 --token <TOKEN> --name standby-runner \
  --labels docker:docker://node:20-bookworm
docker compose run --rm forgejo-runner sh -c 'forgejo-runner generate-config > /data/config.yml'

# 3. Start it
docker compose up -d docker-in-docker forgejo-runner
```

Check the runner shows as *Idle* under **Site Administration → Actions → Runners**. Enable Actions per repository in **Settings → Units → Actions** only where needed.

[↑ Back to contents](#contents)

## 5. Adapting GitHub Actions workflows

- Copy the workflow to `.forgejo/workflows/` in the writable repository so GitHub and Forgejo versions can diverge.
- Replace `runs-on: ubuntu-latest` with a label your runner provides (e.g. `docker`).
- Use fully qualified action URLs when an action is not on the default action mirror: `uses: https://github.com/actions/checkout@v4`.
- Recreate required secrets/variables in Forgejo (**Settings → Actions → Secrets**).

[↑ Back to contents](#contents)

## 6. Common syntax differences

| GitHub | Forgejo |
|---|---|
| `runs-on: ubuntu-latest` | `runs-on: docker` (any registered label) |
| `${{ github.* }}` context | Available; `${{ forge.* }}` / `${{ forgejo.* }}` also supported |
| `GITHUB_TOKEN` | Automatic token is provided (`${{ secrets.GITHUB_TOKEN }}` / `FORGEJO_TOKEN`) with Forgejo-scoped permissions |
| `permissions:` granular scopes | Partially supported |
| `concurrency:` | Not supported in older runner versions |
| Larger runners, `environment:` protection rules | Not available |
| `actions/cache`, `upload-artifact@v4` | Use versions compatible with Forgejo (e.g. `upload-artifact@v3`) |

[↑ Back to contents](#contents)

## 7. Disabling specific workflows on mirrors

- Keep the Actions unit disabled for mirror repositories (default when Actions was off at creation time).
- On writable repositories, move unwanted files out of `.forgejo/workflows/` / `.github/workflows/`, or guard jobs with `if: ${{ !startsWith(github.server_url, 'https://github.com') }}` / the inverse.
- You can disable a single workflow in the repository's **Actions** tab (⋯ → Disable workflow).

[↑ Back to contents](#contents)
