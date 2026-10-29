#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
#
# First-time setup helper. Safe to re-run: existing files are never overwritten.
#
#   ./setup.sh
#
# 1. Checks that Docker and Docker Compose v2 are installed.
# 2. Creates .env from .env.example (auto-generating the internal random
#    secrets) and opens it in ${EDITOR:-nano}.
# 3. Creates config/config.yml from config/config.example.yml and opens it.
# 4. Creates ./backups and ./state.
set -euo pipefail

cd "$(dirname "$0")"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

EDITOR_CMD="${EDITOR:-nano}"

open_editor() {
  local file="$1"
  if [[ ! -t 0 || ! -t 1 ]]; then
    warn "No interactive terminal: edit ${file} manually."
    return 0
  fi
  if ! command -v "${EDITOR_CMD%% *}" >/dev/null 2>&1; then
    warn "Editor '${EDITOR_CMD}' not found: edit ${file} manually (or set EDITOR=vi)."
    return 0
  fi
  read -r -p "Press Enter to open ${file} in ${EDITOR_CMD} (Ctrl+C to abort)... " _
  ${EDITOR_CMD} "${file}"
}

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

# --- 1. Prerequisites --------------------------------------------------------
info "Checking prerequisites"
command -v docker >/dev/null 2>&1 \
  || die "Docker is not installed. Install Docker Engine 24+: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 is not available ('docker compose'). Install the compose plugin: https://docs.docker.com/compose/install/linux/"
ok "$(docker --version)"
ok "$(docker compose version)"
if ! docker info >/dev/null 2>&1; then
  warn "Cannot talk to the Docker daemon. Start it (sudo systemctl start docker) or add your user to the 'docker' group."
fi

# --- 2. .env -----------------------------------------------------------------
if [[ -f .env ]]; then
  ok ".env already exists - leaving it unchanged"
else
  [[ -f .env.example ]] || die ".env.example not found - run this script from the repository root"
  cp .env.example .env
  chmod 600 .env
  # Fill in the secrets nobody needs to remember.
  sed -i \
    -e "s|^POSTGRES_PASSWORD=change-me.*|POSTGRES_PASSWORD=$(random_hex 24)|" \
    -e "s|^FORGEJO_SECRET_KEY=change-me.*|FORGEJO_SECRET_KEY=$(random_hex 32)|" \
    -e "s|^FORGEJO_INTERNAL_TOKEN=change-me.*|FORGEJO_INTERNAL_TOKEN=$(random_hex 48)|" \
    .env
  ok "Created .env (POSTGRES_PASSWORD, FORGEJO_SECRET_KEY, FORGEJO_INTERNAL_TOKEN generated)"
  info "Now set at least: GITHUB_TOKEN, FORGEJO_ADMIN_PASSWORD, FORGEJO_ADMIN_EMAIL."
  info "Leave FORGEJO_TOKEN as is for now - you create it after Forgejo starts (README step 5)."
  open_editor .env
fi

# --- 3. config/config.yml ------------------------------------------------------
if [[ -f config/config.yml ]]; then
  ok "config/config.yml already exists - leaving it unchanged"
else
  [[ -f config/config.example.yml ]] || die "config/config.example.yml not found"
  cp config/config.example.yml config/config.yml
  ok "Created config/config.yml"
  info "Set github.accounts / github.organizations and forgejo.owner (replace the example-* values)."
  open_editor config/config.yml
fi

# --- 4. Local directories ----------------------------------------------------------
mkdir -p ./backups ./state
ok "Directories ./backups and ./state are present"

# --- Summary -----------------------------------------------------------------------
remaining=()
grep -qE '^GITHUB_TOKEN=(change-me.*)?$' .env && remaining+=("GITHUB_TOKEN in .env")
grep -qE '^FORGEJO_ADMIN_PASSWORD=(change-me.*)?$' .env && remaining+=("FORGEJO_ADMIN_PASSWORD in .env")
grep -qE '^\s*-\s*example-(user|org)\s*$' config/config.yml && remaining+=("example-user / example-org in config/config.yml")
if ((${#remaining[@]})); then
  warn "Still contains placeholders:"
  for item in "${remaining[@]}"; do warn "  - ${item}"; done
  warn "Edit them and re-run ./setup.sh (it will not overwrite anything)."
fi

echo
ok "Setup finished."
echo "Next: run \`docker compose up -d\`"
