#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Worker entrypoint: installs the crontab from $SYNC_SCHEDULE, starts cron and
# streams job output to stdout. If arguments are given they are executed
# instead (e.g. `docker compose run --rm worker python -m scripts.run status`).
set -euo pipefail

if [[ $# -gt 0 ]]; then
  exec "$@"
fi

SCHEDULE="${SYNC_SCHEDULE:-0 2 * * *}"
# Basic sanity check: five cron fields or an @macro.
if ! [[ "${SCHEDULE}" =~ ^@(hourly|daily|weekly|monthly|yearly|annually|midnight)$ ]] \
   && [[ $(echo "${SCHEDULE}" | awk '{print NF}') -ne 5 ]]; then
  echo "Invalid SYNC_SCHEDULE '${SCHEDULE}' (expected 5 cron fields)" >&2
  exit 1
fi

# Persist only the variables jobs need, readable by root only. Values are never printed.
umask 077
export -p | grep -E '^declare -x (GITHUB_|FORGEJO_|CONFIG_FILE|STATE_DIR|BACKUP_DIR|LOCK_DIR|BACKUP_RETENTION_DAYS|DRY_RUN|LOG_LEVEL|LOG_FORMAT|PYTHON|TZ)' > /run/worker.env || true
chmod 0600 /run/worker.env

ESCAPED_SCHEDULE=$(printf '%s' "${SCHEDULE}" | sed -e 's/[\/&]/\\&/g')
sed "s/__SYNC_SCHEDULE__/${ESCAPED_SCHEDULE}/" /app/crontab.template > /etc/cron.d/forgejo-sync
chmod 0644 /etc/cron.d/forgejo-sync
touch /var/log/cron.log

echo "forgejo-github-nightly-cloner worker: schedule '${SCHEDULE}' (TZ=${TZ:-UTC}), DRY_RUN=${DRY_RUN:-false}"

# Validate configuration early so mistakes show up in `docker compose logs`.
(cd /app && python -m scripts.run status) || echo "WARNING: 'status' failed - check config and tokens" >&2

shutdown() {
  echo "Received stop signal, stopping cron"
  [[ -n "${CRON_PID:-}" ]] && kill "${CRON_PID}" 2>/dev/null || true
  [[ -n "${TAIL_PID:-}" ]] && kill "${TAIL_PID}" 2>/dev/null || true
  exit 0
}
trap shutdown SIGTERM SIGINT

cron -f &
CRON_PID=$!
tail -n 0 -F /var/log/cron.log &
TAIL_PID=$!
wait -n "${CRON_PID}" "${TAIL_PID}"
echo "cron or log tail exited unexpectedly" >&2
exit 1
