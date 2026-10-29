#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 forgejo-github-nightly-cloner contributors
#
# Restore drill: proves that a backup can be verified and imported, without
# touching production repositories.
#
#   scripts/restore-drill.sh [YYYYMMDD] [GITHUB_OWNER/REPO] [FORGEJO_OWNER/DRILL_REPO]
#
# Steps:
#   1. Pick the backup (latest if no date given) and verify it (checksums, counts).
#   2. Pick a repository from the manifest (first succeeded one if not given).
#   3. Dry-run the metadata import against the drill target.
#   4. If DRILL_APPLY=1, create the drill repo (empty, private) and import for real.
#
# Run from the host next to docker-compose.yml. Uses the running `worker` container.
set -euo pipefail

DATE="${1:-}"
SOURCE="${2:-}"
TARGET="${3:-}"
COMPOSE="${COMPOSE:-docker compose}"

run() { ${COMPOSE} exec -T worker "$@"; }

echo "== 1. Selecting backup"
if [[ -z "${DATE}" ]]; then
  DATE="$(run sh -c 'ls -1 /backups | grep -E "^[0-9]{8}$" | sort -r | head -n1')"
fi
[[ -n "${DATE}" ]] || { echo "No backups found"; exit 1; }
echo "Backup date: ${DATE}"

echo "== 2. Verifying backup integrity"
run python -m scripts.run verify --date "${DATE}"

echo "== 3. Selecting repository"
if [[ -z "${SOURCE}" ]]; then
  SOURCE="$(run python -c "import json;m=json.load(open('/backups/${DATE}/manifest.json'));print(next(k for k,v in sorted(m['repositories'].items()) if v['status']=='succeeded'))")"
fi
OWNER="$(run sh -c 'echo "${FORGEJO_OWNER:-}"')"
TARGET="${TARGET:-${OWNER:-backup-user}/restore-drill-${SOURCE##*/}}"
echo "Source: ${SOURCE}  ->  Drill target: ${TARGET}"

if [[ "${DRILL_APPLY:-0}" == "1" ]]; then
  echo "== 4. Creating drill repository ${TARGET} (if missing)"
  run python - "${TARGET}" <<'PY'
import os, sys
from scripts.common.forgejo_client import ForgejoClient, ForgejoConflictError
owner, name = sys.argv[1].split("/", 1)
c = ForgejoClient(os.environ["FORGEJO_URL"], os.environ["FORGEJO_TOKEN"])
if c.get_repo(owner, name) is None:
    me = c.request("GET", "/user")["login"]
    path = "/user/repos" if me == owner else f"/orgs/{owner}/repos"
    try:
        c.request("POST", path, json_body={"name": name, "private": True, "description": "restore drill"})
    except ForgejoConflictError:
        pass
print("drill repo ready")
PY
  echo "== 5. Importing metadata"
  run python -m scripts.restore_metadata --backup "/backups/${DATE}" --source "${SOURCE}" --target "${TARGET}" --apply --skip releases
else
  echo "== 4. Dry-run import (set DRILL_APPLY=1 to import into a throwaway repo)"
  if run python -c "import os,sys;from scripts.common.forgejo_client import ForgejoClient as C;o,n='${TARGET}'.split('/',1);sys.exit(0 if C(os.environ['FORGEJO_URL'],os.environ['FORGEJO_TOKEN']).get_repo(o,n) else 1)"; then
    run python -m scripts.restore_metadata --backup "/backups/${DATE}" --source "${SOURCE}" --target "${TARGET}"
  else
    echo "Drill target ${TARGET} does not exist; showing backup contents instead:"
    run sh -c "ls -l /backups/${DATE}/${SOURCE}/"
  fi
fi

echo "== Restore drill finished successfully for ${SOURCE} (${DATE})"
