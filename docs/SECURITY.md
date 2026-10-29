# Security

<a id="contents"></a>
## Contents

1. [Credential management](#1-credential-management)
2. [Network security](#2-network-security)
3. [Access control](#3-access-control)
4. [Token scope requirements](#4-token-scope-requirements)
5. [Rotating credentials](#5-rotating-credentials)
6. [Log sanitization](#6-log-sanitization)

## 1. Credential management

- All secrets live in `.env` (git-ignored) and reach containers as environment variables. Nothing is hardcoded; compose fails fast (`${VAR:?}`) if a required secret is missing.
- `config/config.yml` holds no secrets and is mounted read-only.
- Inside the worker, cron needs the environment in a file: `/run/worker.env` is written with `umask 077` (mode 0600, root only) and lives in the container filesystem, never in an image layer or volume.
- The GitHub token is sent to Forgejo only in the migration request body (`auth_token`), never in a clone URL. Forgejo stores it encrypted with `FORGEJO_SECRET_KEY` to sync private mirrors.
- Tokens never appear in exception messages: API errors are passed through the redactor before being raised.

[↑ Back to contents](#contents)

## 2. Network security

- Forgejo ports are bound to `127.0.0.1` only. Publish the UI through a TLS reverse proxy (Caddy/nginx/Traefik; example in `docker-compose.override.example.yml`) and set `FORGEJO_ROOT_URL` to the `https://` URL.
- PostgreSQL has no published port; it is reachable only on the compose network.
- The compose network is a normal bridge (outbound internet is required to reach GitHub).
- `migrations.ALLOWED_DOMAINS=github.com,*.github.com` and `ALLOW_LOCALNETWORKS=false` prevent mirror creation from internal addresses (SSRF).

[↑ Back to contents](#contents)

## 3. Access control

- `REQUIRE_SIGNIN_VIEW=true`: nothing is visible anonymously.
- `DISABLE_REGISTRATION=true`: accounts are created by an admin.
- Mirrors of private GitHub repositories are created private (`private` follows GitHub; set `forgejo.force_private: true` to make every mirror private).
- Use a dedicated Forgejo organization (e.g. `backup-user`) and grant teams read access only. Anyone with access to the Forgejo owner can read every mirrored private repository.
- Backups in the `backup-data` volume contain issue/PR text of private repositories; protect host access and any off-site copies (encrypt them).

[↑ Back to contents](#contents)

## 4. Token scope requirements

| Token | Required scopes |
|---|---|
| GitHub classic PAT | `repo` (private repositories, issues, PRs), `read:org` (organization repository listing). Public-only: `public_repo` + `read:org` |
| GitHub fine-grained PAT | Repository access to the selected owners; permissions **Metadata, Contents, Issues, Pull requests: read-only**; Administration: read for collaborators (optional) |
| Forgejo | Token of an admin (or of a user who owns/administers `forgejo.owner`) with `write:repository`, `write:organization`, `read:user` (`all` is simplest for an admin bootstrap token) |

Organizations with SAML SSO require authorizing the GitHub PAT for SSO.

[↑ Back to contents](#contents)

## 5. Rotating credentials

1. **GitHub token**: create the new token, update `GITHUB_TOKEN` in `.env`, `docker compose up -d worker`. Existing mirrors keep the old token stored in Forgejo: update each mirror's credentials in **Settings → Mirror settings** (or delete and let `sync` recreate the mirror), then revoke the old token.
2. **Forgejo token**: create a new token (`forgejo admin user generate-access-token`), update `FORGEJO_TOKEN`, restart the worker, delete the old token in the UI.
3. **Database password**: `ALTER USER forgejo PASSWORD '...'` in PostgreSQL, update `.env`, `docker compose up -d`.
4. **`FORGEJO_SECRET_KEY`**: do not rotate casually; stored mirror credentials become undecryptable.

[↑ Back to contents](#contents)

## 6. Log sanitization

`scripts/common/logging_utils.py` installs a filter on every log record that:

- replaces every registered secret value (GitHub token, Forgejo token, admin password) with `***REDACTED***`;
- masks token-shaped strings (`ghp_…`, `github_pat_…`, `Authorization: token/Bearer …`, `password=…`, `https://user:pass@host`);
- re-applies redaction to formatted exceptions and to the final formatted line (text and JSON formats).

`urllib3` debug logging (which can include headers) is capped at WARNING. Tests in `tests/test_github_client.py` assert that tokens never reach exceptions or log output.

[↑ Back to contents](#contents)
